"""Durable FFmpeg media handlers.

The executor owns the workflow Attempt lease. These handlers use only
attempt-scoped temporary files, supervised FFmpeg processes, and immutable
Artifact Store outputs; they do not call Celery or claim repository attempts.
"""
import json
import os
import tempfile

import ffmpeg

from video_streaming import settings
from video_streaming.execution.media_probe import probe_media
from video_streaming.execution.process_control import (
    ProcessUsageAccumulator,
    run_cancellable_process,
)
from video_streaming.ffmpeg.thumbnail import render_thumbnail
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.errors import (
    PermanentOperationError,
    RetryableOperationError,
)


_VALIDATION = {"valid": True, "errors": []}


def _parameters(node):
    value = node.get("parameters")
    if not isinstance(value, dict):
        raise PermanentOperationError("workflow node parameters are invalid")
    return value


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise PermanentOperationError("{} is required".format(name))
    return value.strip()


def _check_active(context):
    if context.is_cancelled():
        context.cancel(reason="JOB_CANCELLED_DURING_MEDIA_OPERATION")
        raise AttemptLeaseLost("workflow job was cancelled")
    if context.heartbeat() is False:
        raise AttemptLeaseLost("workflow attempt lease was lost")


def _find_artifact(context, logical_name):
    matches = [
        item
        for artifacts in (context.input_artifacts or {}).values()
        for item in artifacts
        if item.get("logical_name") == logical_name
    ]
    if len(matches) != 1:
        raise PermanentOperationError(
            "expected exactly one dependency artifact: {}".format(logical_name)
        )
    return matches[0]


def _read_manifest(artifact_store, context):
    manifest_artifact = _find_artifact(context, "inputs.manifest")
    descriptor, path = tempfile.mkstemp(prefix="workflow-media-", suffix=".json")
    os.close(descriptor)
    try:
        artifact_store.download_file(
            manifest_artifact["uri"], path,
            expected_sha256=manifest_artifact.get("checksum_sha256"),
            on_chunk=lambda _size: _check_active(context),
        )
        with open(path, "rb") as source:
            manifest = json.loads(source.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermanentOperationError("input manifest is invalid") from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if not isinstance(manifest, dict) or not isinstance(manifest.get("inputs"), dict):
        raise PermanentOperationError("input manifest schema is invalid")
    return manifest


def _download_input(artifact_store, context, manifest, name, directory):
    entry = manifest["inputs"].get(name)
    if not isinstance(entry, dict):
        raise PermanentOperationError("required media input is missing: {}".format(name))
    artifact = entry.get("file_artifact")
    if not isinstance(artifact, dict) or not artifact.get("uri"):
        raise PermanentOperationError("media input artifact reference is invalid")
    filename = os.path.join(directory, "{}-source".format(name))
    artifact_store.download_file(
        artifact["uri"], filename,
        expected_sha256=artifact.get("checksum_sha256"),
        on_chunk=lambda _size: _check_active(context),
    )
    _check_active(context)
    return filename, artifact


def _store_output(artifact_store, path, logical_name, media_type, context):
    if not os.path.isfile(path) or os.path.getsize(path) <= 0:
        raise RetryableOperationError("media operation produced no output")
    _check_active(context)
    stored = artifact_store.put_file(
        path,
        "media/{}".format(logical_name.replace(".", "/")),
        context.attempt_id,
        media_type=media_type,
    )
    _check_active(context)
    return {
        "logical_name": logical_name,
        "artifact_type": "media_file",
        "uri": stored["uri"],
        "media_type": stored.get("media_type"),
        "size_bytes": stored["size_bytes"],
        "checksum_sha256": stored["checksum_sha256"],
    }


def build_media_handlers(
        artifact_store,
        ffmpeg_bin_path=None,
        ffprobe_bin_path=None,
        thumbnail_renderer=render_thumbnail,
        media_probe=probe_media):
    """Build the two standalone media handlers; packaging remains separate."""
    if artifact_store is None:
        raise ValueError("Artifact Store is required")
    ffmpeg_bin_path = ffmpeg_bin_path or settings.FFMPEG_BIN_PATH
    ffprobe_bin_path = ffprobe_bin_path or settings.FFPROBE_BIN_PATH

    def generate_thumbnail(node, context):
        params = _parameters(node)
        thumbnail_time = params.get("thumbnail_time")
        if isinstance(thumbnail_time, bool) or not isinstance(
                thumbnail_time, (int, float, str)):
            raise PermanentOperationError("thumbnail_time is invalid")
        width = params.get("scale_width", 0)
        height = params.get("scale_height", 0)
        for name, value in (("scale_width", width), ("scale_height", height)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PermanentOperationError("{} is invalid".format(name))
        manifest = _read_manifest(artifact_store, context)
        input_name = "watermarked_video" if params.get("use_watermark") else "video"
        usage = ProcessUsageAccumulator()
        with tempfile.TemporaryDirectory(prefix="workflow-thumbnail-") as directory:
            video_path, source = _download_input(
                artifact_store, context, manifest, input_name, directory
            )
            output_path = os.path.join(directory, "thumbnail.jpg")

            def on_usage(sample):
                usage.record(sample)
                _check_active(context)

            try:
                thumbnail_renderer(
                    video_path=video_path,
                    output_path=output_path,
                    thumbnail_time=str(thumbnail_time),
                    scale_width=width,
                    scale_height=height,
                    ffmpeg_bin_path=ffmpeg_bin_path,
                    ffprobe_bin_path=ffprobe_bin_path,
                    timeout_seconds=settings.FFMPEG_THUMBNAIL_TIMEOUT_SECONDS,
                    ffprobe_timeout_seconds=settings.FFPROBE_TIMEOUT_SECONDS,
                    should_cancel=context.is_cancelled,
                    usage_callback=on_usage,
                )
            except AttemptLeaseLost:
                raise
            except Exception as exc:
                if context.is_cancelled():
                    context.cancel(reason="JOB_CANCELLED_DURING_THUMBNAIL")
                    raise AttemptLeaseLost("workflow job was cancelled") from exc
                raise RetryableOperationError("thumbnail rendering failed") from exc
            _check_active(context)
            dimensions = media_probe(
                output_path,
                ffprobe_bin_path=ffprobe_bin_path,
                timeout_seconds=settings.FFPROBE_TIMEOUT_SECONDS,
                should_cancel=context.is_cancelled,
                on_usage=on_usage,
            )
            stream = next(
                (item for item in dimensions.get("streams", [])
                 if item.get("codec_type") == "video"),
                None,
            )
            if stream is None:
                raise PermanentOperationError("thumbnail contains no image stream")
            width_actual = int(stream.get("width") or 0)
            height_actual = int(stream.get("height") or 0)
            if width_actual <= 0 or height_actual <= 0:
                raise PermanentOperationError("thumbnail dimensions are invalid")
            artifact = _store_output(
                artifact_store, output_path, "thumbnail.image", "image/jpeg", context
            )
            metrics = usage.aggregate()
            metrics["input_bytes"] = source.get("size_bytes")
            metrics["output_bytes"] = artifact["size_bytes"]
            return {
                "validation_report": {
                    "valid": True,
                    "artifact_type": "image_thumbnail",
                    "width": width_actual,
                    "height": height_actual,
                    "size_bytes": artifact["size_bytes"],
                    "errors": [],
                },
                "artifacts": [artifact],
                "metrics": metrics,
            }

    def apply_watermark(node, context):
        params = _parameters(node)
        manifest = _read_manifest(artifact_store, context)
        usage = ProcessUsageAccumulator()
        with tempfile.TemporaryDirectory(prefix="workflow-watermark-") as directory:
            video_path, video_artifact = _download_input(
                artifact_store, context, manifest, "video", directory
            )
            watermark_path, watermark_artifact = _download_input(
                artifact_store, context, manifest, "watermark", directory
            )
            output_path = os.path.join(directory, "watermarked.mp4")

            def on_usage(sample):
                usage.record(sample)
                _check_active(context)

            try:
                video = ffmpeg.input(video_path)
                watermark = ffmpeg.input(watermark_path)
                stream = ffmpeg.filter([video, watermark], "overlay", 0, 0)
                command = ffmpeg.compile(
                    stream.output(output_path),
                    cmd=ffmpeg_bin_path,
                    overwrite_output=True,
                )
                run_cancellable_process(
                    command,
                    timeout_seconds=settings.FFMPEG_WATERMARK_TIMEOUT_SECONDS,
                    should_cancel=context.is_cancelled,
                    on_usage=on_usage,
                )
                _check_active(context)
                metadata = media_probe(
                    output_path,
                    ffprobe_bin_path=ffprobe_bin_path,
                    timeout_seconds=settings.FFPROBE_TIMEOUT_SECONDS,
                    should_cancel=context.is_cancelled,
                    on_usage=on_usage,
                )
            except AttemptLeaseLost:
                raise
            except Exception as exc:
                if context.is_cancelled():
                    context.cancel(reason="JOB_CANCELLED_DURING_WATERMARK")
                    raise AttemptLeaseLost("workflow job was cancelled") from exc
                raise RetryableOperationError("watermark processing failed") from exc
            video_stream = next(
                (item for item in metadata.get("streams", [])
                 if item.get("codec_type") == "video"),
                None,
            )
            if video_stream is None:
                raise PermanentOperationError(
                    "watermarked output contains no video stream"
                )
            artifact = _store_output(
                artifact_store, output_path, "watermark.video", "video/mp4", context
            )
            metrics = usage.aggregate()
            metrics["input_bytes"] = (
                video_artifact.get("size_bytes", 0)
                + watermark_artifact.get("size_bytes", 0)
            )
            metrics["output_bytes"] = artifact["size_bytes"]
            return {
                "validation_report": {
                    "valid": True,
                    "artifact_type": "video_file",
                    "width": int(video_stream.get("width") or 0),
                    "height": int(video_stream.get("height") or 0),
                    "size_bytes": artifact["size_bytes"],
                    "video_codec": video_stream.get("codec_name"),
                    "errors": [],
                },
                "artifacts": [artifact],
                "metrics": metrics,
            }

    return {
        "video.thumbnail.generate": generate_thumbnail,
        "video.watermark.apply": apply_watermark,
    }
