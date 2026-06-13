from __future__ import annotations

from dataclasses import dataclass


SERVICE_UUID = "0000ff30-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ff31-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ff32-0000-1000-8000-00805f9b34fb"


def checksum(payload: bytes | bytearray) -> int:
    return sum(payload) & 0xFF


def with_checksum(values: list[int]) -> bytes:
    payload = bytearray(values)
    payload.append(checksum(payload))
    return bytes(payload)


def generation1_control(
    channel: str,
    strength: int,
    frequency: int,
    pulse_width: int,
    mode: int = 0x11,
) -> bytes:
    channel_code = {"A": 1, "B": 2, "AB": 3}[channel]
    strength = max(0, min(276, strength))
    if mode == 0x11 and pulse_width <= 0:
        strength = 0
    enabled = int(strength > 0)
    if not enabled:
        return with_checksum(
            [0x35, 0x11, channel_code, 0, 0, 0, 0, 1, 0]
        )
    fixed_mode = max(1, min(16, mode)) if mode != 0x11 else 0x11
    return with_checksum(
        [
            0x35,
            0x11,
            channel_code,
            enabled,
            strength >> 8,
            strength & 0xFF,
            fixed_mode,
            max(1, min(100, frequency)) if fixed_mode == 0x11 else 0,
            max(0, min(100, pulse_width)) if fixed_mode == 0x11 else 0,
        ]
    )


def generation2_fixed(
    a_strength: int,
    a_mode: int,
    b_strength: int,
    b_mode: int,
) -> bytes:
    return with_checksum(
        [
            0x35,
            0x11,
            0x01,
            max(0, min(276, a_strength)) >> 8,
            max(0, min(276, a_strength)) & 0xFF,
            max(1, min(16, a_mode)),
            max(0, min(276, b_strength)) >> 8,
            max(0, min(276, b_strength)) & 0xFF,
            max(1, min(16, b_mode)),
        ]
    )


def generation2_realtime(
    a_strength: int,
    a_frequency: int,
    a_pulse_width: int,
    b_strength: int,
    b_frequency: int,
    b_pulse_width: int,
) -> bytes:
    return with_checksum(
        [
            0x35,
            0x11,
            0x02,
            max(0, min(276, a_strength)) >> 8,
            max(0, min(276, a_strength)) & 0xFF,
            max(1, min(100, a_frequency)),
            max(0, min(100, a_pulse_width)),
            max(0, min(276, b_strength)) >> 8,
            max(0, min(276, b_strength)) & 0xFF,
            max(1, min(100, b_frequency)),
            max(0, min(100, b_pulse_width)),
        ]
    )


def query(query_type: int) -> bytes:
    return with_checksum([0x35, 0x71, query_type])


@dataclass(slots=True)
class ChannelReport:
    channel: str
    electrode_state: int
    enabled: bool
    strength: int
    mode: int


def parse_notification(data: bytes) -> ChannelReport | dict[str, int] | None:
    if len(data) < 4 or data[0] != 0x35 or checksum(data[:-1]) != data[-1]:
        return None
    if data[1] != 0x71:
        return None
    response_type = data[2]
    if response_type in (1, 2) and len(data) >= 9:
        return ChannelReport(
            "A" if response_type == 1 else "B",
            data[3],
            bool(data[4]),
            (data[5] << 8) | data[6],
            data[7],
        )
    if response_type == 4 and len(data) >= 5:
        return {"battery": data[3]}
    if response_type == 0x55 and len(data) >= 5:
        return {"error": data[3]}
    return None
