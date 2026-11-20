"""Per-task CPU and peak-RSS measurements for non-FFmpeg worker phases."""

import math
import threading
import time

import psutil


class ProcessResourceMeter:
    """Measure worker-process CPU and peak RSS over one bounded task phase."""

    def __init__(self, sample_interval_seconds=0.25):
        if (
                isinstance(sample_interval_seconds, bool)
                or not isinstance(sample_interval_seconds, (int, float))
                or not math.isfinite(float(sample_interval_seconds))
                or sample_interval_seconds <= 0):
            raise ValueError("sample_interval_seconds must be positive")
        self.sample_interval_seconds = float(sample_interval_seconds)
        self._process = psutil.Process()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._started_at = None
        self._cpu_started = None
        self._peak_memory_bytes = 0
        self._result = None

    def _sample_memory(self):
        try:
            value = int(self._process.memory_info().rss)
        except psutil.Error:
            return
        with self._lock:
            self._peak_memory_bytes = max(self._peak_memory_bytes, value)

    def _sample_loop(self):
        while not self._stop_event.wait(self.sample_interval_seconds):
            self._sample_memory()

    def start(self):
        if self._started_at is not None:
            raise RuntimeError("ProcessResourceMeter can only be started once")
        self._started_at = time.monotonic()
        self._cpu_started = time.process_time()
        self._sample_memory()
        self._thread = threading.Thread(
            target=self._sample_loop,
            name="workflow-resource-meter",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self):
        if self._started_at is None:
            raise RuntimeError("ProcessResourceMeter has not been started")
        if self._result is not None:
            return dict(self._result)

        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
        self._sample_memory()
        finished_at = time.monotonic()
        with self._lock:
            peak_memory_bytes = self._peak_memory_bytes
        self._result = {
            "cpu_seconds": max(
                0.0, time.process_time() - self._cpu_started
            ),
            "peak_memory_bytes": peak_memory_bytes,
            "memory_duration_ms": max(
                0, int((finished_at - self._started_at) * 1000)
            ),
        }
        return dict(self._result)
