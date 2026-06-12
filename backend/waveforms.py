from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class WavePoint:
    frequency: int
    pulse_width: int


@dataclass(slots=True)
class Waveform:
    name: str
    points: list[WavePoint]
    source: str = "builtin"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


WAVE_NAMES = ["潮汐", "连击", "压缩", "快按", "渐强", "心跳", "节奏", "呼吸", "摩擦", "弹跳", "波浪", "敲击"]


def _clamp(value: float, low: int = 1, high: int = 100) -> int:
    return max(low, min(high, round(value)))


def _build_builtin(index: int, name: str) -> Waveform:
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
    return Waveform(name, points)


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


def _decode_coyote_v2(hex_value: str) -> WavePoint:
    packed = int.from_bytes(bytes.fromhex(hex_value), "little")
    x = packed & 0x1F
    y = (packed >> 5) & 0x3FF
    z = (packed >> 15) & 0x1F
    period_ms = x + y
    if x <= 0 or period_ms < 10:
        return WavePoint(1, 0)
    return WavePoint(
        _period_to_frequency(period_ms),
        round(z * 5 * x),
    )


def _decode_coyote_v3_period(value: int) -> float | None:
    if value < 10 or value > 240:
        return None
    if value <= 100:
        return float(value)
    if value <= 200:
        return float((value - 100) * 5 + 100)
    return float((value - 200) * 10 + 600)


def _estimated_coyote_burst_count(period_ms: float) -> float:
    # DG-LAB's documented V2 balance formula estimates how many 1ms pulses
    # form one burst. V3 hides this split behind its frequency-balance setting.
    return max(1.0, min(31.0, math.sqrt(period_ms / 1000.0) * 15.0))


def _decode_coyote_v3(hex_value: str) -> WavePoint:
    data = bytes.fromhex(hex_value)
    samples: list[tuple[int, float]] = []
    for encoded_period, pulse in zip(data[:4], data[4:8]):
        period_ms = _decode_coyote_v3_period(encoded_period)
        if period_ms is None or pulse <= 0:
            samples.append((1, 0.0))
            continue
        frequency = _period_to_frequency(period_ms)
        equivalent_width = pulse * _estimated_coyote_burst_count(period_ms)
        samples.append((frequency, equivalent_width))
    pulse = sum(width for _, width in samples) / 4
    weight = sum(width for _, width in samples)
    frequency = (
        sum(freq * width for freq, width in samples) / weight
        if weight
        else 1
    )
    return WavePoint(_clamp(frequency), round(pulse))


def _normalize_coyote_points(points: list[WavePoint]) -> list[WavePoint]:
    if not points:
        return points
    peak = max(point.pulse_width for point in points)
    scale = min(1.0, 100.0 / peak) if peak > 0 else 1.0
    return [
        WavePoint(
            _clamp(point.frequency),
            _clamp(point.pulse_width * scale, 0, 100),
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
    if path.suffix.lower() == ".json":
        raw = json.loads(text)
        candidates = [raw]
        if isinstance(raw, dict):
            candidates.extend(raw.get(key) for key in ("points", "data", "wave", "pulses"))
        for candidate in candidates:
            points = _find_point_pairs(candidate)
            if points:
                name = raw.get("name", path.stem) if isinstance(raw, dict) else path.stem
                return Waveform(str(name), points[:100], "役次元 JSON")
        points = _find_coyote_points(raw)
        if points:
            name = raw.get("name", path.stem) if isinstance(raw, dict) else path.stem
            return Waveform(str(name), points[:100], "郊狼 PULSE（已适配役次元）")
        raise ValueError("JSON 中未找到 frequency/pulse 波形点")

    points = _find_coyote_points(text)
    if points:
        return Waveform(path.stem, points[:100], "郊狼 PULSE（已适配役次元）")

    numbers = [int(value) for value in re.findall(r"\d+", text)]
    if len(numbers) < 2:
        raise ValueError("PULSE 文件中未找到频率/脉宽数据")
    points = [
        WavePoint(_clamp(numbers[i]), _clamp(numbers[i + 1], 0, 100))
        for i in range(0, len(numbers) - 1, 2)
    ]
    return Waveform(path.stem, points[:100], "郊狼 PULSE（频率/脉宽）")
