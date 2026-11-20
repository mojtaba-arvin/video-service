"""Bounded FFprobe metadata inspection using the shared process supervisor."""

import json

from video_streaming.execution.process_control import run_cancellable_process


def probe_media(
        media_path,
        ffprobe_bin_path,
        timeout_seconds,
        should_cancel=None):
    """Return structurally valid FFprobe JSON under a process-tree deadline.

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
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("FFprobe returned invalid JSON metadata") from exc

    if not isinstance(result, dict):
        raise ValueError("FFprobe metadata root must be a JSON object")
    streams = result.get("streams")
    if not isinstance(streams, list):
        raise ValueError("FFprobe metadata must contain a streams array")
    if any(not isinstance(stream, dict) for stream in streams):
        raise ValueError("FFprobe streams must contain JSON objects")
    format_data = result.get("format")
    if format_data is not None and not isinstance(format_data, dict):
        raise ValueError("FFprobe format metadata must be a JSON object")
    return result
