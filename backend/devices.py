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
    generation2_realtime,
    parse_notification,
    query,
)
from .scheduler import ChannelOutput


DeviceCallback = Callable[[], Awaitable[None]]


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
        elif isinstance(parsed, dict):
            if "battery" in parsed:
                self.state.battery = parsed["battery"]
            elif "error" in parsed:
                self.state.error = f"设备异常代码 {parsed['error']}"
        asyncio.create_task(self.notify_change())

    async def _query_state(self) -> None:
        if not self.client:
            return
        for query_type in (1, 2, 4):
            await self.client.write_gatt_char(WRITE_UUID, query(query_type), response=False)
            await asyncio.sleep(0.05)

    async def write_output(self, channel: str, output: ChannelOutput) -> None:
        if not self.client or not self.state.connected:
            return
        self.outputs[channel] = output
        async with self._write_lock:
            if self.state.generation == 1:
                packet = generation1_control(
                    channel,
                    round(output.strength),
                    output.frequency,
                    output.pulse_width,
                )
            else:
                a, b = self.outputs["A"], self.outputs["B"]
                packet = generation2_realtime(
                    round(a.strength),
                    a.frequency,
                    a.pulse_width,
                    round(b.strength),
                    b.frequency,
                    b.pulse_width,
                )
            await self.client.write_gatt_char(WRITE_UUID, packet, response=False)

    async def disconnect(self) -> None:
        if self.client:
            try:
                if self.state.generation == 1:
                    for channel in ("A", "B"):
                        try:
                            await asyncio.wait_for(
                                self.client.write_gatt_char(
                                    WRITE_UUID,
                                    generation1_control(channel, 0, 1, 0),
                                    response=False,
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
