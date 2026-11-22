"""Durable FFmpeg media handlers.

The executor owns the workflow Attempt lease. These handlers use only
attempt-scoped temporary files, supervised FFmpeg processes, and immutable
Artifact Store outputs; they do not call Celery or claim repository attempts.
"""
import json
import os
import shlex
import tempfile

import ffmpeg
import ffmpeg_streaming
from ffmpeg_streaming import Bitrate, Representation, Size
from ffmpeg_streaming._command_builder import command_builder

from video_streaming import settings
from video_streaming.execution.media_probe import probe_media
from video_streaming.execution.process_control import (
    ProcessUsageAccumulator,
    run_cancellable_process,
    run_streaming_process,
)
from video_streaming.ffmpeg.constants import Resolutions, VideoEncodingFormats
from video_streaming.ffmpeg.package_validation import validate_package
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


def _download_dependency_artifact(artifact_store, context, artifact, name, directory):
    if not isinstance(artifact, dict) or not artifact.get("uri"):
        raise PermanentOperationError("media dependency artifact is invalid")
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
    """Build four durable media handlers for HLS, DASH, thumbnail and watermark."""
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
        usage = ProcessUsageAccumulator()
        with tempfile.TemporaryDirectory(prefix="workflow-thumbnail-") as directory:
            if params.get("use_watermark"):
                source = _find_artifact(context, "watermark.video")
                video_path, source = _download_dependency_artifact(
                    artifact_store, context, source, "watermarked-video", directory
                )
            else:
                manifest = _read_manifest(artifact_store, context)
                video_path, source = _download_input(
                    artifact_store, context, manifest, "video", directory
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

    def package_playlist(node, context, protocol_name):
        params = _parameters(node)
        if params.get("protocol") != protocol_name:
            raise PermanentOperationError("package protocol does not match operation")
        encode_format = params.get("encode_format") or settings.DEFAULT_ENCODE_FORMAT
        fragmented = params.get("fragmented", False)
        if not isinstance(fragmented, bool):
            raise PermanentOperationError("fragmented must be a boolean")
        quality_names = params.get("quality_names") or []
        custom_qualities = params.get("custom_qualities") or []
        if not isinstance(quality_names, list) or not isinstance(custom_qualities, list):
            raise PermanentOperationError("package quality configuration is invalid")
        usage = ProcessUsageAccumulator()
        with tempfile.TemporaryDirectory(prefix="workflow-package-") as directory:
            if params.get("use_watermark"):
                source = _find_artifact(context, "watermark.video")
                video_path, source_artifact = _download_dependency_artifact(
                    artifact_store, context, source, "watermarked-video", directory
                )
            else:
                manifest = _read_manifest(artifact_store, context)
                video_path, source_artifact = _download_input(
                    artifact_store, context, manifest, "video", directory
                )
            output_key = _required_text(params.get("key"), "key")
            output_path = os.path.join(directory, os.path.basename(output_key))

            def on_usage(sample):
                usage.record(sample)
                _check_active(context)

            try:
                video = ffmpeg_streaming.input(video_path)
                format_instance = VideoEncodingFormats().get_format_class(
                    encode_format,
                    video=params.get("video_codec"),
                    audio=params.get("audio_codec"),
                )
                protocol = (
                    video.hls(format_instance)
                    if protocol_name == "hls"
                    else video.dash(format_instance)
                )
                if protocol_name == "hls" and fragmented:
                    protocol.fragmented_mp4()
                if not (custom_qualities or quality_names):
                    protocol.auto_generate_representations(
                        ffprobe_bin=settings.FFPROBE_BIN_PATH
                    )
                else:
                    representations = []
                    if quality_names:
                        representations.extend(Resolutions().get_reps(quality_names))
                    for quality in custom_qualities:
                        if not isinstance(quality, dict):
                            raise PermanentOperationError(
                                "custom quality entry must be an object"
                            )
                        size = quality.get("size")
                        bitrate = quality.get("bitrate")
                        if not size or not bitrate:
                            raise PermanentOperationError(
                                "custom quality requires both size and bitrate"
                            )
                        representations.append(
                            Representation(Size(*size), Bitrate(*bitrate))
                        )
                    if not representations:
                        raise PermanentOperationError(
                            "package requires at least one representation"
                        )
                    protocol.representations(*representations)

                protocol.output(output_path, run_command=False)
                reps = list(protocol.reps)
                if protocol_name == "hls":
                    stem = os.path.splitext(os.path.basename(output_path))[0]
                    expected_renditions = [
                        "{}_{}p.m3u8".format(stem, rep.size.height)
                        for rep in reps
                    ]
                else:
                    expected_renditions = [
                        str(index) for index, _ in enumerate(reps)
                    ]
                command = shlex.split(
                    command_builder(settings.FFMPEG_BIN_PATH, protocol)
                )
                run_streaming_process(
                    command,
                    timeout_seconds=settings.FFMPEG_PLAYLIST_TIMEOUT_SECONDS,
                    on_line=lambda _line, _process: None,
                    should_cancel=context.is_cancelled,
                    on_usage=on_usage,
                )
                _check_active(context)
                validation = validate_package(
                    directory=directory,
                    manifest_path=output_path,
                    package_type=protocol_name,
                    expected_renditions=expected_renditions,
                    ffprobe_bin_path=ffprobe_bin_path,
                    metadata_probe_timeout_seconds=(
                        settings.PACKAGE_METADATA_PROBE_TIMEOUT_SECONDS
                    ),
                    should_cancel=context.is_cancelled,
                    on_usage=on_usage,
                )
                validation.require_valid()
                validation_payload = validation.as_dict()
            except AttemptLeaseLost:
                raise
            except Exception as exc:
                if context.is_cancelled():
                    context.cancel(reason="JOB_CANCELLED_DURING_PACKAGE")
                    raise AttemptLeaseLost("workflow job was cancelled") from exc
                if isinstance(exc, (PermanentOperationError, RetryableOperationError)):
                    raise
                raise RetryableOperationError(
                    "{} packaging or validation failed".format(protocol_name.upper())
                ) from exc

            uploaded = []
            main_manifest_artifact = None
            for root, directories, filenames in os.walk(directory, followlinks=False):
                directories[:] = sorted(
                    name for name in directories
                    if not os.path.islink(os.path.join(root, name))
                )
                for filename in sorted(filenames):
                    local_path = os.path.join(root, filename)
                    if os.path.islink(local_path) or not os.path.isfile(local_path):
                        raise PermanentOperationError(
                            "package contains an unsupported filesystem entry"
                        )
                    relative = os.path.relpath(local_path, directory).replace(
                        os.sep, "/"
                    )
                    media_type = (
                        "application/vnd.apple.mpegurl"
                        if filename.endswith(".m3u8")
                        else "application/dash+xml"
                        if filename.endswith(".mpd")
                        else "application/octet-stream"
                    )
                    logical_name = (
                        "package.{}.manifest".format(protocol_name)
                        if relative == os.path.basename(output_path)
                        else "package.file.{}".format(relative)
                    )
                    artifact = _store_output(
                        artifact_store, local_path, logical_name, media_type, context
                    )
                    uploaded.append({"path": relative, "artifact": artifact})
                    if relative == os.path.basename(output_path):
                        main_manifest_artifact = artifact

            if not uploaded or main_manifest_artifact is None:
                raise RetryableOperationError("validated package artifacts are missing")
            directory_payload = json.dumps(
                {
                    "schema_version": 1,
                    "protocol": protocol_name,
                    "entrypoint": main_manifest_artifact,
                    "files": uploaded,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            stored_manifest = artifact_store.put_bytes(
                directory_payload,
                "media/{}/directory-manifest.json".format(protocol_name),
                context.attempt_id,
                media_type="application/json",
            )
            directory_artifact = {
                "logical_name": "package.directory_manifest",
                "artifact_type": "directory_manifest",
                "uri": stored_manifest["uri"],
                "media_type": stored_manifest.get("media_type"),
                "size_bytes": stored_manifest["size_bytes"],
                "checksum_sha256": stored_manifest["checksum_sha256"],
            }
            metrics = usage.aggregate()
            metrics["input_bytes"] = source_artifact.get("size_bytes")
            metrics["output_bytes"] = sum(
                item["artifact"]["size_bytes"] for item in uploaded
            )
            return {
                "validation_report": validation_payload,
                "artifacts": [main_manifest_artifact, directory_artifact],
                "metrics": metrics,
            }

    def package_hls(node, context):
        return package_playlist(node, context, "hls")

    def package_dash(node, context):
        return package_playlist(node, context, "dash")

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
        "video.hls.package": package_hls,
        "video.dash.package": package_dash,
        "video.thumbnail.generate": generate_thumbnail,
        "video.watermark.apply": apply_watermark,
    }
