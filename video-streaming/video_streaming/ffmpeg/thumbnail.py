"""FFmpeg-backed thumbnail rendering primitives."""

from typing import Tuple

import ffmpeg


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


def render_thumbnail(
        video_path: str,
        output_path: str,
        thumbnail_time: str = "0",
        scale_width: int = -1,
        scale_height: int = -1,
        ffmpeg_bin_path: str = "ffmpeg"
        ) -> str:
    """Render one frame to an image and return its output path."""
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
    return output_path
