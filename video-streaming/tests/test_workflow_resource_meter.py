"""Tests for per-Attempt publication resource measurements."""

import math
import time
import unittest

from video_streaming.workflows.resource_meter import ProcessResourceMeter


class ProcessResourceMeterTests(unittest.TestCase):

    def test_meter_records_cpu_memory_and_duration_and_stops_idempotently(self):
        meter = ProcessResourceMeter(sample_interval_seconds=0.01).start()
        payload = bytearray(1024 * 1024)
        for index in range(0, len(payload), 4096):
            payload[index] = 1
        time.sleep(0.04)

        first = meter.stop()
        second = meter.stop()

        self.assertGreaterEqual(first["cpu_seconds"], 0)
        self.assertGreater(first["peak_memory_bytes"], 0)
        self.assertGreater(first["memory_duration_ms"], 0)
        self.assertTrue(math.isfinite(first["cpu_seconds"]))
        self.assertEqual(first, second)

    def test_meter_rejects_invalid_sampling_interval(self):
        for value in (0, -1, True, float("inf")):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ProcessResourceMeter(sample_interval_seconds=value)

    def test_meter_cannot_be_started_twice(self):
        meter = ProcessResourceMeter().start()
        try:
            with self.assertRaises(RuntimeError):
                meter.start()
        finally:
            meter.stop()


if __name__ == "__main__":
    unittest.main()
