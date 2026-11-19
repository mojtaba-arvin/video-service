"""Real FFmpeg integration test for supervised watermark rendering."""

import shutil
import tempfile
import unittest
from pathlib import Path

import ffmpeg

from video_streaming.execution.media_probe import probe_media

from video_streaming.execution.process_control import run_streaming_process


@unittest.skipUnless(
    shutil.which("ffmpeg") and shutil.which("ffprobe"),
    "FFmpeg and FFprobe are required for watermark integration tests",
)
class SupervisedWatermarkRenderingTests(unittest.TestCase):

    def test_overlay_render_streams_progress_and_produces_probeable_video(self):
        ffmpeg_path = shutil.which("ffmpeg")
        ffprobe_path = shutil.which("ffprobe")

        with tempfile.TemporaryDirectory(prefix="watermark-supervision-") as temp_dir:
            output_path = Path(temp_dir) / "watermarked.mp4"

            main = ffmpeg.input(
                "testsrc=size=320x180:rate=25:duration=1",
                f="lavfi",
                re=None,
            )
            watermark = ffmpeg.input(
                "color=c=red@0.6:s=64x32:duration=1",
                f="lavfi",
            )
            stream = (
                ffmpeg
                .filter([main, watermark], "overlay", 10, 10)
                .output(
                    str(output_path),
                    vframes=25,
                    vcodec="libx264",
                    pix_fmt="yuv420p",
                )
                .global_args(
                    "-hide_banner",
                    "-loglevel", "info",
                    "-stats_period", "0.1",
                )
            )
            command = ffmpeg.compile(
                stream,
                cmd=ffmpeg_path,
                overwrite_output=True,
            )

            progress_lines = []
            usage_samples = []
            exit_code = run_streaming_process(
                command,
                timeout_seconds=20,
                poll_interval=0.05,
                on_line=lambda line, process: progress_lines.append(line),
                on_usage=usage_samples.append,
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(usage_samples)
            self.assertTrue(any(
                sample["peak_memory_bytes"] for sample in usage_samples
            ))
            self.assertTrue(output_path.is_file())
            self.assertGreater(output_path.stat().st_size, 0)
            self.assertTrue(
                any("frame=" in line for line in progress_lines),
                "FFmpeg progress output was not delivered to the callback",
            )

            probe = probe_media(
                str(output_path),
                ffprobe_bin_path=ffprobe_path,
                timeout_seconds=10,
            )
            video = next(
                item for item in probe.get("streams", [])
                if item.get("codec_type") == "video"
            )
            self.assertEqual((video["width"], video["height"]), (320, 180))
            self.assertEqual(video["codec_name"], "h264")


if __name__ == "__main__":
    unittest.main()
