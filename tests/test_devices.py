import asyncio
import unittest

from backend.devices import DeviceConnection, DeviceState
from backend.scheduler import ChannelOutput


class FakeClient:
    def __init__(self):
        self.writes = []
        self.notify_stopped = False
        self.disconnected = False

    async def write_gatt_char(self, uuid, packet, response=False):
        self.writes.append((uuid, packet, response))

    async def stop_notify(self, uuid):
        self.notify_stopped = True

    async def disconnect(self):
        self.disconnected = True


class DeviceDisconnectTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_generation1_builtin_uses_fixed_mode_packet(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client

        await connection.write_output(
            "A",
            ChannelOutput(strength=40, frequency=80, pulse_width=60, mode=5),
        )

        packet = client.writes[-1][1]
        self.assertEqual(packet[6:9], bytes([5, 0, 0]))

    async def test_generation1_matching_channels_use_ab_sync_packet(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        output = ChannelOutput(strength=40, mode=5)

        await connection.write_output("A", output)
        await connection.write_output("B", output)

        packet = client.writes[-1][1]
        self.assertEqual(packet[2], 3)
        self.assertEqual(packet[4:7], bytes([0, 40, 5]))

    async def test_generation1_writes_are_spaced_for_device_firmware(self):
        async def changed():
            return None

        connection = DeviceConnection(DeviceState("id", "name", 1, connected=True), changed)
        client = FakeClient()
        connection.client = client
        started = asyncio.get_running_loop().time()

        await connection.write_output("A", ChannelOutput(strength=20, mode=1))
        await connection.write_output("B", ChannelOutput(strength=30, mode=2))

        elapsed = asyncio.get_running_loop().time() - started
        self.assertGreaterEqual(elapsed, 0.09)

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
        error_report = bytearray([0x35, 0x71, 0x55, 0x04])
        from backend.protocol import checksum

        error_report.append(checksum(error_report))
        for _ in range(3):
            connection._notification(None, error_report)
        await asyncio.sleep(0)

        self.assertIn("连续拒绝控制数据", connection.state.error)


if __name__ == "__main__":
    unittest.main()
