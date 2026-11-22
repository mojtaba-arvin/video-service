"""Durable S3 output preflight handlers.

The workflow Executor owns the Attempt lease. These adapters perform only
bounded S3 control-plane calls and publish immutable preflight receipts; they
never touch Celery state or claim a second Attempt.
"""
import json

from video_streaming import settings
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.errors import (
    PermanentOperationError,
    RetryableOperationError,
)


_VALIDATION = {"valid": True, "errors": []}
_JSON = "application/json"


def _required_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise PermanentOperationError("{} is required".format(name))
    return value.strip()


def _check_active(context):
    if context.is_cancelled():
        context.cancel(reason="JOB_CANCELLED_DURING_OUTPUT_PREFLIGHT")
        raise AttemptLeaseLost("workflow job was cancelled")
    if context.heartbeat() is False:
        raise AttemptLeaseLost("workflow attempt lease was lost")


def _classify_s3_error(exc, *, missing_is_permanent=True):
    response = getattr(exc, "response", {}) or {}
    metadata = response.get("ResponseMetadata", {}) or {}
    error = response.get("Error", {}) or {}
    status = metadata.get("HTTPStatusCode")
    code = str(error.get("Code", ""))
    if status == 404 or code in ("404", "NoSuchBucket", "NotFound"):
        if missing_is_permanent:
            raise PermanentOperationError("output bucket or object does not exist") from exc
        return "missing"
    if status in (401, 403) or code in (
        "AccessDenied", "AccessDeniedException", "AllAccessDisabled"
    ):
        raise PermanentOperationError("S3 access to output resource is denied") from exc
    if status in (400, 405) or code in (
        "InvalidBucketName", "InvalidAccessKeyId", "SignatureDoesNotMatch"
    ):
        raise PermanentOperationError("S3 rejected output resource parameters") from exc
    raise RetryableOperationError("S3 output preflight request failed") from exc


def _publish_receipt(artifact_store, context, logical_name, artifact_type, value):
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    stored = artifact_store.put_bytes(
        payload,
        "preflight/{}.json".format(logical_name.replace(".", "/")),
        context.attempt_id,
        media_type=_JSON,
    )
    return {
        "logical_name": logical_name,
        "artifact_type": artifact_type,
        "uri": stored["uri"],
        "media_type": stored.get("media_type"),
        "size_bytes": stored["size_bytes"],
        "checksum_sha256": stored["checksum_sha256"],
    }


def build_output_preflight_handlers(
        s3_client, artifact_store, region_name=None):
    """Return real bucket/key preflight handlers; not a complete registry."""
    if s3_client is None or artifact_store is None:
        raise ValueError("S3 client and Artifact Store are required")
    region_name = region_name or settings.S3_REGION_NAME

    def check_output_bucket(node, context):
        params = node.get("parameters")
        if not isinstance(params, dict):
            raise PermanentOperationError("workflow node parameters are invalid")
        bucket = _required_text(params.get("bucket"), "bucket")
        create_bucket = params.get("create_bucket", False)
        if not isinstance(create_bucket, bool):
            raise PermanentOperationError("create_bucket must be a boolean")
        _check_active(context)
        try:
            s3_client.head_bucket(Bucket=bucket)
            exists = True
        except Exception as exc:
            missing = _classify_s3_error(exc, missing_is_permanent=False)
            if missing != "missing":
                raise
            exists = False
        created = False
        if not exists:
            if not create_bucket:
                raise PermanentOperationError("output bucket does not exist")
            _check_active(context)
            create_args = {"Bucket": bucket}
            # us-east-1 rejects an explicit LocationConstraint.
            if region_name and region_name != "us-east-1":
                create_args["CreateBucketConfiguration"] = {
                    "LocationConstraint": region_name
                }
            try:
                s3_client.create_bucket(**create_args)
                created = True
            except Exception as exc:
                response = getattr(exc, "response", {}) or {}
                code = (response.get("Error", {}) or {}).get("Code")
                status = (response.get("ResponseMetadata", {}) or {}).get(
                    "HTTPStatusCode"
                )
                # A race is acceptable only after a fresh HEAD proves that
                # this caller can access the bucket. Never swallow global-name
                # conflicts or an ownership/access denial.
                if code in ("BucketAlreadyOwnedByYou",):
                    created = False
                elif code in ("BucketAlreadyExists", "AccessDenied") or status == 403:
                    raise PermanentOperationError(
                        "output bucket name is unavailable or not owned"
                    ) from exc
                else:
                    raise RetryableOperationError(
                        "output bucket creation failed"
                    ) from exc
                _check_active(context)
                try:
                    s3_client.head_bucket(Bucket=bucket)
                except Exception as head_exc:
                    _classify_s3_error(head_exc)
        _check_active(context)
        receipt = {
            "bucket": bucket,
            "ready": True,
            "created_by_attempt": created,
            "region": region_name,
        }
        artifact = _publish_receipt(
            artifact_store, context,
            "output.bucket_ready.{}".format(bucket),
            "output_bucket_ready",
            receipt,
        )
        return {
            "validation_report": dict(_VALIDATION),
            "artifacts": [artifact],
            "metrics": {},
        }

    def check_output_key(node, context):
        params = node.get("parameters")
        if not isinstance(params, dict):
            raise PermanentOperationError("workflow node parameters are invalid")
        bucket = _required_text(params.get("bucket"), "bucket")
        key = _required_text(params.get("key"), "key")
        dont_replace = params.get("dont_replace", True)
        if not isinstance(dont_replace, bool):
            raise PermanentOperationError("dont_replace must be a boolean")
        if not dont_replace:
            raise PermanentOperationError(
                "key preflight is only valid when dont_replace is enabled"
            )
        _check_active(context)
        try:
            details = s3_client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            _classify_s3_error(exc, missing_is_permanent=False)
            # A 404 proves absence; all other errors are classified above.
            details = None
        if details is not None:
            raise PermanentOperationError(
                "output key already exists and replacement is disabled"
            )
        _check_active(context)
        receipt = {
            "bucket": bucket,
            "key": key,
            "available": True,
        }
        artifact = _publish_receipt(
            artifact_store, context,
            "output.key_available.{}".format(context.node_id),
            "output_key_available",
            receipt,
        )
        return {
            "validation_report": dict(_VALIDATION),
            "artifacts": [artifact],
            "metrics": {},
        }

    return {
        "video.output.bucket.check": check_output_bucket,
        "video.output.key.check": check_output_key,
    }
