import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

from backend.devices import DeviceConnection, DeviceState
from backend.scheduler import ChannelOutput


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.writes = []
        self.notify_stopped = False
        self.disconnected = False
        self.connected = False
        self.notify_started = False
        self.write_times = []

    async def connect(self):
        self.connected = True

    async def start_notify(self, uuid, callback):
        self.notify_started = True

    async def write_gatt_char(self, uuid, packet, response=False):
        self.writes.append((uuid, packet, response))
        self.write_times.append(time.monotonic())

    async def stop_notify(self, uuid):
        self.notify_stopped = True

    async def disconnect(self):
        self.disconnected = True


class DeviceDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_rediscovers_device_before_creating_client(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("device-id", "old-name", 1), changed)
        discovered = type(
            "DiscoveredDevice",
            (),
            {"name": "fresh-name", "address": "device-id"},
        )()

        with (
            patch(
                "backend.devices.BleakScanner.find_device_by_address",
                new=AsyncMock(return_value=discovered),
            ) as find_device,
            patch("backend.devices.BleakClient", FakeClient),
        ):
            await connection.connect()

        find_device.assert_awaited_once()
        self.assertTrue(connection.state.connected)
        self.assertEqual(connection.state.name, "fresh-name")
        self.assertIs(connection.ble_device, discovered)
        self.assertTrue(connection.client.connected)
        self.assertTrue(connection.client.notify_started)

    async def test_connect_reports_when_device_is_not_advertising(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("device-id", "name", 1), changed)
        with patch(
            "backend.devices.BleakScanner.find_device_by_address",
            new=AsyncMock(return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "未发现设备"):
                await connection.connect()

        self.assertFalse(connection.state.connected)
        self.assertIn("重新扫描", connection.state.error)
        self.assertIsNone(connection.client)

    async def test_generation1_disconnect_uses_single_ab_close_packet(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.disconnect()

        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][1][2:9], bytes([3, 0, 0, 0, 0, 1, 0]))
        self.assertTrue(client.disconnected)

    async def test_generation2_disconnect_sends_zero_and_closes(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 2, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.disconnect()

        self.assertEqual(len(client.writes), 1)
        self.assertEqual(client.writes[0][1][3:5], bytes([0, 0]))
        self.assertEqual(client.writes[0][1][7:9], bytes([0, 0]))
        self.assertTrue(client.notify_stopped)
        self.assertTrue(client.disconnected)
        self.assertFalse(connection.state.connected)

    async def test_generation1_builtin_uses_selected_channel(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.write_output(
            "A",
            ChannelOutput(strength=40, frequency=80, pulse_width=60, mode=5),
        )
        await asyncio.sleep(0.02)

        packet = client.writes[-1][1]
        self.assertEqual(packet[2], 1)
        self.assertEqual(packet[6:9], bytes([5, 0, 0]))

    async def test_generation1_keeps_a_and_b_independent(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        await connection.write_output("A", ChannelOutput(strength=40, mode=5))
        await connection.write_output("B", ChannelOutput(strength=30, mode=2))
        await asyncio.sleep(0.15)

        self.assertEqual(client.writes[-2][1][2], 1)
        self.assertEqual(client.writes[-2][1][4:7], bytes([0, 40, 5]))
        self.assertEqual(client.writes[-1][1][2], 2)
        self.assertEqual(client.writes[-1][1][4:7], bytes([0, 30, 2]))

    async def test_generation1_writes_are_spaced_for_device_firmware(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        started = asyncio.get_running_loop().time()

        await connection.write_output("A", ChannelOutput(strength=20, mode=1))
        await connection.write_output("B", ChannelOutput(strength=30, mode=2))
        await asyncio.sleep(0.15)

        self.assertEqual(len(client.write_times), 2)
        self.assertGreaterEqual(
            client.write_times[1] - client.write_times[0],
            0.11,
        )

    async def test_generation1_channel_can_stop_independently(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.write_output("A", ChannelOutput(strength=20, mode=1))
        await connection.write_output("B", ChannelOutput(strength=30, mode=2))
        await connection.write_output("B", ChannelOutput())
        await asyncio.sleep(0.15)

        packet = client.writes[-1][1]
        self.assertEqual(packet[2], 2)
        self.assertEqual(packet[3], 0)
        self.assertEqual(connection.outputs["A"].strength, 20)

    async def test_generation1_custom_keeps_strength_and_sends_wave_point(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.write_output(
            "A",
            ChannelOutput(strength=20, frequency=99, pulse_width=25),
        )
        await asyncio.sleep(0.02)

        packet = client.writes[-1][1]
        self.assertEqual(packet[3:9], bytes([1, 0, 20, 0x11, 99, 25]))

    async def test_generation1_custom_zero_pulse_keeps_strength(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.write_output(
            "A",
            ChannelOutput(strength=20, frequency=80, pulse_width=0),
        )
        await asyncio.sleep(0.02)

        packet = client.writes[-1][1]
        self.assertEqual(packet[2:9], bytes([1, 1, 0, 20, 0x11, 80, 0]))

    async def test_generation1_wave_points_do_not_change_strength(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.write_output(
            "A",
            ChannelOutput(strength=40, frequency=20, pulse_width=10),
        )
        await asyncio.sleep(0.03)
        await connection.write_output(
            "A",
            ChannelOutput(strength=40, frequency=90, pulse_width=100),
        )
        await asyncio.sleep(0.13)

        self.assertEqual(
            [(write[1][4] << 8) | write[1][5] for write in client.writes],
            [40, 40],
        )
        self.assertEqual(
            [tuple(write[1][7:9]) for write in client.writes],
            [(20, 10), (90, 100)],
        )

    async def test_generation1_matching_channels_use_one_ab_packet(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        output = ChannelOutput(
            strength=20,
            frequency=71,
            pulse_width=99,
            mode=0x11,
        )

        await connection.write_output("A", output)
        await connection.write_output("B", output)
        await asyncio.sleep(0.03)

        self.assertEqual(len(client.writes), 1)
        self.assertEqual(
            client.writes[0][1][2:9],
            bytes([3, 1, 0, 20, 0x11, 71, 99]),
        )

    async def test_generation1_sync_state_can_split_and_resync(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        shared = ChannelOutput(
            strength=20,
            frequency=70,
            pulse_width=100,
            mode=0x11,
        )

        await connection.write_output("A", shared)
        await connection.write_output("B", shared)
        await asyncio.sleep(0.03)
        await connection.write_output(
            "B",
            ChannelOutput(
                strength=10,
                frequency=40,
                pulse_width=100,
                mode=0x11,
            ),
        )
        await asyncio.sleep(0.13)
        await connection.write_output("B", shared)
        await asyncio.sleep(0.13)

        self.assertEqual([write[1][2] for write in client.writes], [3, 2, 3])

    async def test_generation1_deduplicates_identical_channel_packets(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        output = ChannelOutput(strength=20, frequency=40, pulse_width=30)

        await connection.write_output("A", output)
        await connection.write_output("A", output)
        await asyncio.sleep(0.02)

        self.assertEqual(len(client.writes), 1)

    async def test_generation1_coalesces_old_points_per_channel(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.write_output(
            "A",
            ChannelOutput(strength=20, frequency=20, pulse_width=20),
        )
        await asyncio.sleep(0.02)
        await connection.write_output(
            "B",
            ChannelOutput(strength=10, frequency=30, pulse_width=30),
        )
        await connection.write_output(
            "B",
            ChannelOutput(strength=10, frequency=40, pulse_width=40),
        )
        await connection.write_output(
            "B",
            ChannelOutput(strength=10, frequency=50, pulse_width=50),
        )
        await asyncio.sleep(0.14)

        self.assertEqual(len(client.writes), 2)
        self.assertEqual(client.writes[-1][1][2], 2)
        self.assertEqual(client.writes[-1][1][4:9], bytes([0, 10, 0x11, 50, 50]))

    async def test_generation2_builtins_use_fixed_mode_packet(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 2, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.write_output("A", ChannelOutput(strength=20, mode=1))
        await connection.write_output("B", ChannelOutput(strength=30, mode=12))

        packet = client.writes[-1][1]
        self.assertEqual(packet[2], 0x01)
        self.assertEqual(packet[3:6], bytes([0, 20, 1]))
        self.assertEqual(packet[6:9], bytes([0, 30, 12]))

    async def test_single_error_code_four_is_retried_without_user_warning(self):
        changed_count = 0

        async def changed():
            nonlocal changed_count
            changed_count += 1

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        await connection.write_output("A", ChannelOutput(strength=20, mode=1))
        await asyncio.sleep(0.02)
        error_report = bytearray([0x35, 0x71, 0x55, 0x04])
        from backend.protocol import checksum

        error_report.append(checksum(error_report))
        connection._notification(None, error_report)
        await asyncio.sleep(0.25)
        self.assertEqual(connection.state.error, "")
        self.assertEqual(len(client.writes), 2)

        report = bytearray([0x35, 0x71, 1, 1, 1, 0, 20, 0x01])
        report.append(checksum(report))
        connection._notification(None, report)
        await asyncio.sleep(0)
        self.assertEqual(connection.state.error, "")
        self.assertGreaterEqual(changed_count, 2)

    async def test_realtime_error_does_not_replay_stale_waveform_point(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        await connection.write_output(
            "A",
            ChannelOutput(strength=20, frequency=80, pulse_width=50, mode=0x11),
        )
        await asyncio.sleep(0.02)
        error_report = bytearray([0x35, 0x71, 0x55, 0x04])
        from backend.protocol import checksum

        error_report.append(checksum(error_report))
        connection._notification(None, error_report)
        await asyncio.sleep(0.25)

        self.assertEqual(len(client.writes), 1)
        self.assertEqual(connection.state.error, "")

    async def test_repeated_error_code_four_displays_persistent_warning(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        await connection.write_output(
            "A",
            ChannelOutput(strength=20, frequency=100, pulse_width=100, mode=0x11),
        )
        await asyncio.sleep(0.02)
        error_report = bytearray([0x35, 0x71, 0x55, 0x04])
        from backend.protocol import checksum

        error_report.append(checksum(error_report))
        for _ in range(3):
            connection._notification(None, error_report)
        await asyncio.sleep(0)

        self.assertIn("连续拒绝控制数据", connection.state.error)
        self.assertIn("100Hz/100us", connection.state.error)


if __name__ == "__main__":
    unittest.main()
