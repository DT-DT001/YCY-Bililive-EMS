import unittest

from backend.bilibili import LiveEvent
from backend.app import Controller
from backend.models import AppConfig, ChannelTarget, EventRule


class ModelTests(unittest.TestCase):
    def test_missing_rules_uses_defaults(self):
        config = AppConfig.from_dict({"room_id": "1"})
        self.assertEqual(len(config.rules), 35)

    def test_existing_config_receives_new_rules(self):
        config = AppConfig.from_dict(
            {
                "rules": [
                    {
                        "id": "gift:normal",
                        "name": "custom",
                        "event_type": "gift",
                        "base_strength": 88,
                    }
                ]
            }
        )
        gift = next(rule for rule in config.rules if rule.id == "gift:normal")
        self.assertEqual(gift.base_strength, 88)
        self.assertTrue(any(rule.id == "enter:normal" for rule in config.rules))
        self.assertFalse(next(rule for rule in config.rules if rule.id == "leave:normal").enabled)

    def test_calculation_respects_limits(self):
        rule = EventRule(
            id="gift",
            name="gift",
            event_type="gift",
            base_strength=20,
            base_duration=5,
            strength_rate=10,
            duration_rate=2,
            strength_limit=50,
            duration_limit=12,
        )
        strength, duration, increment = rule.calculate(10)
        self.assertEqual(strength, 50)
        self.assertEqual(duration, 12)
        self.assertEqual(increment, 7)

    def test_calculation_uses_event_value_for_both_increments(self):
        rule = EventRule(
            id="gift",
            name="gift",
            event_type="gift",
            base_strength=20,
            base_duration=5,
            strength_rate=0.1,
            duration_rate=0.2,
            strength_limit=100,
            duration_limit=60,
        )
        strength, duration, increment = rule.calculate(10)
        self.assertEqual(strength, 21)
        self.assertEqual(duration, 7)
        self.assertEqual(increment, 2)

    def test_keyword_and_tier_matching(self):
        controller = Controller()
        rule = EventRule(
            id="special",
            name="special",
            event_type="danmu",
            tier="captain",
            keyword="启动",
        )
        controller.config.rules = [rule]
        matching = controller.matching_rules(
            LiveEvent("danmu", "u", "captain", 1, "请启动")
        )
        self.assertEqual(matching, [rule])
        nonmatching = controller.matching_rules(
            LiveEvent("danmu", "u", "normal", 1, "请启动")
        )
        self.assertEqual(nonmatching, [])

    def test_export_omits_targets_and_import_preserves_current_targets(self):
        controller = Controller()
        rule = controller.config.rules[0]
        rule.targets = [ChannelTarget("device-a", "A")]
        exported = controller.export_event_config()
        self.assertNotIn("targets", exported["rules"][0])
        exported["rules"][0]["base_strength"] = 77
        controller.import_event_config(exported)
        imported = next(item for item in controller.config.rules if item.id == rule.id)
        self.assertEqual(imported.base_strength, 77)
        self.assertEqual(imported.targets[0].device_id, "device-a")

    def test_waveform_mode_is_normalized_without_losing_order(self):
        single = EventRule.from_dict(
            {
                "id": "single",
                "name": "single",
                "event_type": "gift",
                "waveforms": ["敲击"],
                "play_mode": "random",
            }
        )
        multiple = EventRule.from_dict(
            {
                "id": "multiple",
                "name": "multiple",
                "event_type": "gift",
                "waveforms": ["心跳", "潮汐", "连击"],
                "play_mode": "loop",
            }
        )
        self.assertEqual(single.play_mode, "loop")
        self.assertEqual(multiple.play_mode, "sequence")
        self.assertEqual(multiple.waveforms, ["心跳", "潮汐", "连击"])


if __name__ == "__main__":
    unittest.main()
