---
name: ffmpeg-regression-testing
description: Use when changing FFmpeg/FFprobe media processing, playlist or thumbnail generation, output validation, cancellation, retries, process cleanup, or fixtures in this repository.
---

# FFmpeg regression testing

1. Read the calling task, mixins, callbacks, configuration, and tests first.
2. Define the complete output contract: container, streams, codec, dimensions,
   frame rate, duration, rendition list, playlist references, and audio needs.
3. Prefer short lavfi-generated inputs such as testsrc2, color, and sine.
   Do not download media for routine tests.
4. Run FFmpeg with explicit argument arrays, deterministic parameters, and
   bounded timeouts. Capture exit code and useful stderr.
5. Probe every relevant output with FFprobe. Validate existence, non-zero size,
   parseability, stream type, codec, dimensions, duration, and manifest refs.
6. Cover negative cases: missing, empty, corrupt, truncated, non-zero exit,
   partial output, rendition mismatch, cancellation, retry, and cleanup.
7. Test concurrent independent jobs when paths or process supervision change.
8. Keep CI hermetic: no S3, Redis, RabbitMQ, CDN, or network dependency unless
   an isolated adapter contract explicitly requires it.

A callback message or Celery task return is not proof of a valid artifact.
Cancelled or partial outputs must never be published. Keep generated media
outside Git. Synthetic content is for correctness, not perceptual claims.
