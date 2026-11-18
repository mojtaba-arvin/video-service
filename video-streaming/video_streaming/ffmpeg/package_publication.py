"""Storage publication gate for HLS/DASH VOD packages.

No manifest is uploaded until all of its dependencies have been uploaded,
HEAD-verified, and successfully read through a one-byte ranged GET. The root
manifest is withdrawn before retry publication and uploaded last.
"""

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


def publish_package(
        directory,
        manifest_path,
        protocol,
        expected_renditions,
        s3_output_key,
        s3_output_bucket,
        storage,
        callback_factory=None):
    """Validate and publish package assets with dependency-aware ordering.

    Existing root-manifest visibility is removed before writing any replacement
    objects. Deterministic object keys are overwritten on retries, so repeated
    attempts do not create duplicate segment keys. A failed attempt leaves no
    root manifest pointing at a partial package.
    """
    root = Path(directory).resolve()
    manifest_input = Path(manifest_path)
    manifest = manifest_input.resolve()
    manifest_relative = manifest.relative_to(root).as_posix()
    prefix = s3_output_key.rpartition("/")[0]
    root_key = _object_key(prefix, manifest_relative)

    # Withdraw the root and child manifests already present in this isolated
    # output directory. A failed retry must not leave any old playlist
    # advertising assets while the package is being replaced.
    manifest_keys = [root_key]
    for current, directories, filenames in os.walk(
            str(root), followlinks=False):
        for filename in filenames:
            if Path(filename).suffix.lower() in (".m3u8", ".mpd"):
                relative = (Path(current) / filename).relative_to(root).as_posix()
                key = _object_key(prefix, relative)
                if key not in manifest_keys:
                    manifest_keys.append(key)
    for key in manifest_keys:
        storage.delete_object(key=key, bucket_name=s3_output_bucket)

    try:
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
        total_size = sum(file_size for _, _, file_size in files)
        total_files = len(files)
        manifest_keys = [
            _object_key(prefix, relative_path)
            for file_path, relative_path, _ in files
            if Path(file_path).suffix.lower() in (".m3u8", ".mpd")
        ]
        for manifest_key in manifest_keys:
            storage.delete_object(
                key=manifest_key, bucket_name=s3_output_bucket
            )

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
            _verify_readable(
                storage,
                s3_output_bucket,
                object_key,
                expected_size=file_size,
            )

        # The root manifest must be the last ordered artifact.
        if files[-1][1] != manifest_relative:
            raise PackagePublicationError(
                "Root manifest was not the final package artifact"
            )
        _verify_readable(
            storage, s3_output_bucket, root_key,
            expected_size=manifest.stat().st_size,
        )
    except Exception:
        # Never leave old/partial playlists advertising an incomplete package.
        # Media segments may be overwritten deterministically, but no manifest
        # created or replaced by this attempt remains visible.
        for manifest_key in set(manifest_keys):
            storage.delete_object(
                key=manifest_key, bucket_name=s3_output_bucket
            )
        raise

    published_artifacts = []
    for file_path, relative_path, file_size in files:
        suffix = Path(relative_path).suffix.lower()
        if suffix == ".m3u8":
            artifact_type = (
                "streaming_manifest"
                if relative_path == manifest_relative
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
        published_artifacts.append({
            "artifact_type": artifact_type,
            "logical_name": relative_path,
            "uri": "s3://{}/{}".format(s3_output_bucket, key),
            "media_type": media_type,
            "size_bytes": file_size,
            "metadata": {
                "protocol": protocol,
                "readback_verified": True,
            },
        })

    return {
        "directory_size": total_size,
        "files": total_files,
        "manifest_key": root_key,
        "validation_report": report.as_dict(),
        "artifacts": published_artifacts,
    }
