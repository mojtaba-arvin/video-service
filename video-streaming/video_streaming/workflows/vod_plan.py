"""Build a complete, transport-neutral plan for the legacy VOD request shape.

The input is a normalized mapping owned by a transport adapter. This module
must not import protobuf, Celery, storage SDKs, or FFmpeg task signatures.
"""

import math
from copy import deepcopy

from video_streaming.workflows.execution_plan import (
    EXECUTION_PLAN_SCHEMA_VERSION,
    validate_execution_plan,
)


DEFAULT_RETRY_POLICIES = {
    "control": {
        "max_attempts": 2,
        "backoff": "exponential",
        "initial_delay_seconds": 1,
        "max_delay_seconds": 15,
    },
    "input": {
        "max_attempts": 3,
        "backoff": "exponential",
        "initial_delay_seconds": 2,
        "max_delay_seconds": 60,
    },
    "media": {
        "max_attempts": 2,
        "backoff": "exponential",
        "initial_delay_seconds": 2,
        "max_delay_seconds": 60,
    },
    "upload": {
        "max_attempts": 3,
        "backoff": "exponential",
        "initial_delay_seconds": 2,
        "max_delay_seconds": 120,
    },
    "callback": {
        "max_attempts": 5,
        "backoff": "exponential",
        "initial_delay_seconds": 2,
        "max_delay_seconds": 300,
    },
}


class VODPlanBuildError(ValueError):
    """Raised when a normalized VOD request cannot form a safe execution DAG."""


def _required_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise VODPlanBuildError("{} must be a non-empty string".format(field))
    return value


def _optional_text(value, field):
    if value is None or value == "":
        return None
    return _required_text(value, field)


def _as_bool(value, field):
    if not isinstance(value, bool):
        raise VODPlanBuildError("{} must be a boolean".format(field))
    return value


def _node(
        node_id,
        operation,
        dependencies,
        parameters,
        retry_policy,
        resource_profile):
    return {
        "node_id": node_id,
        "operation": operation,
        "stage_key": node_id,
        "depends_on": list(dependencies),
        "parameters": parameters,
        "retry_policy": deepcopy(DEFAULT_RETRY_POLICIES[retry_policy]),
        "resource_profile": resource_profile,
    }


def _normalize_output(output, kind, index):
    if not isinstance(output, dict):
        raise VODPlanBuildError(
            "{}[{}] must be an object".format(kind, index)
        )
    bucket = _required_text(output.get("bucket"), "{}[{}].bucket".format(kind, index))
    key = _required_text(output.get("key"), "{}[{}].key".format(kind, index))
    use_watermark = _as_bool(
        output.get("use_watermark", False),
        "{}[{}].use_watermark".format(kind, index),
    )
    dont_replace = _as_bool(
        output.get("dont_replace", False),
        "{}[{}].dont_replace".format(kind, index),
    )
    create_bucket = _as_bool(
        output.get("create_bucket", False),
        "{}[{}].create_bucket".format(kind, index),
    )
    normalized = {
        "bucket": bucket,
        "key": key,
        "use_watermark": use_watermark,
        "dont_replace": dont_replace,
        "create_bucket": create_bucket,
    }
    if kind == "playlists":
        protocol = _required_text(
            output.get("protocol"), "{}[{}].protocol".format(kind, index)
        ).lower()
        if protocol not in ("hls", "dash"):
            raise VODPlanBuildError(
                "{}[{}].protocol must be hls or dash".format(kind, index)
            )
        quality_names = output.get("quality_names", [])
        if not isinstance(quality_names, list) or any(
                not isinstance(item, str) or not item.strip()
                for item in quality_names):
            raise VODPlanBuildError(
                "{}[{}].quality_names must be a list of non-empty strings"
                .format(kind, index)
            )
        normalized.update({
            "protocol": protocol,
            "quality_names": list(quality_names),
            "encode_format": output.get("encode_format"),
            "video_codec": output.get("video_codec"),
            "audio_codec": output.get("audio_codec"),
            "fragmented": _as_bool(output.get("fragmented", False), "{}[{}].fragmented".format(kind, index)),
            "custom_qualities": deepcopy(
                output.get("custom_qualities", [])
            ),
        })
    else:
        thumbnail_time = output.get("thumbnail_time")
        if (
            isinstance(thumbnail_time, bool)
            or not isinstance(thumbnail_time, (int, float))
            or not math.isfinite(thumbnail_time)
            or thumbnail_time < 0
        ):
            raise VODPlanBuildError(
                "{}[{}].thumbnail_time must be non-negative".format(kind, index)
            )
        for field in ("scale_width", "scale_height"):
            value = output.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise VODPlanBuildError(
                    "{}[{}].{} must be a non-negative integer".format(
                        kind, index, field
                    )
                )
        normalized.update({
            "thumbnail_time": thumbnail_time,
            "scale_width": output.get("scale_width", 0),
            "scale_height": output.get("scale_height", 0),
        })
    return normalized


def build_vod_execution_plan(
        job_id,
        request,
        workflow_version="1",
        webhook_secret_ref=None):
    """Build and validate a complete DAG from a normalized VOD request.

    Expected request shape:
      video: {bucket, key}
      watermark: optional {input_bucket, input_key, output_bucket, output_key}
      playlists: [{bucket, key, protocol, quality_names, use_watermark, ...}]
      thumbnails: [{bucket, key, thumbnail_time, scale_width, scale_height, ...}]

    The webhook reference is opaque and must already have been stored by a
    durable secret service. Raw webhook URLs are intentionally not accepted.
    """
    if not isinstance(request, dict):
        raise VODPlanBuildError("request must be an object")
    video = request.get("video")
    if not isinstance(video, dict):
        raise VODPlanBuildError("video must be an object")
    video_bucket = _required_text(video.get("bucket"), "video.bucket")
    video_key = _required_text(video.get("key"), "video.key")

    watermark = request.get("watermark") or {}
    if not isinstance(watermark, dict):
        raise VODPlanBuildError("watermark must be an object")
    watermark_input_bucket = _optional_text(
        watermark.get("input_bucket"), "watermark.input_bucket"
    )
    watermark_input_key = _optional_text(
        watermark.get("input_key"), "watermark.input_key"
    )
    if bool(watermark_input_bucket) != bool(watermark_input_key):
        raise VODPlanBuildError(
            "watermark input bucket and key must be provided together"
        )
    watermark_output_bucket = _optional_text(
        watermark.get("output_bucket"), "watermark.output_bucket"
    )
    watermark_output_key = _optional_text(
        watermark.get("output_key"), "watermark.output_key"
    )
    if bool(watermark_output_bucket) != bool(watermark_output_key):
        raise VODPlanBuildError(
            "watermark output bucket and key must be provided together"
        )
    if watermark_output_key and not watermark_input_key:
        raise VODPlanBuildError(
            "watermarked video output requires a watermark input"
        )

    playlists_raw = request.get("playlists", [])
    thumbnails_raw = request.get("thumbnails", [])
    if not isinstance(playlists_raw, list) or not isinstance(thumbnails_raw, list):
        raise VODPlanBuildError("playlists and thumbnails must be lists")
    playlists = [
        _normalize_output(item, "playlists", index)
        for index, item in enumerate(playlists_raw)
    ]
    thumbnails = [
        _normalize_output(item, "thumbnails", index)
        for index, item in enumerate(thumbnails_raw)
    ]
    outputs = playlists + thumbnails
    if watermark_output_key:
        outputs.append({
            "bucket": watermark_output_bucket,
            "key": watermark_output_key,
            "use_watermark": True,
            "dont_replace": _as_bool(
                watermark.get("dont_replace", False),
                "watermark.dont_replace",
            ),
            "create_bucket": _as_bool(
                watermark.get("create_bucket", False),
                "watermark.create_bucket",
            ),
        })
    if not outputs:
        raise VODPlanBuildError("at least one output is required")

    if any(item["use_watermark"] for item in playlists + thumbnails):
        if not watermark_input_key:
            raise VODPlanBuildError(
                "watermarked outputs require a watermark input"
            )

    locations = [(item["bucket"], item["key"]) for item in outputs]
    if len(locations) != len(set(locations)):
        raise VODPlanBuildError("duplicate output locations are not allowed")

    _required_text(job_id, "job_id")
    _required_text(workflow_version, "workflow_version")
    if webhook_secret_ref is not None:
        _required_text(webhook_secret_ref, "webhook_secret_ref")
        if len(webhook_secret_ref) > 512:
            raise VODPlanBuildError("webhook_secret_ref exceeds 512 characters")
        if webhook_secret_ref.lower().startswith(("http://", "https://")):
            raise VODPlanBuildError(
                "webhook_secret_ref must be an opaque secret reference, not a URL"
            )

    nodes = []
    input_checks = []
    input_specs = [("video", 0, video_bucket, video_key)]
    if watermark_input_key:
        input_specs.append((
            "watermark", 1, watermark_input_bucket, watermark_input_key
        ))
    for name, number, bucket, key in input_specs:
        node_id = "input-{}-check".format(name)
        nodes.append(_node(
            node_id, "video.input.check", [], {
                "input_name": name,
                "input_number": number,
                "bucket": bucket,
                "key": key,
            }, "control", "control",
        ))
        input_checks.append(node_id)

    unique_buckets = sorted({item["bucket"] for item in outputs})
    bucket_checks = []
    for index, bucket in enumerate(unique_buckets):
        create_bucket = any(
            item["bucket"] == bucket and item["create_bucket"]
            for item in outputs
        )
        node_id = "output-bucket-{}-check".format(index)
        nodes.append(_node(
            node_id, "video.output.bucket.check", [], {
                "bucket": bucket,
                "create_bucket": create_bucket,
            }, "control", "control",
        ))
        bucket_checks.append(node_id)

    key_checks = []
    for kind, items in (("playlist", playlists), ("thumbnail", thumbnails)):
        for index, item in enumerate(items):
            if not item["dont_replace"]:
                continue
            node_id = "output-{}-{}-key-check".format(kind, index)
            nodes.append(_node(
                node_id, "video.output.key.check", [], {
                    "bucket": item["bucket"],
                    "key": item["key"],
                    "dont_replace": True,
                }, "control", "control",
            ))
            key_checks.append(node_id)
    if watermark_output_key and outputs[-1]["dont_replace"]:
        nodes.append(_node(
            "output-watermarked-video-key-check",
            "video.output.key.check", [], {
                "bucket": watermark_output_bucket,
                "key": watermark_output_key,
                "dont_replace": True,
            }, "control", "control",
        ))
        key_checks.append("output-watermarked-video-key-check")

    preflight_dependencies = input_checks + bucket_checks + key_checks
    analyze_nodes = []
    for name, number, bucket, key in input_specs:
        download_id = "input-{}-download".format(name)
        analyze_id = "input-{}-analyze".format(name)
        nodes.append(_node(
            download_id, "video.input.download", preflight_dependencies, {
                "input_name": name,
                "input_number": number,
                "bucket": bucket,
                "key": key,
            }, "input", "upload",
        ))
        nodes.append(_node(
            analyze_id, "video.input.analyze", [download_id], {
                "input_name": name,
                "input_number": number,
            }, "media", "probe",
        ))
        analyze_nodes.append(analyze_id)

    nodes.append(_node(
        "inputs-funnel", "video.inputs.funnel", analyze_nodes, {},
        "control", "control",
    ))
    base_dependency = "inputs-funnel"
    watermark_dependency = None
    needs_watermark = bool(
        watermark_input_key and (
            watermark_output_key
            or any(item["use_watermark"] for item in playlists + thumbnails)
        )
    )
    if needs_watermark:
        watermark_dependency = "watermark-apply"
        nodes.append(_node(
            watermark_dependency, "video.watermark.apply", [base_dependency], {
                "watermark_input_number": 1,
                "watermark_output_key": watermark_output_key,
                "watermark_output_bucket": watermark_output_bucket,
            }, "media", "transcode",
        ))

    callback_enabled = webhook_secret_ref is not None

    def append_callback(prefix, dependency):
        callback_id = "{}-webhook".format(prefix)
        parameters = {"enabled": callback_enabled}
        if webhook_secret_ref is not None:
            parameters["webhook_secret_ref"] = webhook_secret_ref
        nodes.append(_node(
            callback_id, "video.webhook.deliver", [dependency], parameters,
            "callback", "control",
        ))

    for index, item in enumerate(playlists):
        prefix = "playlist-{}".format(index)
        dependency = (
            watermark_dependency if item["use_watermark"] else base_dependency
        )
        operation = (
            "video.hls.package" if item["protocol"] == "hls"
            else "video.dash.package"
        )
        package_id = "{}-package".format(prefix)
        upload_id = "{}-upload".format(prefix)
        nodes.append(_node(
            package_id, operation, [dependency], {
                "bucket": item["bucket"],
                "key": item["key"],
                "protocol": item["protocol"],
                "quality_names": item["quality_names"],
                "encode_format": item["encode_format"],
                "video_codec": item["video_codec"],
                "audio_codec": item["audio_codec"],
                "fragmented": item["fragmented"],
                "custom_qualities": item["custom_qualities"],
                "use_watermark": item["use_watermark"],
            }, "media", "package",
        ))
        nodes.append(_node(
            upload_id, "video.output.upload_directory", [package_id], {
                "bucket": item["bucket"],
                "key": item["key"],
                "output_id": prefix,
                "protocol": item["protocol"],
            }, "upload", "upload",
        ))
        append_callback(prefix, upload_id)

    for index, item in enumerate(thumbnails):
        prefix = "thumbnail-{}".format(index)
        dependency = (
            watermark_dependency if item["use_watermark"] else base_dependency
        )
        generate_id = "{}-generate".format(prefix)
        upload_id = "{}-upload".format(prefix)
        nodes.append(_node(
            generate_id, "video.thumbnail.generate", [dependency], {
                "bucket": item["bucket"],
                "key": item["key"],
                "thumbnail_time": item["thumbnail_time"],
                "scale_width": item["scale_width"],
                "scale_height": item["scale_height"],
                "use_watermark": item["use_watermark"],
            }, "media", "thumbnail",
        ))
        nodes.append(_node(
            upload_id, "video.output.upload_file", [generate_id], {
                "bucket": item["bucket"],
                "key": item["key"],
                "output_id": prefix,
            }, "upload", "upload",
        ))
        append_callback(prefix, upload_id)

    if watermark_output_key:
        upload_id = "watermarked-video-upload"
        nodes.append(_node(
            upload_id, "video.output.upload_file", [watermark_dependency or base_dependency], {
                "bucket": watermark_output_bucket,
                "key": watermark_output_key,
                "output_id": "watermarked-video",
            }, "upload", "upload",
        ))
        append_callback("watermarked-video", upload_id)

    plan = {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "workflow_name": "video.vod",
        "workflow_version": workflow_version,
        "job_id": job_id,
        "nodes": nodes,
    }
    return validate_execution_plan(plan)
