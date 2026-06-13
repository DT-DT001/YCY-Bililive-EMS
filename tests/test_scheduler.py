import asyncio
import unittest

from backend.models import ChannelTarget, EventRule
from backend.scheduler import ChannelScheduler, OutputTask
from backend.waveforms import WavePoint, Waveform, builtin_waveforms


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.outputs = []
        self.output_details = []

        async def callback(device, channel, output):
            self.outputs.append((device, channel, output.strength))
            self.output_details.append(
                (output.event_name, output.strength, output.remaining)
            )

        self.scheduler = ChannelScheduler("dev", "A", builtin_waveforms(), callback)

    async def asyncTearDown(self):
        await self.scheduler.close()

    def rule(self, event_id, strength, duration=1, rate=0.3):
        return EventRule(
            id=event_id,
            name=event_id,
            event_type="gift",
            base_strength=strength,
            base_duration=duration,
            duration_rate=rate,
            targets=[ChannelTarget("dev", "A")],
        )

    async def test_higher_strength_preempts_and_lower_is_preserved(self):
        await self.scheduler.trigger(self.rule("gift", 20), 0)
        await asyncio.sleep(0.12)
        before = self.scheduler.tasks[0].remaining
        await self.scheduler.trigger(self.rule("guard", 40), 0)
        self.assertEqual(self.scheduler.tasks[0].strength, 40)
        self.assertTrue(any(task.strength == 20 for task in self.scheduler.tasks))
        await asyncio.sleep(0.12)
        lower = next(task for task in self.scheduler.tasks if task.strength == 20)
        self.assertAlmostEqual(lower.remaining, before, delta=0.08)

    async def test_different_events_keep_independent_strength_limits(self):
        normal = self.rule("danmu:normal", 90, duration=1, rate=0.1)
        normal.strength_rate = 10
        normal.strength_limit = 100
        normal.duration_limit = 10
        captain = self.rule("danmu:captain", 110, duration=1, rate=0.2)
        captain.strength_rate = 10
        captain.strength_limit = 120
        captain.duration_limit = 20

        await self.scheduler.trigger(normal, 1)
        await self.scheduler.trigger(captain, 1)

        states = {task.event_id: task for task in self.scheduler.tasks}
        self.assertEqual(states[normal.id].strength, 100)
        self.assertEqual(states[normal.id].strength_limit, 100)
        self.assertEqual(states[captain.id].strength, 120)
        self.assertEqual(states[captain.id].strength_limit, 120)
        self.assertEqual(self.scheduler.tasks[0].event_id, captain.id)

        await self.scheduler.trigger(normal, 10)
        states = {task.event_id: task for task in self.scheduler.tasks}
        self.assertEqual(states[normal.id].strength, 100)
        self.assertEqual(states[captain.id].strength, 120)
        self.assertAlmostEqual(states[normal.id].remaining, 2.1, delta=0.05)
        self.assertAlmostEqual(states[captain.id].remaining, 1.2, delta=0.05)

    async def test_higher_event_finishes_then_lower_event_resumes_unchanged(self):
        normal = self.rule("danmu:normal", 100, duration=0.7, rate=0)
        normal.strength_limit = 100
        captain = self.rule("danmu:captain", 120, duration=0.2, rate=0)
        captain.strength_limit = 120

        await self.scheduler.trigger(normal, 0)
        await asyncio.sleep(0.12)
        normal_before = next(
            task for task in self.scheduler.tasks if task.event_id == normal.id
        ).remaining
        await self.scheduler.trigger(captain, 0)
        await asyncio.sleep(0.15)
        normal_during = next(
            task for task in self.scheduler.tasks if task.event_id == normal.id
        ).remaining
        self.assertAlmostEqual(normal_during, normal_before, delta=0.06)
        await asyncio.sleep(0.2)

        active = self.scheduler.tasks[0]
        self.assertEqual(active.event_id, normal.id)
        self.assertEqual(active.strength, 100)
        self.assertEqual(active.strength_limit, 100)
        active_outputs = [
            (name, strength)
            for name, strength, _ in self.output_details
            if strength > 0
        ]
        self.assertIn((captain.name, 120), active_outputs)
        self.assertEqual(active_outputs[-1], (normal.name, 100))

    async def test_each_event_uses_its_own_duration_limit(self):
        normal = self.rule("danmu:normal", 80, duration=5, rate=1)
        normal.duration_limit = 6
        captain = self.rule("danmu:captain", 120, duration=5, rate=1)
        captain.duration_limit = 20

        await self.scheduler.trigger(normal, 1)
        await self.scheduler.trigger(captain, 1)
        await self.scheduler.trigger(normal, 10)
        await self.scheduler.trigger(captain, 10)

        states = {task.event_id: task for task in self.scheduler.tasks}
        self.assertEqual(states[normal.id].remaining, 6)
        self.assertEqual(states[captain.id].remaining, 16)

    async def test_same_equal_event_merges_increment_into_remaining_time(self):
        rule = self.rule("gift", 20, duration=5, rate=0.3)
        await self.scheduler.trigger(rule, 10)
        await asyncio.sleep(0.12)
        before = self.scheduler.tasks[0].remaining
        await self.scheduler.trigger(rule, 10)
        self.assertEqual(len(self.scheduler.tasks), 1)
        self.assertAlmostEqual(
            self.scheduler.tasks[0].remaining, before + 3, delta=0.05
        )
        await asyncio.sleep(0.12)
        self.assertAlmostEqual(
            self.scheduler.output.total_remaining,
            self.scheduler.tasks[0].remaining,
            delta=0.12,
        )

    async def test_larger_event_value_increases_strength_and_preempts(self):
        rule = self.rule("gift", 20, duration=5, rate=0.3)
        rule.strength_rate = 1
        await self.scheduler.trigger(rule, 1)
        await self.scheduler.trigger(rule, 5)
        self.assertEqual(self.scheduler.tasks[0].strength, 26)
        self.assertEqual(len(self.scheduler.tasks), 1)

    async def test_repeated_events_use_each_event_value_and_cap_total_duration(self):
        rule = self.rule("gift", 20, duration=5, rate=0.2)
        rule.strength_rate = 0.1
        rule.duration_limit = 60
        for _ in range(30):
            await self.scheduler.trigger(rule, 10)
        same = [task for task in self.scheduler.tasks if task.event_id == rule.id]
        self.assertAlmostEqual(sum(task.remaining for task in same), 60)
        self.assertEqual(self.scheduler.tasks[0].strength, 50)

    async def test_strength_still_increases_when_duration_is_at_limit(self):
        rule = self.rule("gift", 20, duration=5, rate=1)
        rule.strength_rate = 1
        rule.duration_limit = 5
        await self.scheduler.trigger(rule, 1)
        await self.scheduler.trigger(rule, 5)
        self.assertAlmostEqual(sum(task.remaining for task in self.scheduler.tasks), 5)
        self.assertEqual(self.scheduler.tasks[0].strength, 26)

    async def test_equal_gifts_increment_strength_on_every_trigger(self):
        rule = self.rule("gift", 20, duration=5, rate=0.5)
        rule.strength_rate = 0.5
        await self.scheduler.trigger(rule, 10)
        await self.scheduler.trigger(rule, 10)
        await self.scheduler.trigger(rule, 10)
        self.assertEqual(self.scheduler.tasks[0].strength, 35)
        self.assertAlmostEqual(
            sum(task.remaining for task in self.scheduler.tasks), 20
        )

    async def test_repeated_event_adds_only_duration_increment_not_base(self):
        rule = self.rule("danmu", 20, duration=5, rate=0.1)
        rule.strength_rate = 1
        await self.scheduler.trigger(rule, 0)
        await self.scheduler.trigger(rule, 10)
        self.assertEqual(len(self.scheduler.tasks), 1)
        self.assertAlmostEqual(self.scheduler.tasks[0].remaining, 6, delta=0.05)

    async def test_same_event_does_not_fall_back_to_old_strength(self):
        rule = self.rule("gift", 20, duration=0.2, rate=0.1)
        rule.strength_rate = 10
        rule.strength_limit = 100
        for _ in range(8):
            await self.scheduler.trigger(rule, 1)
        self.assertEqual(len(self.scheduler.tasks), 1)
        self.assertEqual(self.scheduler.tasks[0].strength, 100)
        await asyncio.sleep(0.35)
        active_strengths = [strength for _, _, strength in self.outputs if strength]
        self.assertTrue(active_strengths)
        self.assertTrue(all(strength == 100 for strength in active_strengths))

    async def test_random_mode_changes_only_after_complete_waveform(self):
        rule = self.rule("random", 20)
        rule.waveforms = ["潮汐", "连击", "心跳", "呼吸"]
        rule.play_mode = "random"
        await self.scheduler.trigger(rule, 0)
        task = self.scheduler.tasks[0]
        first = task.waveform
        point_count = len(self.scheduler.waveforms[first].points)
        for _ in range(point_count - 1):
            self.scheduler._next_point(task)
            self.assertEqual(task.waveform, first)
        self.scheduler._next_point(task)
        self.assertNotEqual(task.waveform, first)

    async def test_random_mode_never_repeats_current_waveform(self):
        rule = self.rule("random", 20)
        rule.waveforms = ["潮汐", "连击", "心跳", "呼吸"]
        rule.play_mode = "random"
        await self.scheduler.trigger(rule, 0)
        task = self.scheduler.tasks[0]
        selected = [task.waveform]
        for _ in range(11):
            task.waveform = self.scheduler._select_waveform(rule, task.waveform)
            selected.append(task.waveform)
        self.assertTrue(
            all(left != right for left, right in zip(selected, selected[1:]))
        )

    async def test_waveform_restarts_when_active_task_changes(self):
        self.scheduler.waveforms["测试"] = Waveform(
            "测试",
            [WavePoint(10, 11), WavePoint(20, 22), WavePoint(30, 33)],
        )
        first = OutputTask("first", "a", "a", 10, 1, "测试", 0, 1)
        second = OutputTask("second", "b", "b", 20, 1, "测试", 0, 2)

        self.assertEqual(self.scheduler._next_point(first).pulse_width, 11)
        self.assertEqual(self.scheduler._next_point(first).pulse_width, 22)
        self.assertEqual(self.scheduler._next_point(second).pulse_width, 11)
        self.assertEqual(self.scheduler._next_point(first).pulse_width, 11)

    async def test_idle_output_scrolls_toward_flat_line(self):
        self.scheduler.output.history = [80] * 120
        await self.scheduler.stop()
        await asyncio.sleep(0.25)
        self.assertEqual(self.scheduler.output.strength, 0)
        self.assertGreaterEqual(self.scheduler.output.history.count(0), 2)
        self.assertEqual(self.scheduler.output.history[-1], 0)

    async def test_fixed_waveform_history_is_cleared_when_output_finishes(self):
        rule = self.rule("fixed", 20, duration=0.12, rate=0)
        rule.waveforms = ["潮汐"]

        await self.scheduler.trigger(rule, 0)
        await asyncio.sleep(0.15)
        self.assertEqual(self.scheduler.output.mode, 1)
        await asyncio.sleep(0.15)

        self.assertEqual(self.scheduler.output.strength, 0)
        self.assertTrue(all(value == 0 for value in self.scheduler.output.history))
        self.assertTrue(
            all(value == 0 for value in self.scheduler.output.frequency_history)
        )

    async def test_stop_cancels_active_output_and_clears_queue(self):
        await self.scheduler.trigger(self.rule("gift", 40, duration=5), 10)
        await asyncio.sleep(0.12)
        await self.scheduler.stop()
        await asyncio.sleep(0.02)
        self.assertEqual(self.scheduler.tasks, [])
        self.assertEqual(self.scheduler.output.strength, 0)
        self.assertEqual(self.outputs[-1][2], 0)

    async def test_sequence_mode_uses_selected_waveform_order(self):
        rule = self.rule("ordered", 20)
        rule.waveforms = ["心跳", "潮汐", "连击"]
        rule.play_mode = "sequence"
        await self.scheduler.trigger(rule, 0)
        task = self.scheduler.tasks[0]
        selected = [task.waveform]
        for _ in range(3):
            self.scheduler._advance_waveform(task)
            selected.append(task.waveform)
        self.assertEqual(selected, ["心跳", "潮汐", "连击", "心跳"])

    async def test_builtin_waveform_reports_protocol_fixed_mode(self):
        rule = self.rule("builtin", 20)
        rule.waveforms = ["潮汐"]
        await self.scheduler.trigger(rule, 0)
        await asyncio.sleep(0.12)
        self.assertEqual(self.scheduler.output.mode, 1)
        self.assertTrue(self.scheduler.output.frequency_history)
        self.assertEqual(
            self.scheduler.output.frequency_history[-1],
            self.scheduler.output.frequency,
        )

    async def test_imported_waveform_reports_realtime_mode(self):
        self.scheduler.waveforms["imported"] = Waveform(
            "imported",
            [WavePoint(20, 30)],
            source="json",
        )
        rule = self.rule("imported", 20)
        rule.waveforms = ["imported"]
        await self.scheduler.trigger(rule, 0)
        await asyncio.sleep(0.12)
        self.assertEqual(self.scheduler.output.mode, 0x11)


if __name__ == "__main__":
    unittest.main()
