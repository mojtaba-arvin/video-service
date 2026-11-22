"""Durable input-stage adapters; lease ownership stays with WorkflowNodeExecutor.

These handlers deliberately do not import Celery or use Redis/task state. Every
handoff is an immutable S3 Artifact reference, so a downstream node can run on
a different worker without depending on a producer's local filesystem.
"""
import json
import os
import tempfile

from video_streaming import settings
from video_streaming.execution.media_probe import probe_media
from video_streaming.execution.process_control import ProcessUsageAccumulator
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.errors import (
    PermanentOperationError,
    RetryableOperationError,
)


_VALIDATION = {"valid": True, "errors": []}
_JSON = "application/json"


def _parameters(node):
    parameters = node.get("parameters")
    if not isinstance(parameters, dict):
        raise PermanentOperationError("workflow node parameters are invalid")
    return parameters


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise PermanentOperationError("{} is required".format(name))
    return value


def _input_name(value):
    name = _required_text(value, "input_name")
    if name not in ("video", "watermark"):
        raise PermanentOperationError("unsupported input name")
    return name


def _check_active(context):
    if context.is_cancelled():
        context.cancel(reason="JOB_CANCELLED_DURING_INPUT_OPERATION")
        raise AttemptLeaseLost("workflow job was cancelled")
    if context.heartbeat() is False:
        raise AttemptLeaseLost("workflow attempt lease was lost")


def _provider_error(exc):
    response = getattr(exc, "response", {}) or {}
    error = response.get("Error", {}) or {}
    code = str(error.get("Code", ""))
    status = (response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
    if status in (400, 403, 404) or code in (
        "AccessDenied", "AccessDeniedException", "NoSuchKey",
        "NotFound", "NoSuchBucket", "InvalidBucketName",
    ):
        raise PermanentOperationError("input object is unavailable") from exc
    raise RetryableOperationError("input object storage request failed") from exc


def _put_json(artifact_store, value, logical_key, attempt_id):
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return artifact_store.put_bytes(
        payload, logical_key, attempt_id, media_type=_JSON
    )


def _artifact_record(logical_name, artifact_type, stored):
    return {
        "logical_name": logical_name,
        "artifact_type": artifact_type,
        "uri": stored["uri"],
        "media_type": stored.get("media_type"),
        "size_bytes": stored["size_bytes"],
        "checksum_sha256": stored["checksum_sha256"],
    }


def _find_dependency_artifact(context, logical_name):
    for artifacts in (context.input_artifacts or {}).values():
        for artifact in artifacts:
            if artifact.get("logical_name") == logical_name:
                return artifact
    raise PermanentOperationError(
        "required dependency artifact is missing: {}".format(logical_name)
    )


def _read_json_artifact(artifact_store, artifact, suffix, on_chunk=None):
    descriptor, path = tempfile.mkstemp(prefix="workflow-input-", suffix=suffix)
    os.close(descriptor)
    try:
        artifact_store.download_file(
            artifact["uri"], path,
            expected_sha256=artifact.get("checksum_sha256"),
            on_chunk=on_chunk,
        )
        with open(path, "rb") as source:
            value = json.loads(source.read().decode("utf-8"))
        if not isinstance(value, dict):
            raise PermanentOperationError("dependency artifact JSON is invalid")
        return value
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PermanentOperationError("dependency artifact JSON is invalid") from exc
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def build_input_handlers(
        s3_client,
        artifact_store,
        ffprobe_bin_path=None,
        probe_timeout_seconds=None,
        probe=probe_media):
    """Build the four implemented input handlers, not a full 13-op registry.

    The returned mapping is intentionally partial. It must not be passed to
    WorkflowNodeExecutor until the remaining nine operation adapters exist.
    """
    if s3_client is None or artifact_store is None:
        raise ValueError("S3 client and Artifact Store are required")
    ffprobe_bin_path = ffprobe_bin_path or settings.FFPROBE_BIN_PATH
    probe_timeout_seconds = (
        probe_timeout_seconds or settings.FFPROBE_TIMEOUT_SECONDS
    )

    def check_input(node, context):
        params = _parameters(node)
        name = _input_name(params.get("input_name"))
        bucket = _required_text(params.get("bucket"), "bucket")
        key = _required_text(params.get("key"), "key")
        _check_active(context)
        try:
            details = s3_client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            _provider_error(exc)
        size = details.get("ContentLength")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise PermanentOperationError("input object is empty or has invalid size")
        etag = details.get("ETag")
        version_id = details.get("VersionId")
        if version_id in (None, "", "null") and (not isinstance(etag, str) or not etag):
            raise PermanentOperationError(
                "input object has no stable version or ETag validator"
            )
        reference = {
            "input_name": name,
            "bucket": bucket,
            "key": key,
            "size_bytes": size,
            "etag": etag,
            "version_id": version_id,
            "content_type": details.get("ContentType"),
            "last_modified": (
                details["LastModified"].isoformat()
                if details.get("LastModified") is not None else None
            ),
        }
        stored = _put_json(
            artifact_store, reference,
            "inputs/{}/object-reference.json".format(name),
            context.attempt_id,
        )
        return {
            "validation_report": dict(_VALIDATION),
            "artifacts": [_artifact_record(
                "input.{}.object_ref".format(name), "input_object_ref", stored
            )],
            "metrics": {"input_bytes": size},
        }

    def download_input(node, context):
        params = _parameters(node)
        name = _required_text(params.get("input_name"), "input_name")
        bucket = _required_text(params.get("bucket"), "bucket")
        key = _required_text(params.get("key"), "key")
        reference_artifact = _find_dependency_artifact(
            context, "input.{}.object_ref".format(name)
        )
        reference = _read_json_artifact(
            artifact_store, reference_artifact, ".json",
            on_chunk=lambda _size: _check_active(context),
        )
        if (
            reference.get("input_name") != name
            or reference.get("bucket") != bucket
            or reference.get("key") != key
        ):
            raise PermanentOperationError("input object reference does not match the plan")
        descriptor, local_path = tempfile.mkstemp(prefix="workflow-source-")
        try:
            digest_size = 0
            body = None
            try:
                get_args = {"Bucket": bucket, "Key": key}
                version_id = reference.get("version_id")
                if version_id not in (None, "", "null"):
                    get_args["VersionId"] = version_id
                else:
                    etag = reference.get("etag")
                    if not isinstance(etag, str) or not etag:
                        raise PermanentOperationError(
                            "input object reference has no stable download validator"
                        )
                    get_args["IfMatch"] = etag
                response = s3_client.get_object(**get_args)
                body = response["Body"]
                with os.fdopen(descriptor, "wb") as target:
                    descriptor = None
                    while True:
                        _check_active(context)
                        chunk = body.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                        digest_size += len(chunk)
            except AttemptLeaseLost:
                raise
            except Exception as exc:
                _provider_error(exc)
            finally:
                close = getattr(body, "close", None)
                if close:
                    close()
            if digest_size != reference["size_bytes"]:
                raise RetryableOperationError("downloaded input size changed during transfer")
            _check_active(context)
            stored = artifact_store.put_file(
                local_path,
                "inputs/{}/source.bin".format(name),
                context.attempt_id,
                media_type=reference.get("content_type") or "application/octet-stream",
                on_chunk=lambda _size: _check_active(context),
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(local_path)
            except OSError:
                pass
        return {
            "validation_report": dict(_VALIDATION),
            "artifacts": [_artifact_record(
                "input.{}.file_artifact".format(name), "media_file", stored
            )],
            "metrics": {"input_bytes": stored["size_bytes"]},
        }

    def analyze_input(node, context):
        params = _parameters(node)
        name = _required_text(params.get("input_name"), "input_name")
        source = _find_dependency_artifact(
            context, "input.{}.file_artifact".format(name)
        )
        usage = ProcessUsageAccumulator()
        if name == "video":
            descriptor, local_path = tempfile.mkstemp(prefix="workflow-probe-")
            os.close(descriptor)

            def on_usage(sample):
                usage.record(sample)
                _check_active(context)

            try:
                artifact_store.download_file(
                    source["uri"], local_path,
                    expected_sha256=source.get("checksum_sha256"),
                    on_chunk=lambda _size: _check_active(context),
                )
                _check_active(context)
                metadata = probe(
                    local_path,
                    ffprobe_bin_path,
                    probe_timeout_seconds,
                    should_cancel=context.is_cancelled,
                    on_usage=on_usage,
                )
                streams = metadata.get("streams", [])
                if not any(
                    stream.get("codec_type") == "video" for stream in streams
                ):
                    raise PermanentOperationError(
                        "primary input contains no video stream"
                    )
            finally:
                try:
                    os.unlink(local_path)
                except OSError:
                    pass
        elif name == "watermark":
            # The legacy pipeline intentionally defers watermark validation
            # (including SVG handling) to the FFmpeg watermark operation.
            # Do not introduce an FFprobe-only format restriction here.
            _check_active(context)
            metadata = {
                "input_name": "watermark",
                "inspection": "deferred_to_ffmpeg",
            }
        else:
            raise PermanentOperationError("unsupported input name")
        stored = _put_json(
            artifact_store, metadata,
            "inputs/{}/probe.json".format(name),
            context.attempt_id,
        )
        metrics = usage.aggregate()
        metrics["input_bytes"] = source["size_bytes"]
        artifacts = [
            dict(source),
            _artifact_record(
                "input.{}.probe_metadata".format(name),
                "probe_metadata",
                stored,
            ),
        ]
        return {
            "validation_report": dict(_VALIDATION),
            "artifacts": artifacts,
            "metrics": metrics,
        }

    def inputs_funnel(node, context):
        del node
        _check_active(context)
        inputs = {}
        for artifacts in (context.input_artifacts or {}).values():
            for artifact in artifacts:
                logical_name = artifact.get("logical_name", "")
                if not logical_name.startswith("input.") or not logical_name.endswith(
                    ".probe_metadata"
                ):
                    continue
                name = logical_name[len("input."):-len(".probe_metadata")]
                if name in inputs:
                    raise PermanentOperationError("duplicate input probe metadata")
                metadata = _read_json_artifact(
                    artifact_store, artifact, ".json",
                    on_chunk=lambda _size: _check_active(context),
                )
                file_artifact = _find_dependency_artifact(
                    context, "input.{}.file_artifact".format(name)
                )
                inputs[name] = {
                    "file_artifact": dict(file_artifact),
                    "probe_metadata": metadata,
                }
        if "video" not in inputs:
            raise PermanentOperationError("primary video input is missing")
        manifest = {
            "schema_version": 1,
            "job_id": context.job_id,
            "inputs": inputs,
        }
        _check_active(context)
        stored = _put_json(
            artifact_store, manifest, "inputs/manifest.json", context.attempt_id
        )
        return {
            "validation_report": dict(_VALIDATION),
            "artifacts": [_artifact_record(
                "inputs.manifest", "input_manifest", stored
            )],
            "metrics": {"input_count": len(inputs)},
        }

    return {
        "video.input.check": check_input,
        "video.input.download": download_input,
        "video.input.analyze": analyze_input,
        "video.inputs.funnel": inputs_funnel,
    }
