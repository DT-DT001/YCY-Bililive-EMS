from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .fixed_waveform_points import FIXED_WAVEFORM_POINTS

@dataclass(slots=True)
class WavePoint:
    frequency: int
    pulse_width: int


@dataclass(slots=True)
class Waveform:
    name: str
    points: list[WavePoint]
    source: str = "builtin"
    fixed_mode: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WAVE_NAMES = ["潮汐", "连击", "压缩", "快按", "渐强", "心跳", "节奏", "呼吸", "摩擦", "弹跳", "波浪", "敲击"]

COYOTE_FREQUENCY_PERIODS_MS = [
    10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25,
    26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41,
    42, 43, 44, 45, 46, 47, 48, 49, 50, 52, 54, 56, 58, 60, 62, 64,
    66, 68, 70, 72, 74, 76, 78, 80, 85, 90, 95, 100, 110, 120, 130,
    140, 150, 160, 170, 180, 190, 200, 233, 266, 300, 333, 366, 400,
    450, 500, 550, 600, 700, 800, 900, 1000,
]
COYOTE_SECTION_SECONDS = [
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1, 1.1, 1.2, 1.3,
    1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6,
    2.7, 2.8, 2.9, 3, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9,
    4, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 4.9, 5, 5.2, 5.4,
    5.6, 5.8, 6, 6.2, 6.4, 6.6, 6.8, 7, 7.2, 7.4, 7.6, 7.8, 8,
    8.5, 9, 9.5, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 23.4,
    26.6, 30, 33.4, 36.6, 40, 45, 50, 55, 60, 70, 80, 90, 100, 120,
    140, 160, 180, 200, 250, 300,
]
MAX_IMPORTED_POINTS = 3000


def _clamp(value: float, low: int = 1, high: int = 100) -> int:
    return max(low, min(high, round(value)))


def _build_builtin(index: int, name: str) -> Waveform:
    fixed_points = FIXED_WAVEFORM_POINTS.get(index + 1)
    if fixed_points:
        return Waveform(
            name,
            [WavePoint(frequency, pulse) for frequency, pulse in fixed_points],
            fixed_mode=index + 1,
        )
    points: list[WavePoint] = []
    sample_count = 20
    for step in range(sample_count):
        phase = step / sample_count * math.tau
        if index == 0:
            freq, pulse = 35 + 18 * math.sin(phase), 45 + 30 * math.sin(phase)
        elif index == 1:
            hit = step % 8 in (0, 1, 3)
            freq, pulse = (78, 85) if hit else (25, 20)
        elif index == 2:
            freq, pulse = 70, 20 + (step % 10) * 7
        elif index == 3:
            freq, pulse = 90 if step % 2 else 55, 65
        elif index == 4:
            freq, pulse = 30 + step * 3, 15 + step * 4
        elif index == 5:
            beat = step % 10
            freq, pulse = (75, 90) if beat in (0, 2) else (20, 15)
        elif index == 6:
            freq, pulse = 40 + (step % 4) * 12, 40 + (step % 4) * 10
        elif index == 7:
            freq, pulse = 28 + 12 * math.sin(phase), 50 + 35 * math.sin(phase)
        elif index == 8:
            freq, pulse = 65 + 12 * math.sin(phase * 5), 45 + 15 * math.sin(phase * 7)
        elif index == 9:
            pulse = 25 + abs(math.sin(phase * 2)) * 70
            freq = 35 + abs(math.sin(phase * 2)) * 45
        elif index == 10:
            freq, pulse = 45 + 25 * math.sin(phase * 2), 50 + 25 * math.sin(phase)
        else:
            hit = step % 10 == 0
            freq, pulse = (95, 95) if hit else (15, 8)
        points.append(WavePoint(_clamp(freq), _clamp(pulse, 0, 100)))
    start = min(range(len(points)), key=lambda position: points[position].pulse_width)
    points = points[start:] + points[:start]
    return Waveform(name, points, fixed_mode=index + 1)


def builtin_waveforms() -> dict[str, Waveform]:
    return {name: _build_builtin(i, name) for i, name in enumerate(WAVE_NAMES)}


def _find_point_pairs(value: Any) -> list[WavePoint]:
    points: list[WavePoint] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                frequency = item.get("frequency", item.get("freq", item.get("hz")))
                pulse = item.get(
                    "pulse_width", item.get("pulse", item.get("width", item.get("us")))
                )
                if frequency is not None and pulse is not None:
                    points.append(WavePoint(_clamp(float(frequency)), _clamp(float(pulse), 0, 100)))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                points.append(WavePoint(_clamp(float(item[0])), _clamp(float(item[1]), 0, 100)))
    return points


def _period_to_frequency(period_ms: float) -> int:
    if period_ms <= 0:
        return 1
    return _clamp(1000.0 / period_ms)


def _coyote_frequency_from_index(index: float, speed_rate: float) -> int:
    safe_index = max(
        0,
        min(len(COYOTE_FREQUENCY_PERIODS_MS) - 1, math.floor(index)),
    )
    period_ms = COYOTE_FREQUENCY_PERIODS_MS[safe_index]
    return _period_to_frequency(period_ms / max(0.01, speed_rate))


def _parse_dungeonlab_pulse(text: str) -> list[WavePoint]:
    marker = "Dungeonlab+pulse:"
    if not text.startswith(marker):
        return []
    sections = text[len(marker):].strip().split("+section+")
    if not sections:
        return []

    rest_duration_ms = 0.0
    speed_rate = 1.0
    if "=" in sections[0]:
        metadata, sections[0] = sections[0].split("=", 1)
        values = metadata.split(",")
        try:
            rest_duration_ms = max(0.0, float(values[0]))
            speed_rate = max(0.01, float(values[1]))
        except (IndexError, ValueError):
            raise ValueError("Dungeonlab PULSE 元数据无效")

    points: list[WavePoint] = [
        WavePoint(1, 0) for _ in range(math.ceil(rest_duration_ms / 100.0))
    ]
    for section in sections:
        if "/" not in section:
            continue
        header, pulse_data = section.split("/", 1)
        try:
            min_index, max_index, duration_index, mode, enabled = [
                int(value) for value in header.split(",")[:5]
            ]
        except (ValueError, TypeError):
            raise ValueError("Dungeonlab PULSE 小节参数无效")
        if enabled != 1:
            continue

        pulse_values: list[float] = []
        for raw_point in pulse_data.split(","):
            if not raw_point:
                continue
            try:
                pulse_values.append(float(raw_point.split("-", 1)[0]))
            except ValueError as exc:
                raise ValueError("Dungeonlab PULSE 波形点无效") from exc
        if not pulse_values:
            continue

        safe_duration_index = max(
            0,
            min(len(COYOTE_SECTION_SECONDS) - 1, duration_index),
        )
        section_seconds = COYOTE_SECTION_SECONDS[safe_duration_index]
        repeat_count = max(
            1,
            math.ceil(section_seconds / (len(pulse_values) * 0.1)),
        )
        total_points = repeat_count * len(pulse_values)
        for repeat_index in range(repeat_count):
            for pulse_index, pulse in enumerate(pulse_values):
                current = repeat_index * len(pulse_values) + pulse_index
                if mode == 2:
                    frequency_index = min_index + (
                        (max_index - min_index) * current / total_points
                    )
                elif mode == 3:
                    frequency_index = min_index + (
                        (max_index - min_index) * pulse_index / len(pulse_values)
                    )
                elif mode == 4:
                    frequency_index = min_index + (
                        (max_index - min_index) * repeat_index / repeat_count
                    )
                else:
                    frequency_index = min_index
                points.append(
                    WavePoint(
                        _coyote_frequency_from_index(
                            frequency_index,
                            speed_rate,
                        ),
                        _clamp(pulse, 0, 100),
                    )
                )
                if len(points) >= MAX_IMPORTED_POINTS:
                    return points
    return points


def _decode_coyote_v2(hex_value: str) -> WavePoint:
    packed = int.from_bytes(bytes.fromhex(hex_value), "little")
    x = packed & 0x1F
    y = (packed >> 5) & 0x3FF
    z = (packed >> 15) & 0x1F
    period_ms = x + y
    if x <= 0 or z <= 0 or period_ms < 10:
        return WavePoint(1, 0)
    return WavePoint(
        _period_to_frequency(period_ms),
        _clamp(z * 5, 0, 100),
    )


def _decode_coyote_v3_period(value: int) -> float | None:
    if value < 10 or value > 240:
        return None
    if value <= 100:
        return float(value)
    if value <= 200:
        return float((value - 100) * 5 + 100)
    return float((value - 200) * 10 + 600)


def _decode_coyote_v3(hex_value: str) -> WavePoint:
    data = bytes.fromhex(hex_value)
    samples: list[tuple[int, float]] = []
    for encoded_period, pulse in zip(data[:4], data[4:8]):
        period_ms = _decode_coyote_v3_period(encoded_period)
        if period_ms is None or pulse <= 0:
            samples.append((1, 0.0))
            continue
        frequency = _period_to_frequency(period_ms)
        samples.append((frequency, float(pulse)))
    pulse = sum(width for _, width in samples) / 4
    weight = sum(width for _, width in samples)
    frequency = (
        sum(freq * width for freq, width in samples) / weight
        if weight
        else 1
    )
    return WavePoint(_clamp(frequency), round(pulse))


def _normalize_coyote_points(points: list[WavePoint]) -> list[WavePoint]:
    return [
        WavePoint(
            _clamp(point.frequency),
            _clamp(point.pulse_width, 0, 100),
        )
        for point in points
    ]


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _find_coyote_points(value: Any) -> list[WavePoint]:
    text = "\n".join(_iter_strings(value)) if not isinstance(value, str) else value
    tokens = re.findall(
        r"(?i)(?<![0-9a-f])(?:0x)?([0-9a-f]{6}|[0-9a-f]{16}|[0-9a-f]{40})(?![0-9a-f])",
        text,
    )
    points: list[WavePoint] = []
    for token in tokens:
        if len(token) == 6:
            points.append(_decode_coyote_v2(token))
        elif len(token) == 16:
            points.append(_decode_coyote_v3(token))
        else:
            data = bytes.fromhex(token)
            points.append(_decode_coyote_v3(data[4:12].hex()))
    return _normalize_coyote_points(points)


def import_waveform(path: Path) -> Waveform:
    text = path.read_text(encoding="utf-8-sig")
    dungeonlab_points = _parse_dungeonlab_pulse(text)
    if dungeonlab_points:
        return Waveform(
            path.stem,
            dungeonlab_points[:MAX_IMPORTED_POINTS],
            "郊狼 PULSE（原生小节格式）",
        )
    if path.suffix.lower() == ".json":
        raw = json.loads(text)
        candidates = [raw]
        if isinstance(raw, dict):
            candidates.extend(raw.get(key) for key in ("points", "data", "wave", "pulses"))
        for candidate in candidates:
            points = _find_point_pairs(candidate)
            if points:
                name = raw.get("name", path.stem) if isinstance(raw, dict) else path.stem
                return Waveform(
                    str(name),
                    points[:MAX_IMPORTED_POINTS],
                    "役次元 JSON",
                )
        points = _find_coyote_points(raw)
        if points:
            name = raw.get("name", path.stem) if isinstance(raw, dict) else path.stem
            return Waveform(
                str(name),
                points[:MAX_IMPORTED_POINTS],
                "郊狼 PULSE（官方映射 v2）",
            )
        raise ValueError("JSON 中未找到 frequency/pulse 波形点")

    points = _find_coyote_points(text)
    if points:
        return Waveform(
            path.stem,
            points[:MAX_IMPORTED_POINTS],
            "郊狼 PULSE（官方映射 v2）",
        )

    numbers = [int(value) for value in re.findall(r"\d+", text)]
    if len(numbers) < 2:
        raise ValueError("PULSE 文件中未找到频率/脉宽数据")
    points = [
        WavePoint(_clamp(numbers[i]), _clamp(numbers[i + 1], 0, 100))
        for i in range(0, len(numbers) - 1, 2)
    ]
    return Waveform(
        path.stem,
        points[:MAX_IMPORTED_POINTS],
        "郊狼 PULSE（频率/脉宽）",
    )
