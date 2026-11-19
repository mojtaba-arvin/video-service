"""Tests for durable lease fencing in FFmpeg progress callbacks."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from video_streaming.ffmpeg.utils.ffmpeg_callback import FfmpegCallback


class FfmpegCallbackLeaseTests(unittest.TestCase):

    def test_lease_renewal_failure_terminates_ffmpeg_process(self):
        task = Mock()
        task.request = SimpleNamespace(
            id="task-1",
            called_directly=False,
            kwargs={},
        )
        task.is_forced_to_stop.return_value = False
        task.is_output_forced_to_stop.return_value = False

        process = SimpleNamespace(pid=12345)

        def lose_lease():
            raise RuntimeError("lease renewal failed")

        callback = FfmpegCallback(
            task=task,
            task_id="task-1",
            output_id="output-1",
            request_id="job-1",
            on_progress=lose_lease,
        )

        with patch(
                "video_streaming.ffmpeg.utils.ffmpeg_callback."
                "terminate_process_tree") as terminate:
            with self.assertRaisesRegex(
                    RuntimeError, "lease renewal failed"):
                callback.progress(
                    "frame=1",
                    10.0,
                    1.0,
                    9.0,
                    process,
                )

        terminate.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
