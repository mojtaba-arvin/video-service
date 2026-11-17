"""FFmpeg-backed thumbnail rendering and output validation."""

from pathlib import Path
from typing import Dict, Optional, Tuple

import ffmpeg


class ThumbnailValidationError(RuntimeError):
    """Raised when a rendered thumbnail is missing or not a valid image."""


def normalize_thumbnail_scale(
        scale_width: int,
        scale_height: int
        ) -> Tuple[int, int]:
    """Map protobuf's zero-valued unspecified dimensions to FFmpeg's -1.

    FFmpeg uses -1 to infer one dimension while preserving the input aspect
    ratio. Each dimension must be normalized independently.
    """
    if scale_width == 0:
        scale_width = -1
    if scale_height == 0:
        scale_height = -1
    return scale_width, scale_height


def validate_thumbnail_output(
        output_path: str,
        ffprobe_bin_path: str = "ffprobe",
        expected_width: Optional[int] = None,
        expected_height: Optional[int] = None
        ) -> Dict[str, int]:
    """Require a non-empty, probeable image with the requested dimensions."""
    path = Path(output_path)
    if not path.is_file():
        raise ThumbnailValidationError(
            "FFmpeg did not create the thumbnail output file"
        )
    if path.stat().st_size <= 0:
        raise ThumbnailValidationError(
            "FFmpeg created an empty thumbnail output file"
        )

    try:
        probe = ffmpeg.probe(str(path), cmd=ffprobe_bin_path)
    except ffmpeg.Error as exc:
        raise ThumbnailValidationError(
            "FFprobe could not read the generated thumbnail"
        ) from exc

    video_stream = next(
        (stream for stream in probe.get("streams", [])
         if stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise ThumbnailValidationError(
            "Thumbnail output contains no video/image stream"
        )

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if width <= 0 or height <= 0:
        raise ThumbnailValidationError(
            "Thumbnail output has invalid dimensions"
        )
    if expected_width is not None and width != expected_width:
        raise ThumbnailValidationError(
            "Thumbnail width mismatch: expected {}, got {}".format(
                expected_width, width
            )
        )
    if expected_height is not None and height != expected_height:
        raise ThumbnailValidationError(
            "Thumbnail height mismatch: expected {}, got {}".format(
                expected_height, height
            )
        )

    return {"width": width, "height": height}


def render_thumbnail(
        video_path: str,
        output_path: str,
        thumbnail_time: str = "0",
        scale_width: int = -1,
        scale_height: int = -1,
        ffmpeg_bin_path: str = "ffmpeg",
        ffprobe_bin_path: str = "ffprobe"
        ) -> str:
    """Render one frame and return only after FFprobe validates the artifact."""
    scale_width, scale_height = normalize_thumbnail_scale(
        scale_width,
        scale_height
    )

    (
        ffmpeg
        .input(video_path, ss=thumbnail_time)
        .filter("scale", scale_width, scale_height)
        .output(output_path, vframes=1)
        .run(
            cmd=ffmpeg_bin_path,
            capture_stdout=True,
            capture_stderr=True,
            overwrite_output=True
        )
    )

    validate_thumbnail_output(
        output_path=output_path,
        ffprobe_bin_path=ffprobe_bin_path,
        expected_width=scale_width if scale_width > 0 else None,
        expected_height=scale_height if scale_height > 0 else None,
    )
    return output_path
