"""S3-backed immutable artifact storage for cross-worker workflow handoff.

Artifacts are addressed by opaque s3:// URIs and attempt-scoped object keys.
A worker-local path is only a temporary upload/download source and is never
persisted as an inter-node reference. The SQL repository remains responsible
for publishing artifact records after a successful lease-fenced node commit.
"""
import hashlib
import os
import re
from urllib.parse import quote, unquote, urlparse
import tempfile

from video_streaming.workflows.artifacts import attempt_scoped_object_key


_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


class ArtifactStoreError(Exception):
    """Base class for durable artifact storage failures."""


class ArtifactNotFound(ArtifactStoreError):
    """The referenced immutable artifact does not exist."""


def artifact_uri(bucket, key):
    if not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket):
        raise ValueError("invalid artifact bucket")
    if not isinstance(key, str) or not key or key.startswith("/") or "\\" in key:
        raise ValueError("invalid artifact object key")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError("artifact object key contains unsafe segments")
    return "s3://{}/{}".format(bucket, quote(key, safe="/-._~"))


def parse_artifact_uri(uri):
    if not isinstance(uri, str):
        raise ValueError("artifact URI must be text")
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError("artifact URI must be an unqualified s3:// URI")
    key = unquote(parsed.path.lstrip("/"))
    canonical = artifact_uri(parsed.netloc, key)
    if canonical != uri:
        raise ValueError("artifact URI is not canonical")
    return parsed.netloc, key


class S3ArtifactStore:
    """Immutable S3 artifact adapter; never returns local paths as references."""

    def __init__(self, s3_client, default_bucket):
        if s3_client is None:
            raise ValueError("s3_client is required")
        # Validate bucket independently from boto3 so invalid config fails fast.
        artifact_uri(default_bucket, "configuration-check")
        self._client = s3_client
        self._default_bucket = default_bucket

    def put_file(self, local_path, logical_key, attempt_id, media_type=None):
        if not isinstance(local_path, str) or not os.path.isfile(local_path):
            raise ValueError("local_path must identify an existing regular file")
        key = attempt_scoped_object_key(logical_key, attempt_id)
        # Hash and upload one private snapshot. Reading the source twice would
        # allow a producer to replace or mutate it between digest and upload.
        descriptor, snapshot_path = tempfile.mkstemp(prefix="workflow-artifact-")
        try:
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "wb") as snapshot:
                with open(local_path, "rb") as source:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        snapshot.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            checksum = digest.hexdigest()
            metadata = {"sha256": checksum, "attempt_id": attempt_id}
            extra = {"Metadata": metadata, "IfNoneMatch": "*"}
            if media_type:
                extra["ContentType"] = media_type
            try:
                with open(snapshot_path, "rb") as source:
                    self._client.put_object(
                        Bucket=self._default_bucket,
                        Key=key,
                        Body=source,
                        **extra
                    )
            except Exception as exc:
                response = getattr(exc, "response", {}) or {}
                status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                code = response.get("Error", {}).get("Code")
                if status != 412 and code != "PreconditionFailed":
                    raise
                # A retry of the same immutable Attempt/key is safe only when
                # the object already stored is byte-for-byte equivalent.
                existing = self._client.head_object(
                    Bucket=self._default_bucket, Key=key
                )
                existing_metadata = existing.get("Metadata", {})
                if (
                    existing.get("ContentLength") != size
                    or existing_metadata.get("sha256") != checksum
                ):
                    raise ArtifactStoreError(
                        "immutable artifact key already contains different content"
                    ) from exc
            return {
                "uri": artifact_uri(self._default_bucket, key),
                "size_bytes": size,
                "checksum_sha256": checksum,
                "media_type": media_type,
            }
        finally:
            try:
                os.unlink(snapshot_path)
            except OSError:
                pass

    def put_bytes(self, payload, logical_key, attempt_id, media_type=None):
        """Persist a bounded in-memory payload as an immutable S3 Artifact."""
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        import tempfile
        descriptor, temporary_path = tempfile.mkstemp(
            prefix="workflow-artifact-"
        )
        try:
            with os.fdopen(descriptor, "wb") as target:
                target.write(payload)
            return self.put_file(
                temporary_path,
                logical_key,
                attempt_id,
                media_type=media_type,
            )
        finally:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass

    def download_file(self, uri, destination_path, expected_sha256=None):
        bucket, key = parse_artifact_uri(uri)
        if not isinstance(destination_path, str) or not destination_path:
            raise ValueError("destination_path is required")
        response = self._client.get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        digest = hashlib.sha256()
        os.makedirs(os.path.dirname(os.path.abspath(destination_path)), exist_ok=True)
        temporary = destination_path + ".partial"
        try:
            with open(temporary, "wb") as target:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    target.write(chunk)
                    digest.update(chunk)
            actual = digest.hexdigest()
            expected = (
                expected_sha256
                if expected_sha256 is not None
                else response.get("Metadata", {}).get("sha256")
            )
            if not isinstance(expected, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected
            ):
                raise ArtifactStoreError(
                    "artifact checksum is missing or invalid"
                )
            if actual != expected:
                raise ArtifactStoreError("artifact checksum mismatch")
            os.replace(temporary, destination_path)
            return {
                "path": destination_path,
                "size_bytes": os.path.getsize(destination_path),
                "checksum_sha256": actual,
            }
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        finally:
            close = getattr(body, "close", None)
            if close:
                close()

    def head(self, uri):
        bucket, key = parse_artifact_uri(uri)
        try:
            response = self._client.head_object(Bucket=bucket, Key=key)
        except Exception as exc:
            response_code = getattr(exc, "response", {}).get(
                "ResponseMetadata", {}
            ).get("HTTPStatusCode")
            if response_code == 404:
                raise ArtifactNotFound("artifact not found") from exc
            raise
        metadata = response.get("Metadata", {})
        return {
            "uri": uri,
            "size_bytes": response.get("ContentLength"),
            "checksum_sha256": metadata.get("sha256"),
            "media_type": response.get("ContentType"),
        }
