"""Bounded FFprobe metadata inspection using the shared process supervisor."""

import json

from video_streaming.execution.process_control import run_cancellable_process


def probe_media(
        media_path,
        ffprobe_bin_path,
        timeout_seconds,
        should_cancel=None):
    """Return FFprobe JSON while bounding the complete process tree.

    The command runs in its own process group through the same supervisor as
    FFmpeg. Timeout and cooperative cancellation therefore terminate FFprobe
    and any descendants instead of leaving an untracked subprocess behind.
    """
    if timeout_seconds is None or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")

    command = [
        ffprobe_bin_path,
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(media_path),
    ]
    stdout, _ = run_cancellable_process(
        command,
        timeout_seconds=timeout_seconds,
        should_cancel=should_cancel,
    )
    return json.loads(stdout.decode("utf-8"))
