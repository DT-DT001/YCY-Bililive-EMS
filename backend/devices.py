from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from bleak import BleakClient, BleakScanner

from .protocol import (
    NOTIFY_UUID,
    SERVICE_UUID,
    WRITE_UUID,
    ChannelReport,
    generation1_control,
    generation2_fixed,
    generation2_realtime,
    parse_notification,
    query,
)
from .scheduler import ChannelOutput


DeviceCallback = Callable[[], Awaitable[None]]
GENERATION1_MIN_WRITE_INTERVAL = 0.1
GENERATION1_ERROR_WINDOW = 5.0
GENERATION1_ERROR_THRESHOLD = 3
GENERATION1_RETRY_DELAY = 0.2


@dataclass(slots=True)
class DeviceState:
    id: str
    name: str
    generation: int
    connected: bool = False
    battery: int | None = None
    error: str = ""
    channels: dict[str, dict] = field(
        default_factory=lambda: {
            "A": {"electrode_state": 0, "enabled": False, "strength": 0, "mode": 0x11},
            "B": {"electrode_state": 0, "enabled": False, "strength": 0, "mode": 0x11},
        }
    )


class DeviceConnection:
    def __init__(self, state: DeviceState, notify_change: DeviceCallback) -> None:
        self.state = state
        self.notify_change = notify_change
        self.client: BleakClient | None = None
        self.outputs = {"A": ChannelOutput(), "B": ChannelOutput()}
        self._write_lock = asyncio.Lock()
        self._last_write_at = 0.0
        self._last_control_packet: bytes | None = None
        self._data_error_times: list[float] = []
        self._recovery_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self.client = BleakClient(self.state.id, disconnected_callback=self._disconnected)
        try:
            await self.client.connect()
            await self.client.start_notify(NOTIFY_UUID, self._notification)
            self.state.connected = True
            self.state.error = ""
            await self._query_state()
        except Exception as exc:
            self.state.connected = False
            self.state.error = str(exc)
            if self.client:
                await self.client.disconnect()
            raise
        finally:
            await self.notify_change()

    def _disconnected(self, _client: BleakClient) -> None:
        self.state.connected = False
        asyncio.create_task(self.notify_change())

    def _notification(self, _sender, data: bytearray) -> None:
        parsed = parse_notification(bytes(data))
        if isinstance(parsed, ChannelReport):
            self.state.channels[parsed.channel] = {
                "electrode_state": parsed.electrode_state,
                "enabled": parsed.enabled,
                "strength": parsed.strength,
                "mode": parsed.mode,
            }
            self._clear_protocol_error()
        elif isinstance(parsed, dict):
            if "battery" in parsed:
                self.state.battery = parsed["battery"]
                self._clear_protocol_error()
            elif "error" in parsed:
                code = parsed["error"]
                labels = {
                    1: "校验码错误",
                    2: "包头错误",
                    3: "命令错误",
                    4: "数据错误",
                    5: "设备暂未实现该命令",
                }
                detail = labels.get(code, "未知错误")
                if code == 4 and self.state.generation == 1:
                    self._handle_generation1_data_error()
                else:
                    self.state.error = (
                        f"设备拒绝了上一条控制数据（错误码 {code}：{detail}）。"
                        "请断开设备后重新连接"
                    )
        asyncio.create_task(self.notify_change())

    def _clear_protocol_error(self) -> None:
        self._data_error_times.clear()
        if self.state.error.startswith("设备连续拒绝控制数据"):
            self.state.error = ""

    def _handle_generation1_data_error(self) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        self._data_error_times = [
            timestamp
            for timestamp in self._data_error_times
            if now - timestamp <= GENERATION1_ERROR_WINDOW
        ]
        self._data_error_times.append(now)
        if len(self._data_error_times) >= GENERATION1_ERROR_THRESHOLD:
            self.state.error = (
                "设备连续拒绝控制数据（错误码 4：数据错误），自动重试未能恢复。"
                "请断开设备后重新连接"
            )
            return
        if self._recovery_task is None or self._recovery_task.done():
            self._recovery_task = loop.create_task(self._recover_generation1_data_error())

    async def _recover_generation1_data_error(self) -> None:
        try:
            await asyncio.sleep(GENERATION1_RETRY_DELAY)
            if (
                not self.client
                or not self.state.connected
                or self._last_control_packet is None
            ):
                return
            # A realtime waveform naturally sends a fresh point every 100 ms.
            # Replaying an older rejected point would distort its timing.
            if self._last_control_packet[6] == 0x11:
                return
            async with self._write_lock:
                await self._write_generation1_packet(
                    self._last_control_packet,
                    remember=False,
                )
        except (asyncio.CancelledError, Exception):
            return

    async def _write_generation1_packet(
        self,
        packet: bytes,
        *,
        remember: bool,
    ) -> None:
        if not self.client:
            return
        now = asyncio.get_running_loop().time()
        delay = GENERATION1_MIN_WRITE_INTERVAL - (now - self._last_write_at)
        if delay > 0:
            await asyncio.sleep(delay)
        await self.client.write_gatt_char(WRITE_UUID, packet, response=False)
        self._last_write_at = asyncio.get_running_loop().time()
        if remember:
            self._last_control_packet = packet

    async def _query_state(self) -> None:
        if not self.client:
            return
        for query_type in (1, 2, 4):
            if self.state.generation == 1:
                async with self._write_lock:
                    await self._write_generation1_packet(
                        query(query_type),
                        remember=False,
                    )
            else:
                await self.client.write_gatt_char(
                    WRITE_UUID,
                    query(query_type),
                    response=False,
                )
                await asyncio.sleep(0.05)

    async def write_output(self, channel: str, output: ChannelOutput) -> None:
        if not self.client or not self.state.connected:
            return
        self.outputs[channel] = output
        async with self._write_lock:
            if self.state.generation == 1:
                packet = self._generation1_packet(channel, output)
                await self._write_generation1_packet(packet, remember=True)
                return
            else:
                a, b = self.outputs["A"], self.outputs["B"]
                active = [item for item in (a, b) if item.strength > 0]
                if active and all(item.mode != 0x11 for item in active):
                    packet = generation2_fixed(
                        round(a.strength),
                        a.mode if a.mode != 0x11 else 1,
                        round(b.strength),
                        b.mode if b.mode != 0x11 else 1,
                    )
                else:
                    packet = generation2_realtime(
                        round(a.strength),
                        a.frequency,
                        a.pulse_width,
                        round(b.strength),
                        b.frequency,
                        b.pulse_width,
                    )
            await self.client.write_gatt_char(WRITE_UUID, packet, response=False)

    def _generation1_packet(
        self,
        channel: str,
        output: ChannelOutput,
    ) -> bytes:
        a, b = self.outputs["A"], self.outputs["B"]
        same_strength = round(a.strength) == round(b.strength)
        same_mode = a.mode == b.mode
        same_realtime = (
            a.mode != 0x11
            or (
                a.frequency == b.frequency
                and a.pulse_width == b.pulse_width
            )
        )
        if same_strength and same_mode and same_realtime:
            return generation1_control(
                "AB",
                round(a.strength),
                a.frequency,
                a.pulse_width,
                a.mode,
            )
        return generation1_control(
            channel,
            round(output.strength),
            output.frequency,
            output.pulse_width,
            output.mode,
        )

    async def disconnect(self) -> None:
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
        if self.client:
            try:
                if self.state.generation == 1:
                    try:
                        async with self._write_lock:
                            await asyncio.wait_for(
                                self._write_generation1_packet(
                                    generation1_control("AB", 0, 1, 0),
                                    remember=False,
                                ),
                                timeout=0.75,
                            )
                    except Exception:
                        pass
                else:
                    try:
                        await asyncio.wait_for(
                            self.client.write_gatt_char(
                                WRITE_UUID,
                                generation2_realtime(0, 1, 0, 0, 1, 0),
                                response=False,
                            ),
                            timeout=0.75,
                        )
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(
                        self.client.stop_notify(NOTIFY_UUID), timeout=1
                    )
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(self.client.disconnect(), timeout=4)
                except Exception:
                    pass
            finally:
                self.state.connected = False
                self.client = None
                await self.notify_change()


class DeviceManager:
    def __init__(
        self,
        generations: dict[str, int] | None = None,
        notify_change: DeviceCallback | None = None,
    ) -> None:
        self.generations = generations or {}
        self.notify_change = notify_change or self._noop
        self.devices: dict[str, DeviceConnection] = {}
        self.scanning = False

    async def _noop(self) -> None:
        return None

    async def scan(self, timeout: float = 5) -> list[dict]:
        self.scanning = True
        await self.notify_change()
        try:
            discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)
            for address, (device, advertisement) in discovered.items():
                uuids = {value.lower() for value in (advertisement.service_uuids or [])}
                if SERVICE_UUID not in uuids:
                    continue
                if address not in self.devices:
                    generation = self.generations.get(address, 2)
                    state = DeviceState(address, device.name or "EMS Device", generation)
                    self.devices[address] = DeviceConnection(state, self.notify_change)
            return self.snapshot()
        finally:
            self.scanning = False
            await self.notify_change()

    async def connect(self, device_id: str) -> None:
        await self.devices[device_id].connect()

    async def disconnect(self, device_id: str) -> None:
        await self.devices[device_id].disconnect()

    async def set_generation(self, device_id: str, generation: int) -> None:
        if generation not in (1, 2):
            raise ValueError("generation must be 1 or 2")
        connection = self.devices[device_id]
        connection.state.generation = generation
        connection.state.error = ""
        self.generations[device_id] = generation
        await self.notify_change()

    async def write_output(self, device_id: str, channel: str, output: ChannelOutput) -> None:
        connection = self.devices.get(device_id)
        if connection:
            await connection.write_output(channel, output)

    async def close(self) -> None:
        await asyncio.gather(
            *(
                asyncio.wait_for(device.disconnect(), timeout=6)
                for device in self.devices.values()
                if device.state.connected
            ),
            return_exceptions=True,
        )

    def snapshot(self) -> list[dict]:
        result = []
        for connection in self.devices.values():
            raw = asdict(connection.state)
            raw["outputs"] = {
                channel: asdict(output) for channel, output in connection.outputs.items()
            }
            result.append(raw)
        return result
