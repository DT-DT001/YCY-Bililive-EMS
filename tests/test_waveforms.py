import json
import tempfile
import unittest
from pathlib import Path

from backend.waveforms import builtin_waveforms, import_waveform


class WaveformTests(unittest.TestCase):
    def test_twelve_builtins(self):
        waves = builtin_waveforms()
        self.assertEqual(len(waves), 12)
        self.assertTrue(all(len(wave.points) == 20 for wave in waves.values()))
        self.assertTrue(
            all(
                wave.points[0].pulse_width
                == min(point.pulse_width for point in wave.points)
                for wave in waves.values()
            )
        )

    def test_json_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wave.json"
            path.write_text(
                json.dumps({"name": "x", "points": [{"frequency": 20, "pulse": 30}]}),
                "utf-8",
            )
            wave = import_waveform(path)
            self.assertEqual(wave.points[0].frequency, 20)

    def test_pulse_import_and_clamping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wave.pulse"
            path.write_text("10,20\n150,200", "utf-8")
            wave = import_waveform(path)
            self.assertEqual(len(wave.points), 2)
            self.assertEqual(wave.points[1].pulse_width, 100)

    def test_coyote_v2_pulse_is_converted_to_ycy_frequency_and_width(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wave.pulse"
            path.write_text("21010A", "utf-8")
            wave = import_waveform(path)
            self.assertEqual(wave.points[0].frequency, 100)
            self.assertEqual(wave.points[0].pulse_width, 100)

    def test_coyote_v3_frame_is_aggregated_as_one_100ms_ycy_point(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wave.pulse"
            path.write_text("0A0A0A0A0014283C", "utf-8")
            wave = import_waveform(path)
            self.assertEqual(len(wave.points), 1)
            self.assertEqual(wave.points[0].frequency, 100)
            self.assertEqual(wave.points[0].pulse_width, 45)

    def test_coyote_v3_compressed_period_is_converted_to_hz(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wave.json"
            path.write_text(json.dumps({"pulse": ["7878787864646464"]}), "utf-8")
            wave = import_waveform(path)
            self.assertEqual(wave.points[0].frequency, 5)
            self.assertEqual(wave.points[0].pulse_width, 100)

    def test_coyote_v2_burst_count_is_preserved_by_global_normalization(self):
        def encode(x, y, z):
            packed = x | (y << 5) | (z << 15)
            return packed.to_bytes(3, "little").hex()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wave.pulse"
            path.write_text(
                "\n".join((encode(1, 9, 20), encode(5, 95, 20))),
                "utf-8",
            )
            wave = import_waveform(path)
            self.assertEqual(
                [(point.frequency, point.pulse_width) for point in wave.points],
                [(100, 20), (10, 100)],
            )


if __name__ == "__main__":
    unittest.main()
