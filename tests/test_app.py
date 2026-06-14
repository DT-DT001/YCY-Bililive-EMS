import unittest
from unittest.mock import AsyncMock

from backend.app import Controller
from backend.models import AppConfig
from backend.scheduler import ChannelOutput


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


class ControllerOutputTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_output_is_not_cached(self):
        controller = Controller.__new__(Controller)
        controller._last_device_outputs = {}
        controller.devices = type(
            "Devices",
            (),
            {
                "write_output": AsyncMock(return_value=False),
                "devices": {},
            },
        )()
        controller.broadcast = AsyncMock()

        await controller.apply_output(
            "device-1",
            "A",
            ChannelOutput(strength=20, mode=1),
        )

        self.assertNotIn(("device-1", "A"), controller._last_device_outputs)

    async def test_fixed_waveform_point_changes_reach_device_layer(self):
        controller = Controller.__new__(Controller)
        controller._last_device_outputs = {}
        write_output = AsyncMock(return_value=True)
        controller.devices = type(
            "Devices",
            (),
            {
                "write_output": write_output,
                "devices": {},
            },
        )()
        controller.broadcast = AsyncMock()

        await controller.apply_output(
            "device-1",
            "A",
            ChannelOutput(
                strength=20,
                mode=1,
                frequency=20,
                pulse_width=30,
            ),
        )
        await controller.apply_output(
            "device-1",
            "A",
            ChannelOutput(
                strength=20,
                mode=1,
                frequency=40,
                pulse_width=50,
            ),
        )

        self.assertEqual(write_output.await_count, 2)


class ControllerConfigTests(unittest.TestCase):
    def test_import_rejects_duplicate_rule_ids(self):
        controller = Controller.__new__(Controller)
        controller.config = AppConfig()
        controller.save = lambda: None

        with self.assertRaisesRegex(ValueError, "重复"):
            controller.import_event_config(
                {
                    "format": "ycy-bililive-event-config",
                    "rules": [
                        {
                            "id": "gift:normal",
                            "name": "one",
                            "event_type": "gift",
                        },
                        {
                            "id": "gift:normal",
                            "name": "two",
                            "event_type": "gift",
                        },
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
