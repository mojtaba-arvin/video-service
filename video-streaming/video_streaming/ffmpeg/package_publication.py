"""Storage publication gate for static HLS/DASH packages.

The inventory sidecar is an ownership journal for one root manifest. It lets
retries remove only the previous package's objects instead of deleting a
shared bucket prefix. The player-visible root manifest is uploaded last.
"""

import json
import os
from pathlib import Path

from video_streaming.ffmpeg.package_validation import (
    manifest_dependencies,
    ordered_package_files,
    validate_package,
)


class PackagePublicationError(RuntimeError):
    """Raised when a package artifact is not durably readable in storage."""


def _object_key(prefix, relative_path):
    if not prefix:
        return relative_path
    return prefix.rstrip("/") + "/" + relative_path


def _safe_inventory_path(value):
    if (
            not isinstance(value, str)
            or value in ("", ".", "..")
            or "\\" in value
    ):
        raise PackagePublicationError("Invalid package inventory path")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise PackagePublicationError("Unsafe package inventory path: " + value)
    if path.as_posix() != value:
        raise PackagePublicationError("Non-canonical package inventory path: " + value)
    return value


def _verify_readable(storage, bucket, key, expected_size=None):
    details = storage.head(key=key, bucket_name=bucket)
    if not details:
        raise PackagePublicationError(
            "Published dependency is not visible through storage HEAD: " + key
        )

    actual_size = int(details.get("ContentLength", -1))
    if expected_size is not None and actual_size != expected_size:
        raise PackagePublicationError(
            "Published dependency size mismatch for {}: expected {}, found {}"
            .format(key, expected_size, actual_size)
        )

    first_byte = storage.read_range(
        key=key, bucket_name=bucket, start=0, end=0
    )
    if not first_byte:
        raise PackagePublicationError(
            "Published dependency is not readable: " + key
        )
    return actual_size


def _inventory_key(prefix, manifest_relative):
    name = Path(manifest_relative).name
    return _object_key(prefix, ".{}.artifacts.json".format(name))


def _read_inventory(storage, bucket, key):
    raw = storage.read_object_bytes(key=key, bucket_name=bucket)
    if raw is None:
        return []
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackagePublicationError(
            "Existing package inventory is unreadable: " + key
        ) from exc
    if payload.get("format_version") != 1:
        raise PackagePublicationError(
            "Unsupported package inventory version: " + key
        )
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise PackagePublicationError(
            "Package inventory artifacts must be a list: " + key
        )
    return sorted(set(_safe_inventory_path(value) for value in artifacts))


def _write_inventory(storage, bucket, key, protocol, manifest, relatives):
    payload = {
        "format_version": 1,
        "protocol": protocol,
        "manifest": manifest,
        "artifacts": sorted(set(relatives)),
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    storage.put_object_bytes(
        key=key,
        content=encoded,
        bucket_name=bucket,
        content_type="application/json",
    )
    _verify_readable(storage, bucket, key, expected_size=len(encoded))
    return len(encoded)


def _artifact_record(bucket, prefix, file_path, relative_path, file_size,
                     root_manifest, protocol):
    suffix = Path(relative_path).suffix.lower()
    if suffix == ".m3u8":
        artifact_type = (
            "streaming_manifest"
            if relative_path == root_manifest
            else "streaming_playlist"
        )
        media_type = "application/vnd.apple.mpegurl"
    elif suffix == ".mpd":
        artifact_type = "streaming_manifest"
        media_type = "application/dash+xml"
    elif suffix == ".ts":
        artifact_type = "media_segment"
        media_type = "video/mp2t"
    elif suffix == ".m4s":
        artifact_type = "media_segment"
        media_type = "video/iso.segment"
    elif suffix in (".mp4", ".m4a"):
        artifact_type = "media_initialization"
        media_type = "video/mp4" if suffix == ".mp4" else "audio/mp4"
    else:
        artifact_type = "media_artifact"
        media_type = None

    key = _object_key(prefix, relative_path)
    return {
        "artifact_type": artifact_type,
        "logical_name": relative_path,
        "uri": "s3://{}/{}".format(bucket, key),
        "media_type": media_type,
        "size_bytes": file_size,
        "metadata": {
            "protocol": protocol,
            "readback_verified": True,
        },
    }


def publish_package(
        directory,
        manifest_path,
        protocol,
        expected_renditions,
        s3_output_key,
        s3_output_bucket,
        storage,
        callback_factory=None):
    """Validate, replace an owned package and publish its root manifest last.

    1. Validate locally before altering the last known good publication.
    2. Read the previous per-root inventory and write a combined ownership
       journal (previous + next objects).
    3. Withdraw old manifests and old objects owned by that root only.
    4. Narrow the inventory to this attempt, upload/read-back all dependencies,
       and upload the root manifest as the final media object.
    5. On failure, remove every object from this attempt. If cleanup itself
       fails, retain the inventory journal so a later retry can finish cleanup.

    This is idempotent for deterministic object keys and safe for shared
    buckets/prefixes because it never deletes an unowned prefix wholesale.
    """
    root = Path(directory).resolve()
    manifest_input = Path(manifest_path)
    manifest = manifest_input.resolve()
    manifest_relative = manifest.relative_to(root).as_posix()
    prefix = s3_output_key.rpartition("/")[0]
    root_key = _object_key(prefix, manifest_relative)
    inventory_key = _inventory_key(prefix, manifest_relative)

    report = validate_package(
        directory=directory,
        package_type=protocol,
        expected_renditions=expected_renditions,
        manifest_path=manifest_path,
    )
    report.require_valid()

    files = ordered_package_files(
        directory=directory,
        manifest_name=manifest_relative,
        protocol=protocol,
    )
    if not files or files[-1][1] != manifest_relative:
        raise PackagePublicationError(
            "Root manifest must be the final package artifact"
        )

    new_relatives = sorted(relative for _, relative, _ in files)
    old_relatives = _read_inventory(
        storage, s3_output_bucket, inventory_key
    )
    combined_relatives = sorted(set(old_relatives) | set(new_relatives))

    # Write the union first. If the worker dies during deletion or upload, the
    # next attempt knows both the old and the partially written object keys.
    _write_inventory(
        storage, s3_output_bucket, inventory_key,
        protocol, manifest_relative, combined_relatives,
    )

    # Withdraw old manifests first, then delete all old package objects before
    # deterministic keys are written again. No unrelated prefix is touched.
    old_manifest_relatives = [
        relative for relative in combined_relatives
        if Path(relative).suffix.lower() in (".m3u8", ".mpd")
    ]
    # Withdraw the root first. If this fails, preserve the previous package
    # intact; never delete child manifests while the old root is visible.
    storage.delete_object(key=root_key, bucket_name=s3_output_bucket)
    for relative in old_manifest_relatives:
        key = _object_key(prefix, relative)
        if key != root_key:
            storage.delete_object(
                key=key,
                bucket_name=s3_output_bucket,
            )

    for relative in combined_relatives:
        storage.delete_object(
            key=_object_key(prefix, relative),
            bucket_name=s3_output_bucket,
        )

    # Once prior objects are removed, the journal can be narrowed to this
    # attempt. This remains durable across worker loss before root publication.
    inventory_size = _write_inventory(
        storage, s3_output_bucket, inventory_key,
        protocol, manifest_relative, new_relatives,
    )

    total_size = sum(file_size for _, _, file_size in files)
    total_files = len(files)
    uploaded_keys = []
    manifest_keys = [
        _object_key(prefix, relative)
        for relative in new_relatives
        if Path(relative).suffix.lower() in (".m3u8", ".mpd")
    ]

    try:
        # Upload every dependency before its referencing manifest. The root
        # manifest is guaranteed to be the last item by ordered_package_files.
        for index, (file_path, relative_path, file_size) in enumerate(files):
            local_path = Path(file_path)
            object_key = _object_key(prefix, relative_path)
            is_manifest = local_path.suffix.lower() in (".m3u8", ".mpd")

            if is_manifest:
                dependencies = manifest_dependencies(
                    directory=directory,
                    manifest_path=file_path,
                    protocol=protocol,
                )
                for dependency in dependencies:
                    dependency_path = root / dependency
                    dependency_key = _object_key(prefix, dependency)
                    _verify_readable(
                        storage,
                        s3_output_bucket,
                        dependency_key,
                        expected_size=dependency_path.stat().st_size,
                    )

            callback = None
            if callback_factory is not None:
                callback = callback_factory(index, total_files, total_size)

            storage.upload_file_by_path(
                key=object_key,
                file_path=file_path,
                bucket_name=s3_output_bucket,
                callback=callback,
            )
            uploaded_keys.append(object_key)
            _verify_readable(
                storage, s3_output_bucket, object_key,
                expected_size=file_size,
            )

        _verify_readable(
            storage, s3_output_bucket, root_key,
            expected_size=manifest.stat().st_size,
        )
    except Exception:
        cleanup_ok = True
        # Do not leave playlists advertising an incomplete package.
        attempt_keys = [
            _object_key(prefix, relative)
            for relative in new_relatives
        ]
        for key in sorted(set(attempt_keys + uploaded_keys + manifest_keys + [root_key])):
            try:
                storage.delete_object(key=key, bucket_name=s3_output_bucket)
            except Exception:
                cleanup_ok = False
        if cleanup_ok:
            try:
                storage.delete_object(
                    key=inventory_key, bucket_name=s3_output_bucket
                )
            except Exception:
                # Keep the journal if deletion fails; next retry can recover.
                pass
        raise

    published_artifacts = [
        _artifact_record(
            s3_output_bucket, prefix, file_path, relative_path,
            file_size, manifest_relative, protocol,
        )
        for file_path, relative_path, file_size in files
    ]
    inventory_record = {
        "artifact_type": "package_inventory",
        "logical_name": Path(inventory_key).name,
        "uri": "s3://{}/{}".format(s3_output_bucket, inventory_key),
        "media_type": "application/json",
        "size_bytes": inventory_size,
        "metadata": {
            "format_version": 1,
            "owned_artifact_count": len(new_relatives),
        },
    }
    published_artifacts.append(inventory_record)

    return {
        "directory_size": total_size,
        "files": total_files,
        "manifest_key": root_key,
        "inventory_key": inventory_key,
        "validation_report": report.as_dict(),
        "artifacts": published_artifacts,
    }
