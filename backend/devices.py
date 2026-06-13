from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice

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
# The protocol says 100 ms is the fastest update rate. A small guard interval
# keeps Windows BLE scheduling jitter from landing on the firmware boundary.
GENERATION1_MIN_WRITE_INTERVAL = 0.12
GENERATION1_COALESCE_WINDOW = 0.01
GENERATION1_ERROR_WINDOW = 5.0
GENERATION1_ERROR_THRESHOLD = 3
GENERATION1_RETRY_DELAY = 0.2
CONNECT_DISCOVERY_TIMEOUT = 8.0


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
    def __init__(
        self,
        state: DeviceState,
        notify_change: DeviceCallback,
        ble_device: BLEDevice | None = None,
    ) -> None:
        self.state = state
        self.notify_change = notify_change
        self.ble_device = ble_device
        self.client: BleakClient | None = None
        self.outputs = {"A": ChannelOutput(), "B": ChannelOutput()}
        self._write_lock = asyncio.Lock()
        self._last_write_at = 0.0
        self._last_control_packet: bytes | None = None
        self._last_generation1_payloads: dict[str, bytes] = {}
        self._pending_generation1_packets: dict[str, bytes] = {}
        self._generation1_writer_task: asyncio.Task | None = None
        self._generation1_next_channel = "A"
        self._data_error_times: list[float] = []
        self._recovery_task: asyncio.Task | None = None

    async def connect(self) -> None:
        self.state.error = ""
        try:
            discovered = await BleakScanner.find_device_by_address(
                self.state.id,
                timeout=CONNECT_DISCOVERY_TIMEOUT,
            )
            if discovered is None:
                raise RuntimeError(
                    "未发现设备。请确认设备已开机且未连接其他程序，然后重新扫描。"
                )
            self.ble_device = discovered
            self.state.name = discovered.name or self.state.name
            self.client = BleakClient(
                discovered,
                disconnected_callback=self._disconnected,
            )
            await self.client.connect()
            await self.client.start_notify(NOTIFY_UUID, self._notification)
            self.state.connected = True
            self.state.error = ""
            await self._query_state()
        except Exception as exc:
            self.state.connected = False
            message = str(exc)
            if "was not found" in message.lower():
                message = (
                    "未发现设备。请确认设备已开机且未连接其他程序，然后重新扫描。"
                )
            self.state.error = message
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception:
                    pass
            self.client = None
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
            packet_detail = self._describe_generation1_packet(
                self._last_control_packet
            )
            self.state.error = (
                "设备连续拒绝控制数据（错误码 4：数据错误），自动重试未能恢复。"
                f"最近发送：{packet_detail}。请断开设备后重新连接"
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

    @staticmethod
    def _describe_generation1_packet(packet: bytes | None) -> str:
        if packet is None or len(packet) < 9 or packet[1] != 0x11:
            return "未知报文"
        channel = {1: "A", 2: "B", 3: "AB"}.get(packet[2], str(packet[2]))
        strength = (packet[4] << 8) | packet[5]
        if packet[3] == 0:
            return f"{channel}通道关闭"
        if packet[6] == 0x11:
            return (
                f"{channel}通道自定义模式，强度{strength}，"
                f"{packet[7]}Hz/{packet[8]}us"
            )
        return f"{channel}通道固定模式{packet[6]}，强度{strength}"

    async def _write_generation1_packet(
        self,
        packet: bytes,
        *,
        remember: bool,
    ) -> None:
        if not self.client:
            return
        if remember and packet == self._last_control_packet:
            return
        now = asyncio.get_running_loop().time()
        delay = GENERATION1_MIN_WRITE_INTERVAL - (now - self._last_write_at)
        if delay > 0:
            await asyncio.sleep(delay)
        await self.client.write_gatt_char(WRITE_UUID, packet, response=False)
        self._last_write_at = asyncio.get_running_loop().time()
        if remember:
            self._last_control_packet = packet
            channel = {1: "A", 2: "B", 3: "AB"}.get(packet[2])
            payload = packet[3:9]
            if channel == "AB":
                self._last_generation1_payloads["A"] = payload
                self._last_generation1_payloads["B"] = payload
            elif channel:
                self._last_generation1_payloads[channel] = payload

    def _queue_generation1_packet(self, channel: str, packet: bytes) -> None:
        payload = packet[3:9]
        already_applied = (
            self._last_generation1_payloads.get("A") == payload
            and self._last_generation1_payloads.get("B") == payload
            if channel == "AB"
            else self._last_generation1_payloads.get(channel) == payload
        )
        if already_applied:
            self._pending_generation1_packets.pop(channel, None)
            return
        self._pending_generation1_packets[channel] = packet
        if (
            self._generation1_writer_task is None
            or self._generation1_writer_task.done()
        ):
            self._generation1_writer_task = asyncio.create_task(
                self._run_generation1_writer()
            )

    def _next_generation1_packet(self) -> tuple[str, bytes] | None:
        for channel in (
            self._generation1_next_channel,
            "B" if self._generation1_next_channel == "A" else "A",
            "AB",
        ):
            packet = self._pending_generation1_packets.pop(channel, None)
            if packet is not None:
                if channel in ("A", "B"):
                    self._generation1_next_channel = (
                        "B" if channel == "A" else "A"
                    )
                return channel, packet
        return None

    async def _run_generation1_writer(self) -> None:
        try:
            # A and B schedulers tick independently. Give both callbacks one
            # event-loop window so matching states can become one AB packet.
            await asyncio.sleep(GENERATION1_COALESCE_WINDOW)
            while self.client and self.state.connected:
                pending = self._next_generation1_packet()
                if pending is None:
                    return
                channel, packet = pending
                async with self._write_lock:
                    await self._write_generation1_packet(
                        packet,
                        remember=True,
                    )
                # If a newer state arrived while this packet was waiting, keep
                # it pending. Otherwise the last sent packet is authoritative.
                if self._pending_generation1_packets.get(channel) == packet:
                    self._pending_generation1_packets.pop(channel, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state.error = str(exc)
            await self.notify_change()

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
        if self.state.generation == 1:
            a_packet = self._generation1_packet("A", self.outputs["A"])
            b_packet = self._generation1_packet("B", self.outputs["B"])
            if a_packet[3] == 1 and a_packet[3:9] == b_packet[3:9]:
                self._pending_generation1_packets.pop("A", None)
                self._pending_generation1_packets.pop("B", None)
                self._queue_generation1_packet(
                    "AB",
                    generation1_control(
                        "AB",
                        (a_packet[4] << 8) | a_packet[5],
                        a_packet[7],
                        a_packet[8],
                        a_packet[6],
                    ),
                )
            else:
                self._pending_generation1_packets.pop("AB", None)
                packet = a_packet if channel == "A" else b_packet
                self._queue_generation1_packet(channel, packet)
            return
        async with self._write_lock:
            if self.state.generation == 1:
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
        if (
            self._generation1_writer_task
            and not self._generation1_writer_task.done()
        ):
            self._generation1_writer_task.cancel()
            await asyncio.gather(
                self._generation1_writer_task,
                return_exceptions=True,
            )
        self._pending_generation1_packets.clear()
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
                    self.devices[address] = DeviceConnection(
                        state,
                        self.notify_change,
                        device,
                    )
                else:
                    connection = self.devices[address]
                    connection.ble_device = device
                    connection.state.name = device.name or connection.state.name
                    if not connection.state.connected:
                        connection.state.error = ""
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
