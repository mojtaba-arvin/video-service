"""Durable S3 publication and callback delivery adapters.

The WorkflowNodeExecutor exclusively owns Attempt leases. Output objects are
published from immutable Artifact Store inputs; package dependencies are
written under attempt-versioned keys and the player-facing manifest is the
last write. Webhook delivery uses the executor's stable idempotency key.
"""
import hashlib
import json
import mimetypes
import os
import posixpath
import re
import tempfile
import xml.etree.ElementTree as ET

from video_streaming import settings
from video_streaming.ffmpeg.webhook_transport import (
    WebhookDestinationError,
    WebhookTransportError,
    request_webhook,
)
from video_streaming.workflows.domain import AttemptLeaseLost
from video_streaming.workflows.errors import (
    PermanentOperationError,
    RetryableOperationError,
)
from video_streaming.workflows.artifact_store import artifact_uri
from video_streaming.security.secret_store import SecretContextMismatch, SecretDecryptionError


_VALIDATION = {"valid": True, "errors": []}
_CHUNK_SIZE = 8 * 1024 * 1024
_MULTIPART_THRESHOLD = 64 * 1024 * 1024
_MAX_PARTS = 10000


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
        context.cancel(reason="JOB_CANCELLED_DURING_PUBLICATION")
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


def _artifact_metadata(artifact):
    if (
        not isinstance(artifact, dict)
        or not isinstance(artifact.get("uri"), str)
        or not isinstance(artifact.get("checksum_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", artifact["checksum_sha256"])
    ):
        raise PermanentOperationError("dependency artifact reference is invalid")
    return artifact


def _stable_publication_key(key, idempotency_key):
    token = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return key + ".workflow-attempts/" + token


def _upload_file(
        s3_client, bucket, key, path, attempt_id, idempotency_key,
        checksum, media_type, dont_replace, context):
    size = os.path.getsize(path)
    metadata = {
        "sha256": checksum,
        "attempt_id": attempt_id,
        "idempotency_key": idempotency_key,
    }
    extra = {
        "Bucket": bucket,
        "Key": key,
        "Metadata": metadata,
        "ContentType": media_type or "application/octet-stream",
    }
    if dont_replace:
        extra["IfNoneMatch"] = "*"

    def progress(_amount):
        _check_active(context)

    try:
        if size < _MULTIPART_THRESHOLD:
            with open(path, "rb") as source:
                body = _ProgressReader(source, progress)
                s3_client.put_object(Body=body, **extra)
        else:
            _multipart_upload(
                s3_client, path, extra, size, dont_replace, progress
            )
    except AttemptLeaseLost:
        raise
    except Exception as exc:
        if dont_replace and _is_precondition_failure(exc):
            existing = s3_client.head_object(Bucket=bucket, Key=key)
            stored_metadata = existing.get("Metadata", {})
            if (
                int(existing.get("ContentLength", -1)) == size
                and stored_metadata.get("sha256") == checksum
                and stored_metadata.get("idempotency_key") == idempotency_key
            ):
                _check_active(context)
                return {
                    "bucket": bucket, "key": key, "size_bytes": size,
                    "checksum_sha256": checksum, "media_type": media_type,
                    "replayed": True,
                }
            raise PermanentOperationError(
                "output key already exists with different content"
            ) from exc
        if _is_permanent_s3_error(exc):
            raise PermanentOperationError("S3 output publication was rejected") from exc
        raise RetryableOperationError("S3 output publication failed") from exc

    _check_active(context)
    try:
        head = s3_client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise RetryableOperationError("published output is not visible") from exc
    if (
        int(head.get("ContentLength", -1)) != size
        or head.get("Metadata", {}).get("sha256") != checksum
        or head.get("Metadata", {}).get("attempt_id") != attempt_id
    ):
        raise RetryableOperationError("published output read-back did not match")
    return {
        "bucket": bucket, "key": key, "size_bytes": size,
        "checksum_sha256": checksum, "media_type": media_type,
    }


class _ProgressReader:
    def __init__(self, source, callback):
        self._source = source
        self._callback = callback

    def read(self, size=-1):
        chunk = self._source.read(size)
        if chunk:
            self._callback(len(chunk))
        return chunk

    def __getattr__(self, name):
        return getattr(self._source, name)


def _multipart_upload(s3_client, path, params, size, dont_replace, progress):
    created = s3_client.create_multipart_upload(
        Bucket=params["Bucket"],
        Key=params["Key"],
        Metadata=params["Metadata"],
        ContentType=params["ContentType"],
    )
    upload_id = created["UploadId"]
    parts = []
    try:
        with open(path, "rb") as source:
            part_number = 1
            while True:
                chunk = source.read(_CHUNK_SIZE)
                if not chunk:
                    break
                if part_number > _MAX_PARTS:
                    raise PermanentOperationError("output exceeds S3 multipart limits")
                response = s3_client.upload_part(
                    Bucket=params["Bucket"],
                    Key=params["Key"],
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )
                parts.append({
                    "PartNumber": part_number,
                    "ETag": response["ETag"],
                })
                part_number += 1
                progress(len(chunk))
        complete_args = {
            "Bucket": params["Bucket"],
            "Key": params["Key"],
            "UploadId": upload_id,
            "MultipartUpload": {"Parts": parts},
        }
        if dont_replace:
            complete_args["IfNoneMatch"] = "*"
        s3_client.complete_multipart_upload(**complete_args)
    except Exception:
        try:
            s3_client.abort_multipart_upload(
                Bucket=params["Bucket"], Key=params["Key"], UploadId=upload_id
            )
        except Exception:
            pass
        raise


def _is_precondition_failure(exc):
    response = getattr(exc, "response", {}) or {}
    return (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412
        or response.get("Error", {}).get("Code") == "PreconditionFailed"
    )


def _is_permanent_s3_error(exc):
    response = getattr(exc, "response", {}) or {}
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = response.get("Error", {}).get("Code")
    return status in (400, 401, 403) or code in (
        "AccessDenied", "InvalidAccessKeyId", "NoSuchBucket",
        "InvalidBucketName", "InvalidArgument",
    )


def _safe_relative_path(value):
    if (
        not isinstance(value, str) or not value or value.startswith("/")
        or "\\" in value or any(part in ("", ".", "..") for part in value.split("/"))
    ):
        raise PermanentOperationError("package contains an unsafe relative path")
    return value


def _rewrite_hls_manifest(payload, version_relative_prefix):
    text = payload.decode("utf-8")
    output = []
    uri_pattern = re.compile(r'(URI=")([^"]+)(")')
    for line in text.splitlines():
        if line and not line.startswith("#"):
            line = posixpath.join(version_relative_prefix, line)
        elif line.startswith("#"):
            line = uri_pattern.sub(
                lambda match: match.group(1) + posixpath.join(
                    version_relative_prefix, match.group(2)
                ) + match.group(3),
                line,
            )
        output.append(line)
    return ("\n".join(output) + "\n").encode("utf-8")


def _rewrite_dash_manifest(payload, version_relative_prefix):
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise PermanentOperationError("DASH manifest XML is invalid") from exc
    namespace = ""
    if root.tag.startswith("{"):
        namespace = root.tag.split("}", 1)[0] + "}"
    base = ET.Element(namespace + "BaseURL")
    base.text = version_relative_prefix.rstrip("/") + "/"
    root.insert(0, base)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def build_publication_handlers(
        s3_client,
        artifact_store,
        repository,
        webhook_secret_resolver=None,
        webhook_requester=request_webhook):
    """Build upload_file, upload_directory and webhook delivery adapters."""
    if s3_client is None or artifact_store is None or repository is None:
        raise ValueError("S3 client, Artifact Store and repository are required")

    def upload_file(node, context):
        params = _parameters(node)
        bucket = _required_text(params.get("bucket"), "bucket")
        key = _required_text(params.get("key"), "key")
        dont_replace = params.get("dont_replace", False)
        if not isinstance(dont_replace, bool):
            raise PermanentOperationError("dont_replace must be a boolean")
        candidates = [
            name for name in ("thumbnail.image", "watermark.video")
            if any(
                item.get("logical_name") == name
                for values in (context.input_artifacts or {}).values()
                for item in values
            )
        ]
        if len(candidates) != 1:
            raise PermanentOperationError(
                "file upload requires exactly one thumbnail or watermark artifact"
            )
        source = _artifact_metadata(_find_artifact(context, candidates[0]))
        with tempfile.TemporaryDirectory(prefix="workflow-upload-file-") as directory:
            path = os.path.join(directory, "source")
            artifact_store.download_file(
                source["uri"], path,
                expected_sha256=source["checksum_sha256"],
                on_chunk=lambda _size: _check_active(context),
            )
            _check_active(context)
            media_type = source.get("media_type") or mimetypes.guess_type(key)[0]
            publication_key = (
                key if dont_replace
                else _stable_publication_key(key, context.idempotency_key)
            )
            published = _upload_file(
                s3_client, bucket, publication_key, path, context.attempt_id,
                context.idempotency_key, source["checksum_sha256"],
                media_type, dont_replace, context,
            )
        record = {
            "logical_name": "output.published_object",
            "artifact_type": "published_object",
            "uri": artifact_uri(bucket, published["key"]),
            "media_type": published["media_type"],
            "size_bytes": published["size_bytes"],
            "checksum_sha256": published["checksum_sha256"],
            "metadata": {
                "attempt_id": context.attempt_id,
                "dont_replace": dont_replace,
                "readback_verified": True,
                "replayed": published.get("replayed", False),
            },
        }
        return {
            "validation_report": {
                "valid": True, "bucket": bucket, "key": key, "errors": [],
            },
            "artifacts": [record],
            "metrics": {
                "input_bytes": source.get("size_bytes"),
                "output_bytes": published["size_bytes"],
                "artifact_count": 1,
            },
        }

    def upload_directory(node, context):
        params = _parameters(node)
        bucket = _required_text(params.get("bucket"), "bucket")
        manifest_key = _required_text(params.get("key"), "key")
        protocol = _required_text(params.get("protocol"), "protocol").lower()
        if protocol not in ("hls", "dash"):
            raise PermanentOperationError("package protocol is invalid")
        dont_replace = params.get("dont_replace", False)
        if not isinstance(dont_replace, bool):
            raise PermanentOperationError("dont_replace must be a boolean")
        directory_record = _artifact_metadata(
            _find_artifact(context, "package.directory_manifest")
        )
        descriptor, manifest_path = tempfile.mkstemp(
            prefix="workflow-package-manifest-", suffix=".json"
        )
        os.close(descriptor)
        try:
            artifact_store.download_file(
                directory_record["uri"], manifest_path,
                expected_sha256=directory_record["checksum_sha256"],
                on_chunk=lambda _size: _check_active(context),
            )
            with open(manifest_path, "rb") as source:
                package_manifest = json.loads(source.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermanentOperationError("package directory manifest is invalid") from exc
        finally:
            try:
                os.unlink(manifest_path)
            except OSError:
                pass
        if (
            not isinstance(package_manifest, dict)
            or package_manifest.get("schema_version") != 1
            or package_manifest.get("protocol") != protocol
            or not isinstance(package_manifest.get("files"), list)
            or not package_manifest["files"]
        ):
            raise PermanentOperationError("package directory manifest contract is invalid")
        entrypoint = package_manifest.get("entrypoint")
        if not isinstance(entrypoint, dict):
            raise PermanentOperationError("package entrypoint is missing")
        entrypoint = _artifact_metadata(entrypoint)
        entrypoint_path = _safe_relative_path(
            posixpath.basename(manifest_key)
        )
        publication_manifest_key = (
            manifest_key if dont_replace
            else _stable_publication_key(manifest_key, context.idempotency_key)
        )
        version_prefix = (
            publication_manifest_key + ".workflow-versions/" +
            hashlib.sha256(context.idempotency_key.encode("utf-8")).hexdigest()
        )
        relative_prefix = posixpath.relpath(
            version_prefix, posixpath.dirname(manifest_key) or "."
        )
        uploaded = []
        total_bytes = 0
        with tempfile.TemporaryDirectory(prefix="workflow-upload-directory-") as directory:
            for item in package_manifest["files"]:
                if not isinstance(item, dict):
                    raise PermanentOperationError("package file entry is invalid")
                relative = _safe_relative_path(item.get("path"))
                artifact = _artifact_metadata(item.get("artifact"))
                local_path = os.path.join(directory, *relative.split("/"))
                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                artifact_store.download_file(
                    artifact["uri"], local_path,
                    expected_sha256=artifact["checksum_sha256"],
                    on_chunk=lambda _size: _check_active(context),
                )
                _check_active(context)
                output_key = posixpath.join(version_prefix, relative)
                published = _upload_file(
                    s3_client, bucket, output_key, local_path, context.attempt_id,
                    context.idempotency_key, artifact["checksum_sha256"],
                    artifact.get("media_type") or mimetypes.guess_type(relative)[0],
                    True, context,
                )
                uploaded.append({
                    "path": relative,
                    "key": output_key,
                    "size_bytes": published["size_bytes"],
                    "checksum_sha256": published["checksum_sha256"],
                })
                total_bytes += published["size_bytes"]

            root_item = next(
                (item for item in package_manifest["files"]
                 if item.get("path") == entrypoint_path),
                None,
            )
            if root_item is None:
                raise PermanentOperationError("package entrypoint is not in file inventory")
            root_artifact = _artifact_metadata(root_item.get("artifact"))
            if (
                root_artifact["uri"] != entrypoint["uri"]
                or root_artifact["checksum_sha256"] != entrypoint["checksum_sha256"]
            ):
                raise PermanentOperationError(
                    "package entrypoint does not match its inventory record"
                )
            root_local = os.path.join(directory, *entrypoint_path.split("/"))
            with open(root_local, "rb") as source:
                root_payload = source.read()
            if protocol == "hls":
                published_manifest = _rewrite_hls_manifest(
                    root_payload, relative_prefix
                )
                media_type = "application/vnd.apple.mpegurl"
            else:
                published_manifest = _rewrite_dash_manifest(
                    root_payload, relative_prefix
                )
                media_type = "application/dash+xml"
            _check_active(context)
            try:
                response = s3_client.put_object(
                    Bucket=bucket,
                    Key=publication_manifest_key,
                    Body=_ProgressReader(
                        __import__("io").BytesIO(published_manifest),
                        lambda _size: _check_active(context),
                    ),
                    ContentType=media_type,
                    Metadata={
                        "attempt_id": context.attempt_id,
                        "idempotency_key": context.idempotency_key,
                        "sha256": hashlib.sha256(published_manifest).hexdigest(),
                        "package_version_prefix": version_prefix,
                    },
                    **({"IfNoneMatch": "*"} if dont_replace else {}),
                )
            except Exception as exc:
                if dont_replace and _is_precondition_failure(exc):
                    existing = s3_client.head_object(
                        Bucket=bucket, Key=publication_manifest_key
                    )
                    metadata = existing.get("Metadata", {})
                    expected_digest = hashlib.sha256(published_manifest).hexdigest()
                    if (
                        int(existing.get("ContentLength", -1)) == len(published_manifest)
                        and metadata.get("sha256") == expected_digest
                        and metadata.get("idempotency_key") == context.idempotency_key
                    ):
                        pass
                    else:
                        raise PermanentOperationError(
                            "output key already exists with different content"
                        ) from exc
                elif _is_permanent_s3_error(exc):
                    raise PermanentOperationError("package manifest publication was rejected") from exc
                else:
                    raise RetryableOperationError("package manifest publication failed") from exc
            _check_active(context)
            head = s3_client.head_object(Bucket=bucket, Key=manifest_key)
            digest = hashlib.sha256(published_manifest).hexdigest()
            if (
                int(head.get("ContentLength", -1)) != len(published_manifest)
                or head.get("Metadata", {}).get("sha256") != digest
                or head.get("Metadata", {}).get("idempotency_key") != context.idempotency_key
            ):
                raise RetryableOperationError("package manifest read-back did not match")
        manifest_record = {
            "logical_name": "output.published_manifest",
            "artifact_type": "published_manifest",
            "uri": artifact_uri(bucket, publication_manifest_key),
            "media_type": media_type,
            "size_bytes": len(published_manifest),
            "checksum_sha256": digest,
            "metadata": {
                "attempt_id": context.attempt_id,
                "version_prefix": version_prefix,
                "dont_replace": dont_replace,
                "file_count": len(uploaded),
                "readback_verified": True,
            },
        }
        return {
            "validation_report": {
                "valid": True, "protocol": protocol, "file_count": len(uploaded),
                "manifest_key": publication_manifest_key, "errors": [],
            },
            "artifacts": [manifest_record],
            "metrics": {
                "input_bytes": directory_record.get("size_bytes"),
                "output_bytes": total_bytes + len(published_manifest),
                "artifact_count": len(uploaded) + 1,
            },
        }

    def deliver_webhook(node, context):
        params = _parameters(node)
        if params.get("enabled") is not True:
            receipt = {
                "schema_version": 1, "status": "skipped",
                "delivery_id": context.idempotency_key,
            }
        else:
            secret_ref = _required_text(
                params.get("webhook_secret_ref"), "webhook_secret_ref"
            )
            resolver = webhook_secret_resolver
            if resolver is None:
                from video_streaming.workflows.secret_runtime import (
                    resolve_job_webhook_secret,
                )
                resolver = lambda job_id, secret_id: resolve_job_webhook_secret(
                    job_id, secret_id, repository=repository
                )
            _check_active(context)
            try:
                destination = resolver(context.job_id, secret_ref)
            except SecretDecryptionError as exc:
                raise RetryableOperationError(
                    "encrypted webhook destination resolution failed"
                ) from exc
            except (SecretContextMismatch, ValueError) as exc:
                raise PermanentOperationError(
                    "encrypted webhook destination is not valid for this Job"
                ) from exc
            except Exception as exc:
                raise RetryableOperationError(
                    "encrypted webhook destination resolution failed"
                ) from exc
            job = repository.get_job_snapshot(context.job_id)
            if job is None:
                raise PermanentOperationError("webhook job record is missing")
            body = json.dumps(
                {
                    "request_id": context.job_id,
                    "reference_id": job.get("reference_id", ""),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            _check_active(context)
            try:
                response = webhook_requester(
                    destination,
                    body=body,
                    connect_timeout=settings.WEBHOOK_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=settings.WEBHOOK_READ_TIMEOUT_SECONDS,
                    allow_loopback=settings.WEBHOOK_ALLOW_LOOPBACK_CALLBACKS,
                    headers={"Idempotency-Key": context.idempotency_key},
                )
            except WebhookDestinationError as exc:
                raise PermanentOperationError(
                    "webhook destination was rejected by outbound policy"
                ) from exc
            except WebhookTransportError as exc:
                raise RetryableOperationError("webhook transport failed") from exc
            try:
                status = response.status
                if 200 <= status < 300:
                    receipt = {
                        "schema_version": 1,
                        "status": "delivered",
                        "delivery_id": context.idempotency_key,
                        "http_status": status,
                    }
                elif 500 <= status <= 599:
                    raise RetryableOperationError(
                        "webhook endpoint returned a server error"
                    )
                else:
                    raise PermanentOperationError(
                        "webhook endpoint rejected the delivery"
                    )
            finally:
                response.close()
            destination = None
            _check_active(context)

        stored = artifact_store.put_bytes(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            "webhooks/receipts/{}.json".format(context.idempotency_key),
            context.attempt_id,
            media_type="application/json",
            on_chunk=lambda _size: _check_active(context),
        )
        return {
            "validation_report": {
                "valid": True, "status": receipt["status"], "errors": [],
            },
            "artifacts": [{
                "logical_name": "webhook.delivery_receipt",
                "artifact_type": "webhook_receipt",
                "uri": stored["uri"],
                "media_type": "application/json",
                "size_bytes": stored["size_bytes"],
                "checksum_sha256": stored["checksum_sha256"],
            }],
            "metrics": {"delivery_count": 1 if receipt["status"] == "delivered" else 0},
        }

    return {
        "video.output.upload_file": upload_file,
        "video.output.upload_directory": upload_directory,
        "video.webhook.deliver": deliver_webhook,
    }
