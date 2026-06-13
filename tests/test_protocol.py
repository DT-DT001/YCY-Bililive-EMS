import unittest

from backend.protocol import (
    checksum,
    generation1_control,
    generation2_fixed,
    generation2_realtime,
    parse_notification,
)


class ProtocolTests(unittest.TestCase):
    def test_generation1_packet(self):
        packet = generation1_control("A", 276, 80, 50)
        self.assertEqual(len(packet), 10)
        self.assertEqual(packet[:4], bytes([0x35, 0x11, 0x01, 0x01]))
        self.assertEqual(packet[-1], checksum(packet[:-1]))

    def test_generation2_packet(self):
        packet = generation2_realtime(20, 30, 40, 276, 90, 100)
        self.assertEqual(len(packet), 12)
        self.assertEqual(packet[2], 0x02)
        self.assertEqual(packet[-1], checksum(packet[:-1]))

    def test_generation1_fixed_mode_ignores_realtime_parameters(self):
        packet = generation1_control("A", 50, 80, 60, mode=3)
        self.assertEqual(packet[6:9], bytes([3, 0, 0]))

    def test_generation1_stop_packet_uses_valid_frequency(self):
        packet = generation1_control("AB", 0, 80, 60, mode=3)
        self.assertEqual(packet[2:9], bytes([3, 0, 0, 0, 0, 1, 0]))

    def test_generation1_custom_accepts_zero_pulse_time(self):
        packet = generation1_control("A", 50, 80, 0, mode=0x11)
        self.assertEqual(packet[2:9], bytes([1, 1, 0, 50, 0x11, 80, 0]))

    def test_generation1_custom_sends_frequency_and_pulse_time(self):
        packet = generation1_control("A", 50, 100, 100, mode=0x11)
        self.assertEqual(packet[7:9], bytes([100, 100]))

    def test_generation2_fixed_mode_packet(self):
        packet = generation2_fixed(20, 1, 30, 12)
        self.assertEqual(len(packet), 10)
        self.assertEqual(packet[2], 0x01)
        self.assertEqual(packet[3:6], bytes([0, 20, 1]))
        self.assertEqual(packet[6:9], bytes([0, 30, 12]))
        self.assertEqual(packet[-1], checksum(packet[:-1]))

    def test_channel_notification(self):
        raw = bytearray([0x35, 0x71, 1, 1, 1, 0, 20, 0x11])
        raw.append(checksum(raw))
        report = parse_notification(bytes(raw))
        self.assertEqual(report.channel, "A")
        self.assertEqual(report.strength, 20)


if __name__ == "__main__":
    unittest.main()
