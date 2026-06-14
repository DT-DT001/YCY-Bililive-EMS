import asyncio
import unittest

from backend.bilibili import BilibiliListener, normalize_message


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

    def test_gift_tier_can_come_from_medal_info(self):
        event = normalize_message(
            {
                "cmd": "SEND_GIFT",
                "data": {
                    "uname": "tester",
                    "total_coin": 1000,
                    "medal_info": {"guard_level": 3},
                },
            }
        )
        self.assertEqual(event.tier, "captain")

    def test_like_uses_click_count_and_nested_guard_level(self):
        event = normalize_message(
            {
                "cmd": "LIKE_INFO_V3_CLICK",
                "data": {
                    "uname": "tester",
                    "click_count": 18,
                    "fans_medal": {"guard_level": 2},
                },
            }
        )
        self.assertEqual(event.value, 18)
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


class FakeLiveDanmaku:
    instances = []

    def __init__(self, room_id, max_retry, retry_after):
        self.room_id = room_id
        self.max_retry = max_retry
        self.retry_after = retry_after
        self.handlers = {}
        self.closed = asyncio.Event()
        self.disconnected = False
        self.err_reason = ""
        self.instances.append(self)

    def on(self, name):
        def decorator(handler):
            self.handlers[name] = handler
            return handler

        return decorator

    async def connect(self):
        await self.closed.wait()

    async def disconnect(self):
        self.disconnected = True
        self.closed.set()

    async def emit(self, name, data=None):
        await self.handlers[name](data or {})


class BilibiliReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeLiveDanmaku.instances = []
        self.statuses = []
        self.events = []

        async def on_event(event):
            self.events.append(event)

        async def on_status(status):
            self.statuses.append(status.copy())

        self.listener = BilibiliListener(
            on_event,
            on_status,
            client_factory=FakeLiveDanmaku,
            retry_base=0.01,
        )

    async def asyncTearDown(self):
        await self.listener.stop()

    async def wait_for_instances(self, count):
        for _ in range(100):
            if len(FakeLiveDanmaku.instances) >= count:
                return
            await asyncio.sleep(0.01)
        self.fail(f"expected {count} listener clients")

    async def test_timeout_discards_old_client_and_connects_with_a_fresh_one(self):
        await self.listener.start("1826512")
        await self.wait_for_instances(1)
        first = FakeLiveDanmaku.instances[0]
        await first.emit("VERIFICATION_SUCCESSFUL")
        self.assertTrue(self.listener.connected)

        await first.emit("TIMEOUT")
        await self.wait_for_instances(2)
        second = FakeLiveDanmaku.instances[1]

        self.assertTrue(first.disconnected)
        self.assertIs(self.listener._client, second)
        await second.emit("VERIFICATION_SUCCESSFUL")
        self.assertTrue(self.listener.connected)
        self.assertFalse(self.listener.connecting)
        self.assertEqual(self.listener.error, "")
        self.assertEqual(self.listener._retry_delay, 0.01)

    async def test_guard_purchase_and_toast_for_same_transaction_trigger_once(self):
        await self.listener.start("1826512")
        await self.wait_for_instances(1)
        client = FakeLiveDanmaku.instances[0]
        transaction = {
            "uid": 123,
            "username": "tester",
            "guard_level": 3,
            "num": 1,
            "start_time": 1000,
            "end_time": 2000,
        }

        await client.emit(
            "ALL",
            {"data": {"cmd": "GUARD_BUY", "data": transaction}},
        )
        await client.emit(
            "ALL",
            {"data": {"cmd": "USER_TOAST_MSG", "data": transaction}},
        )

        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0].event_type, "guard_captain")


if __name__ == "__main__":
    unittest.main()
