import unittest

from backend.bilibili import normalize_message


class BilibiliTests(unittest.TestCase):
    def test_gift_battery_and_tier(self):
        event = normalize_message(
            {
                "cmd": "SEND_GIFT",
                "data": {
                    "uname": "tester",
                    "giftName": "辣条",
                    "total_coin": 10000,
                    "guard_level": 2,
                },
            }
        )
        self.assertEqual(event.value, 10)
        self.assertEqual(event.tier, "admiral")

    def test_guard_mapping(self):
        event = normalize_message(
            {"cmd": "GUARD_BUY", "data": {"username": "u", "guard_level": 1}}
        )
        self.assertEqual(event.event_type, "guard_governor")

    def test_interact_enter_follow_and_share(self):
        for msg_type, expected in ((1, "enter"), (2, "follow"), (3, "share")):
            event = normalize_message(
                {
                    "cmd": "INTERACT_WORD_V2",
                    "data": {
                        "pb_decoded": {
                            "uname": "tester",
                            "msg_type": msg_type,
                            "fans_medal_info": {"guard_level": 3},
                        }
                    },
                }
            )
            self.assertEqual(event.event_type, expected)
            self.assertEqual(event.tier, "captain")


if __name__ == "__main__":
    unittest.main()
