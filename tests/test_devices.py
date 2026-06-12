import unittest

from backend.devices import DeviceConnection, DeviceState


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


if __name__ == "__main__":
    unittest.main()
