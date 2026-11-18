# ADR 0001: VOD package validation and publication invariants

- **Status:** Accepted
- **Date:** 2026-11-18
- **Scope:** HLS and static MPEG-DASH VOD outputs in the legacy Celery pipeline

## Context

FFmpeg can return while a multi-file package is incomplete, and the original
pipeline marked a playlist output as processed before publication. Uploading a
master manifest before its referenced variants and segments makes an invalid
package discoverable to players. Retrying into a reused directory can also
carry stale local files into the next attempt.

## Decisions

### Validation contract

All protocol validators use the shared `validate_package(directory,
package_type, expected_renditions, ...)` entry point and return a
`PackageValidationReport`. A report is valid only when its error list is
empty. Each issue carries a stable code, a location, and a human-readable
message. HLS expectations are package-relative rendition playlist paths;
DASH expectations are Representation IDs.

The validator fails closed on unsupported/remote references, path traversal,
symlinks, missing/empty files, malformed or incomplete VOD playlists, dynamic
MPDs, unresolved templates, and mismatched rendition inventory.

### Metadata and cost boundary

The required structural checks inspect manifests, file existence, and file
sizes. The first processing boundary additionally uses FFprobe metadata-only
inspection for codec, resolution, measured bitrate, and duration consistency.
FFprobe runs as a subprocess with a configurable per-probe timeout
(`PACKAGE_METADATA_PROBE_TIMEOUT_SECONDS`, default 10 seconds). This step
does not decode frames or re-encode media. No performance/cost claim is made;
the extra probe/request overhead must be benchmarked before optimization claims.

### Publication invariant

Every media object is uploaded first. HLS playlists are topologically ordered:
child playlists are uploaded only after their referenced media, and parents
only after child playlists. DASH media/init objects precede the MPD. Before a
manifest is uploaded, every direct dependency must pass object HEAD and a
one-byte ranged GET; every newly uploaded object is also size-checked and
read-back checked. The root manifest is the final publication point.

The prior root manifest and locally known child manifests are withdrawn before
replacing assets. A failed attempt removes manifests written by that attempt.
This prevents a new/partial package from being advertised while dependencies
are missing.

### Status invariant

Playlist outputs cannot enter `PROCESSING_FINISHED` unless two per-attempt
proof keys exist: successful local validation and successful storage
publication/read-back verification. The check is enforced centrally inside
`BaseOutputMixin.save_output_status`, not only in the playlist task.

### Retry/idempotency

The same job/output produces deterministic object keys. A retry withdraws its
root and child manifests, overwrites those stable asset keys, verifies them,
then republishes the root. This avoids duplicate keys and stale root manifests.
Unreferenced old remote media objects are not broadly deleted because an S3
prefix may be shared by adjacent outputs; they are not referenced by the
published manifest. Safe garbage collection requires an explicit per-output
object ownership namespace and is deferred.

## Consequences

- A manifest is never the publication trigger until all its dependencies are
  present and readable.
- A failed package is not marked processed and leaves no root/child manifest
  from the failed attempt.
- Publication adds one HEAD and one bounded read request per artifact, plus
  FFprobe metadata inspection on the first boundary. This overhead is explicit
  and must be measured.
- The current proof keys still use Redis and are not durable workflow state;
  durable Job/Stage/Attempt/Artifact state and restart recovery remain separate
  roadmap work.
- The current upload adapter is S3-compatible but still uses the existing
  S3Service. Alternative storage backends can implement the same small
  delete/upload/head/range-read contract later.
