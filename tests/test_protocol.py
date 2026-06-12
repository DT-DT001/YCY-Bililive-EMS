import unittest

from backend.protocol import (
    checksum,
    generation1_control,
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

    def test_channel_notification(self):
        raw = bytearray([0x35, 0x71, 1, 1, 1, 0, 20, 0x11])
        raw.append(checksum(raw))
        report = parse_notification(bytes(raw))
        self.assertEqual(report.channel, "A")
        self.assertEqual(report.strength, 20)


if __name__ == "__main__":
    unittest.main()

