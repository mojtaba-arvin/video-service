# Issue #81 — JIT CMAF packaging prototype

**Status:** research-only prototype; not enabled by the service and not a production recommendation.

## Decision boundary

The prototype consumes already encoded H.264/AAC MP4 assets. The offline fixture generator encodes synthetic test inputs once; every packaging operation uses FFmpeg `-c copy`. There is no request-path encoding and no change to the existing playlist task, storage publication, cache, or playback defaults.

Conventional pre-packaged HLS and DASH remain the fallback. The prototype's filesystem cache is deliberately isolated from production storage and request handlers.

## Reproduce

Install the repository's test dependencies and FFmpeg/FFprobe, then run:

```sh
python -m pip install -r requirements-test-py312.txt
python video-streaming/tests/fixtures/synthetic-vod-v1/generate_cmaf_sources.py \
  --output-dir /tmp/synthetic-cmaf-v1
PYTHONPATH=video-streaming python \
  video-streaming/tests/fixtures/synthetic-vod-v1/jit_packaging_benchmark.py \
  --input-dir /tmp/synthetic-cmaf-v1 \
  --output-dir /tmp/issue-81-packaging-results \
  --repetitions 3 --workload-hours 24 --cache-ttl-hours 1 \
  --hls-requests 100 --dash-requests 20
PYTHONPATH=video-streaming python -m unittest \
  video-streaming.tests.test_jit_cmaf_packaging -v
```

The generated `jit-packaging-benchmark.json` contains source checksums, FFmpeg build, package inventories, manifest hashes, repeated cold package latency, warm cache lookup latency, local first-manifest-byte timing, child-process CPU seconds, and a deterministic request/TTL storage model.

Do not commit generated media or benchmark outputs. Keep the JSON report as a CI artifact or attach it to the experiment record together with the machine/OS/CPU, filesystem, thermal/load conditions and exact command.

## What is measured vs modeled

- **Package bytes:** actual output file sizes for both protocols from the same pre-encoded source. The canonical source bytes are common to both approaches and excluded from the differential.
- **Storage byte-hours:** modeled from measured package bytes and a declared evenly-spaced request count, workload horizon and TTL. This is not object-store billing telemetry. It assumes the cache object is retained until TTL expiry after the last request; the model merges overlapping residency intervals.
- **Cache:** cold and warm cache state transitions are measured in the local filesystem prototype. Concurrent fills are covered by a lock/atomic-directory-publication test. The modeled hit ratio is separate from the local cache test.
- **Latency:** cold package wall time includes waiting for the package to be completely validated and atomically published. Warm latency is the cache lookup. First-byte timing reads the first byte of a local manifest after publication; it is not HTTP, CDN, player startup or first-frame latency.
- **CPU:** child-process user+system CPU seconds measured around FFmpeg subprocess execution. This does not include production worker contention, object-store/network overhead or a cloud invoice.
- **Determinism:** manifest hashes are compared across independent cold runs for a fixed source/profile/toolchain.
- **Audio/subtitles:** FFprobe checks packaged audio/video stream start-time alignment within 250 ms. Synthetic WebVTT cues are checked against the source media timeline. This does not prove rendered subtitle synchronization in a real player.
- **DRM:** not tested. No CENC encryption, key rotation, license acquisition, entitlement, FairPlay, Widevine or PlayReady flow is implemented by this prototype. DRM compatibility is therefore unknown, not passed.
- **Playback:** package graph/manifest validation and FFprobe are structural checks only. No hls.js, dash.js, native HLS, hardware decoder or device matrix is run.

## Cache identity and safety

The cache key includes the source SHA-256, protocol, schema version, packaging profile version, prototype version, FFmpeg build and normalized options. A profile/toolchain change creates a different immutable key. Explicit invalidation is serialized with fills. A fill is built in a private staging directory and made visible only by same-filesystem atomic rename after a non-empty root manifest exists. Failed fills remove staging state. The prototype does not implement distributed leases, cross-host locking, tenant authorization, signed URL handling, object-store consistency, eviction/GC or DRM key lifecycle.

A cache failure must select an already validated conventional package; if no valid fallback exists, fail closed. Never serve a partial JIT package.

## Acceptance and rollout gates

The tests cover deterministic manifests, concurrent cache fills, no partial visibility, versioned keys, invalidation/rebuild, fallback selection, audio/video start alignment, subtitle cue timeline bounds and copy-only packaging.

The following evidence is intentionally **not claimed** by this synthetic prototype:

1. Storage byte-hour savings on a representative production request distribution.
2. Positive net economics after cache compute, cache storage, origin requests, CDN egress and operations.
3. HTTP/CDN TTFB or time-to-first-frame improvements.
4. DRM interoperability, key rotation or license-service compatibility.
5. Real player/device compatibility and end-to-end subtitle rendering/synchronization.
6. Distributed cache stampede behavior or production object-store atomicity.

Before any rollout recommendation, run the same immutable pre-encoded, legally approved corpus and profile on pinned hardware; capture raw repeated results and actual storage/CDN usage; test encrypted and clear variants across the required DRM systems; run real browser/native player playback and audio/subtitle sync; then compare p50/p95 latency and byte-hours under a declared request/TTL distribution. Until all relevant evidence exists, keep conventional pre-packaging as the only production path.

## Rollback

No production path or configuration is changed. Remove the experiment branch/prototype to roll back. If a future implementation is separately approved, its rollout must remain feature-flagged and switch immediately to the existing validated pre-packaged artifacts on cache, packaging, DRM, validation or storage failures.
