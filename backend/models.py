from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any


TIERS = ("normal", "captain", "admiral", "governor")
TIER_LABELS = dict(zip(TIERS, ("普通用户", "舰长", "提督", "总督")))
PLAY_MODES = ("loop", "sequence", "random")
TIERED_EVENT_TYPES = ("danmu", "like", "gift", "enter", "leave", "follow", "unfollow", "share")
UNAVAILABLE_EVENT_TYPES = ("leave", "unfollow")
MAX_DEVICE_STRENGTH = 276.0


def nonnegative_finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


@dataclass(slots=True)
class ChannelTarget:
    device_id: str
    channel: str


@dataclass(slots=True)
class EventRule:
    id: str
    name: str
    event_type: str
    tier: str = "normal"
    keyword: str = ""
    enabled: bool = True
    base_strength: float = 20
    base_duration: float = 5
    strength_rate: float = 0
    duration_rate: float = 0
    strength_limit: float = 100
    duration_limit: float = 60
    waveforms: list[str] = field(default_factory=lambda: ["潮汐"])
    play_mode: str = "loop"
    targets: list[ChannelTarget] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EventRule":
        data = dict(raw)
        data["targets"] = [
            ChannelTarget(str(target["device_id"]), str(target["channel"]))
            for target in data.get("targets", [])
            if isinstance(target, dict)
            and target.get("device_id")
            and target.get("channel") in ("A", "B")
        ]
        waveforms = list(data.get("waveforms") or ["潮汐"])
        data["waveforms"] = waveforms
        if len(waveforms) == 1:
            data["play_mode"] = "loop"
        elif data.get("play_mode") not in ("sequence", "random"):
            data["play_mode"] = "sequence"
        return cls(**data)

    def calculate(self, value: float) -> tuple[float, float, float]:
        event_value = nonnegative_finite(value)
        strength_limit = min(
            MAX_DEVICE_STRENGTH,
            nonnegative_finite(self.strength_limit),
        )
        duration_limit = nonnegative_finite(self.duration_limit)
        base_strength = min(
            MAX_DEVICE_STRENGTH,
            nonnegative_finite(self.base_strength),
        )
        base_duration = nonnegative_finite(self.base_duration)
        strength_increment = event_value * nonnegative_finite(self.strength_rate)
        requested_duration_increment = event_value * nonnegative_finite(
            self.duration_rate
        )

        strength = min(strength_limit, base_strength + strength_increment)
        duration = min(
            duration_limit, base_duration + requested_duration_increment
        )
        capped_base_duration = min(duration_limit, base_duration)
        effective_duration_increment = max(0.0, duration - capped_base_duration)
        return strength, duration, effective_duration_increment


def default_rules() -> list[EventRule]:
    labels = {
        "danmu": "弹幕",
        "like": "点赞",
        "gift": "礼物",
        "guard_captain": "上舰长",
        "guard_admiral": "上提督",
        "guard_governor": "上总督",
        "enter": "进入直播间",
        "leave": "离开直播间",
        "follow": "关注直播间",
        "unfollow": "取关直播间",
        "share": "分享直播间",
    }
    rules: list[EventRule] = []
    for event_type in TIERED_EVENT_TYPES:
        for tier in TIERS:
            rules.append(
                EventRule(
                    id=f"{event_type}:{tier}",
                    name=f"{labels[event_type]} · {TIER_LABELS[tier]}",
                    event_type=event_type,
                    tier=tier,
                    enabled=event_type not in UNAVAILABLE_EVENT_TYPES,
                )
            )
    for event_type in ("guard_captain", "guard_admiral", "guard_governor"):
        rules.append(
            EventRule(
                id=event_type,
                name=labels[event_type],
                event_type=event_type,
                tier=event_type.removeprefix("guard_"),
                base_strength=35,
                base_duration=10,
            )
        )
    return rules


@dataclass(slots=True)
class AppConfig:
    room_id: str = ""
    auto_connect: bool = True
    rules: list[EventRule] = field(default_factory=default_rules)
    device_generations: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        rules_raw = raw.get("rules")
        existing: list[EventRule] = []
        seen_ids: set[str] = set()
        if isinstance(rules_raw, list):
            for rule_raw in rules_raw:
                if not isinstance(rule_raw, dict):
                    continue
                rule = EventRule.from_dict(rule_raw)
                if not rule.id or rule.id in seen_ids:
                    continue
                existing.append(rule)
                seen_ids.add(rule.id)
        known_ids = {rule.id for rule in existing}
        rules = existing + [rule for rule in default_rules() if rule.id not in known_ids]
        return cls(
            room_id=str(raw.get("room_id", "")),
            auto_connect=bool(raw.get("auto_connect", True)),
            rules=rules,
            device_generations={
                str(key): int(value)
                for key, value in raw.get("device_generations", {}).items()
            },
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
