"""Bounded FFprobe execution shared by media validation paths."""

import json

from video_streaming.execution.process_control import run_cancellable_process


class FFprobeExecutionError(RuntimeError):
    """Raised when FFprobe fails or returns invalid metadata."""


def probe_media(
        path,
        ffprobe_bin_path="ffprobe",
        timeout_seconds=10,
        should_cancel=None):
    """Run FFprobe under a process-tree deadline and parse its JSON output."""
    command = [
        ffprobe_bin_path,
        "-v", "error",
        "-show_format",
        "-show_streams",
        "-of", "json",
        str(path),
    ]
    try:
        stdout, _ = run_cancellable_process(
            command,
            timeout_seconds=timeout_seconds,
            should_cancel=should_cancel,
        )
    except Exception as exc:
        raise FFprobeExecutionError(
            "FFprobe execution failed for media validation"
        ) from exc

    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FFprobeExecutionError(
            "FFprobe returned invalid JSON metadata"
        ) from exc

    if not isinstance(result, dict) or not isinstance(result.get("streams"), list):
        raise FFprobeExecutionError(
            "FFprobe metadata is missing the streams collection"
        )
    return result
