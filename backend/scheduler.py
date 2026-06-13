from __future__ import annotations

import asyncio
import random
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Awaitable, Callable

from .models import EventRule
from .waveforms import Waveform


@dataclass(slots=True)
class OutputTask:
    id: str
    event_id: str
    event_name: str
    strength: float
    remaining: float
    waveform: str
    created_at: float
    sequence: int
    waveforms: list[str] = field(default_factory=list)
    play_mode: str = "loop"
    strength_limit: float = 100
    duration_limit: float = 60


@dataclass(slots=True)
class ChannelOutput:
    strength: float = 0
    remaining: float = 0
    total_remaining: float = 0
    waveform: str = ""
    frequency: int = 1
    pulse_width: int = 0
    mode: int = 0x11
    event_name: str = ""
    queue_size: int = 0
    history: list[float] = field(default_factory=list)
    frequency_history: list[float] = field(default_factory=list)


OutputCallback = Callable[[str, str, ChannelOutput], Awaitable[None]]
HISTORY_SIZE = 120
TICK_SECONDS = 0.1


class ChannelScheduler:
    def __init__(
        self,
        device_id: str,
        channel: str,
        waveforms: dict[str, Waveform],
        callback: OutputCallback,
    ) -> None:
        self.device_id = device_id
        self.channel = channel
        self.waveforms = waveforms
        self.callback = callback
        self.tasks: list[OutputTask] = []
        self.output = ChannelOutput()
        self._sequence = 0
        self._wave_indices: dict[str, int] = {}
        self._random_bags: dict[str, list[str]] = {}
        self._last_random_wave: dict[str, str] = {}
        self._point_index = 0
        self._active_task_id: str | None = None
        self._last_tick = time.monotonic()
        self._runner: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    def _available_waveforms(self, names: list[str]) -> list[str]:
        available = [name for name in names if name in self.waveforms]
        if not available:
            available = [next(iter(self.waveforms))]
        return available

    def _select_waveform(
        self,
        rule: EventRule,
        current: str | None = None,
    ) -> str:
        available = self._available_waveforms(rule.waveforms)
        if rule.play_mode == "random":
            bag = [
                name
                for name in self._random_bags.get(rule.id, [])
                if name in available and name != current
            ]
            if not bag or any(name not in available for name in bag):
                bag = available.copy()
                random.shuffle(bag)
                last = current or self._last_random_wave.get(rule.id)
                if len(bag) > 1 and bag[0] == last:
                    swap_index = next(
                        index for index, name in enumerate(bag) if name != last
                    )
                    bag[0], bag[swap_index] = bag[swap_index], bag[0]
            if len(available) > 1:
                bag = [name for name in bag if name != current]
                if not bag:
                    bag = [name for name in available if name != current]
                    random.shuffle(bag)
            selected = bag.pop(0)
            self._random_bags[rule.id] = bag
            self._last_random_wave[rule.id] = selected
            return selected
        index = self._wave_indices.get(rule.id, 0)
        selected = available[index % len(available)]
        if rule.play_mode == "sequence":
            self._wave_indices[rule.id] = index + 1
        return selected

    def _advance_waveform(self, active: OutputTask) -> None:
        available = self._available_waveforms(active.waveforms or [active.waveform])
        if len(available) <= 1 or active.play_mode == "loop":
            return
        if active.play_mode == "random":
            rule = EventRule(
                id=active.event_id,
                name=active.event_name,
                event_type="waveform",
                waveforms=available,
                play_mode="random",
            )
            active.waveform = self._select_waveform(rule, active.waveform)
            return
        try:
            index = available.index(active.waveform)
        except ValueError:
            index = -1
        active.waveform = available[(index + 1) % len(available)]

    async def trigger(self, rule: EventRule, value: float) -> None:
        event_value = max(0.0, float(value))
        initial_strength, initial_duration, duration_increment = rule.calculate(
            event_value
        )
        strength_limit = max(0.0, float(rule.strength_limit))
        duration_limit = max(0.0, float(rule.duration_limit))
        strength_increment = event_value * max(0.0, float(rule.strength_rate))
        if initial_strength <= 0 or initial_duration <= 0:
            return
        async with self._lock:
            existing = next(
                (task for task in self.tasks if task.event_id == rule.id),
                None,
            )
            available_waveforms = self._available_waveforms(rule.waveforms)
            if existing:
                existing.strength = min(
                    strength_limit,
                    max(0.0, existing.strength) + strength_increment,
                )
                existing.remaining = min(
                    duration_limit,
                    max(0.0, existing.remaining) + max(0.0, duration_increment),
                )
                existing.event_name = rule.name
                existing.strength_limit = strength_limit
                existing.duration_limit = duration_limit
                existing.waveforms = available_waveforms
                existing.play_mode = rule.play_mode
                if existing.waveform not in available_waveforms:
                    existing.waveform = available_waveforms[0]
                    if existing.id == self._active_task_id:
                        self._point_index = 0
                self._sort()
                return
            selected_waveform = self._select_waveform(rule)
            self._sequence += 1
            self.tasks.append(
                OutputTask(
                    id=uuid.uuid4().hex,
                    event_id=rule.id,
                    event_name=rule.name,
                    strength=initial_strength,
                    remaining=initial_duration,
                    waveform=selected_waveform,
                    created_at=time.monotonic(),
                    sequence=self._sequence,
                    waveforms=available_waveforms,
                    play_mode=rule.play_mode,
                    strength_limit=strength_limit,
                    duration_limit=duration_limit,
                )
            )
            self._sort()
            if self._runner is None or self._runner.done():
                self._last_tick = time.monotonic()
                self._runner = asyncio.create_task(self._run())

    def _sort(self) -> None:
        self.tasks.sort(key=lambda task: (-task.strength, task.sequence))

    def _next_point(self, active: OutputTask):
        if active.id != self._active_task_id:
            self._active_task_id = active.id
            self._point_index = 0
        waveform = self.waveforms[active.waveform]
        point = waveform.points[self._point_index % len(waveform.points)]
        self._point_index += 1
        if self._point_index >= len(waveform.points):
            self._point_index = 0
            self._advance_waveform(active)
        return point

    async def _run(self) -> None:
        idle_ticks = 0
        while self.tasks or idle_ticks < HISTORY_SIZE:
            now = time.monotonic()
            elapsed = min(0.25, now - self._last_tick)
            self._last_tick = now
            async with self._lock:
                if not self.tasks:
                    idle_ticks += 1
                    self._point_index = 0
                    self._active_task_id = None
                    fixed_mode_ended = 1 <= self.output.mode <= 16
                    self.output = ChannelOutput(
                        history=(
                            [0] * HISTORY_SIZE
                            if fixed_mode_ended
                            else (self.output.history + [0])[-HISTORY_SIZE:]
                        ),
                        frequency_history=(
                            [0] * HISTORY_SIZE
                            if fixed_mode_ended
                            else (
                                self.output.frequency_history + [0]
                            )[-HISTORY_SIZE:]
                        ),
                    )
                else:
                    idle_ticks = 0
                    active = self.tasks[0]
                    active.remaining -= elapsed
                    if active.remaining <= 0:
                        self.tasks.pop(0)
                        self._point_index = 0
                        self._active_task_id = None
                        continue
                    waveform_name = active.waveform
                    point = self._next_point(active)
                    mode = self.waveforms[waveform_name].fixed_mode or 0x11
                    self.output = ChannelOutput(
                        strength=active.strength,
                        remaining=active.remaining,
                        total_remaining=sum(
                            max(0.0, task.remaining) for task in self.tasks
                        ),
                        waveform=waveform_name,
                        frequency=point.frequency,
                        pulse_width=point.pulse_width,
                        mode=mode,
                        event_name=active.event_name,
                        queue_size=len(self.tasks),
                        history=(self.output.history + [point.pulse_width])[-HISTORY_SIZE:],
                        frequency_history=(
                            self.output.frequency_history + [point.frequency]
                        )[-HISTORY_SIZE:],
                    )
            await self.callback(self.device_id, self.channel, self.output)
            await asyncio.sleep(TICK_SECONDS)
        self.output = ChannelOutput(history=[0] * HISTORY_SIZE)
        self._active_task_id = None
        self._point_index = 0
        await self.callback(self.device_id, self.channel, self.output)

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        if runner and not runner.done():
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        async with self._lock:
            self.tasks.clear()
            self._active_task_id = None
            self._point_index = 0
            self.output = ChannelOutput(
                history=(self.output.history + [0])[-HISTORY_SIZE:],
                frequency_history=(
                    self.output.frequency_history + [0]
                )[-HISTORY_SIZE:],
            )
        await self.callback(self.device_id, self.channel, self.output)
        self._last_tick = time.monotonic()
        self._runner = asyncio.create_task(self._run())

    async def close(self) -> None:
        async with self._lock:
            self.tasks.clear()
        if self._runner:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        self._active_task_id = None
        self._point_index = 0
        self.output = ChannelOutput(history=[0] * HISTORY_SIZE)
        await self.callback(self.device_id, self.channel, self.output)

    def snapshot(self) -> dict:
        return {
            "device_id": self.device_id,
            "channel": self.channel,
            "output": asdict(self.output),
            "tasks": [asdict(task) for task in self.tasks],
        }


class Scheduler:
    def __init__(self, waveforms: dict[str, Waveform], callback: OutputCallback) -> None:
        self.waveforms = waveforms
        self.callback = callback
        self.channels: dict[tuple[str, str], ChannelScheduler] = {}

    async def trigger(self, rule: EventRule, value: float) -> None:
        for target in rule.targets:
            key = (target.device_id, target.channel)
            channel = self.channels.get(key)
            if channel is None:
                channel = ChannelScheduler(*key, self.waveforms, self.callback)
                self.channels[key] = channel
            await channel.trigger(rule, value)

    async def stop_all(self) -> None:
        await asyncio.gather(*(channel.stop() for channel in self.channels.values()))

    async def close(self) -> None:
        await asyncio.gather(*(channel.close() for channel in self.channels.values()))

    def snapshot(self) -> list[dict]:
        return [channel.snapshot() for channel in self.channels.values()]
