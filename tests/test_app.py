import unittest

from backend.app import Controller


class FakeDevices:
    def snapshot(self):
        return [
            {
                "id": "device-1",
                "outputs": {
                    "A": {"total_remaining": 26.0},
                    "B": {"total_remaining": 0.0},
                },
            }
        ]


class FakeScheduler:
    def snapshot(self):
        return [
            {
                "device_id": "device-1",
                "channel": "A",
                "output": {"total_remaining": 14.6},
                "tasks": [],
            }
        ]


class ControllerSnapshotTests(unittest.TestCase):
    def test_device_snapshot_uses_live_scheduler_output(self):
        controller = Controller.__new__(Controller)
        controller.devices = FakeDevices()
        controller.scheduler = FakeScheduler()

        devices = controller.device_snapshot()

        self.assertEqual(devices[0]["outputs"]["A"]["total_remaining"], 14.6)
        self.assertEqual(devices[0]["outputs"]["B"]["total_remaining"], 0.0)


if __name__ == "__main__":
    unittest.main()
