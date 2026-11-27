# Issue #81 — JIT CMAF packaging prototype

**Status:** research-only prototype; not enabled by the service and not a production recommendation.

## Decision boundary

The prototype consumes already encoded H.264/AAC MP4 assets. The offline fixture generator encodes synthetic test inputs once; every packaging operation uses FFmpeg `-c copy`. It builds a pre-packaged CMAF baseline with one DASH-generated set of audio/video initialization and media objects, then creates HLS master/media playlists that reference those same CMAF objects. It compares that shared-object baseline with separately generated HLS/DASH packages held in a versioned JIT cache. There is no request-path encoding and no change to the existing playlist task, storage publication, cache, or playback defaults.

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
PYTHONPATH=video-streaming python -m unittest discover \
  -s video-streaming/tests -p 'test_jit_cmaf_packaging.py' -v
```

The generated `jit-packaging-benchmark.json` contains fixture and runner provenance, source checksums, FFmpeg build, shared CMAF media-object inventory, HLS/DASH manifest hashes, conventional separate-package bytes, repeated cold package latency, warm cache lookup latency, local first-manifest-byte timing, FFmpeg process CPU seconds, sampled peak RSS, physical bytes read/written, peak output scratch size and a deterministic request/TTL byte-hour comparison between shared pre-packaged CMAF and JIT cache objects. Conventional and JIT cold runs alternate order within each protocol to reduce systematic page-cache/thermal bias.

Do not commit generated media or benchmark outputs. Keep the JSON report as a CI artifact or attach it to the experiment record together with the machine/OS/CPU, filesystem, thermal/load conditions and exact command.

## What is measured vs modeled

- **Package bytes:** actual file sizes for (a) one pre-packaged CMAF segment/init set shared by both HLS and DASH manifests, (b) conventional separately packaged HLS and DASH outputs, and (c) the JIT cache outputs. The canonical source bytes are common to both approaches and excluded from the differential.
- **Storage byte-hours:** shared pre-packaged CMAF bytes are modeled as resident for the full workload horizon. JIT package bytes are modeled using a declared evenly-spaced per-protocol request count, workload horizon and TTL. This is not object-store billing telemetry. It assumes each cache object is retained until TTL expiry after the last request; the model merges overlapping residency intervals. The report also gives a modeled byte-hour difference and estimated cold-package CPU seconds; neither is a measured monetary saving.
- **Cache:** cold and warm cache state transitions are measured in the local filesystem prototype. Concurrent fills are covered by a lock/atomic-directory-publication test. The modeled hit ratio is separate from the local cache test.
- **Latency:** cold package wall time includes waiting for the package to be completely validated and atomically published. Warm latency is the cache lookup. First-byte timing reads the first byte of a local manifest after publication; it is not HTTP, CDN, player startup or first-frame latency.
- **CPU/resources:** FFmpeg process CPU seconds, sampled peak RSS, process I/O counters and sampled peak package-directory scratch bytes are captured with psutil. Sampling may miss short peaks; unavailable process counters remain null. This excludes worker contention, object-store/network overhead and cloud billing.
- **Determinism:** manifest hashes are compared across independent cold runs for a fixed source/profile/toolchain.
- **Run order:** conventional and JIT cold package runs alternate order per repetition. The shared CMAF reference is generated once per fixture before protocol-specific runs, so its cold latency is a single-run observation, not a p50/p95 distribution.
- **Quality/retries:** perceptual quality metrics are not applicable because encoded bitstreams are copied, not re-encoded. The benchmark workload does not inject packaging retries/failures; fallback and cleanup behavior are covered by focused tests.
- **Audio/subtitles:** FFprobe checks packaged audio/video stream start-time alignment within 250 ms, and the shared CMAF audio/video segment timeline durations must agree within 250 ms. Synthetic WebVTT sidecar cue times are checked against source duration; the VTT is not muxed into the CMAF package. Embedded subtitle carriage and rendered subtitle synchronization remain unverified.
- **DRM:** not tested. No CENC encryption, key rotation, license acquisition, entitlement, FairPlay, Widevine or PlayReady flow is implemented by this prototype. DRM compatibility is therefore unknown, not passed.
- **Playback:** package graph/manifest validation and FFprobe are structural checks only. No hls.js, dash.js, native HLS, hardware decoder or device matrix is run.

## Existing production pipeline inspected

- `video_streaming/ffmpeg/tasks/create_playlist.py` selects the HLS/DASH playlist through `CreatePlaylistMixin.initial_protocol()`, asks the existing library to prepare output paths, then builds an FFmpeg command and runs it through `run_streaming_process` with timeout, cancellation, process-usage and Attempt-lease callbacks. The current task supports HLS/DASH selection and the `fragmented` setting; it is not a request-time JIT cache.
- The task calls `validate_package()` before completing the processing Attempt. The validator checks local VOD manifests and references, rejects unsafe/external paths and unsupported/dynamic DASH forms, and can use FFprobe metadata. It is structural validation, not a decoder/player test.
- `ffmpeg/tasks/mixins/upload_directory.py` delegates to `package_publication.publish_package()`. Publication inventories package dependencies, uploads child media and child manifests before the root manifest, and verifies storage visibility/readability (HEAD plus first-byte range read) before the player-visible root manifest is published. Attempt-scoped object keys and an ownership inventory protect retries/cleanup.
- Existing `test_synthetic_vod_package_harness.py` exercises generated HLS/DASH output and package graph validation; its harness encodes rendition variants. `test_package_publication.py` covers storage publication ordering, read-back, retry cleanup and partial failures. Repository searches found no hls.js/dash.js/native-player/device playback suite; actual player compatibility remains an explicit evidence gap.
- The prototype intentionally does not reuse or bypass production publication code: its local filesystem cache isolates cache-key, atomic-fill and measurement experiments. Any production adapter would need to integrate with the existing validation and manifest-last publication boundaries rather than publish its own manifests directly.

## Cache identity and safety

The cache key includes the source SHA-256, protocol, schema version, packaging profile version, prototype version, FFmpeg build and normalized options. A profile/toolchain change creates a different immutable key. Explicit invalidation is serialized with fills. A fill is built in a private staging directory and made visible only by same-filesystem atomic rename after a non-empty root manifest exists. Failed fills remove staging state. The prototype does not implement distributed leases, cross-host locking, tenant authorization, signed URL handling, object-store consistency, eviction/GC or DRM key lifecycle.

A cache failure must select an already validated conventional package; if no valid fallback exists, fail closed. Never serve a partial JIT package.

## Acceptance and rollout gates

The tests cover deterministic shared CMAF/HLS/DASH manifests, reuse of one CMAF media-object set by both protocols, benchmark report generation, concurrent cache fills, no partial visibility, versioned keys, invalidation/rebuild, fallback selection, audio/video start and segment-timeline alignment, synthetic subtitle cue bounds and copy-only packaging.

The following evidence is intentionally **not claimed** by this synthetic prototype:

1. Storage byte-hour savings on a representative production request distribution.
2. Positive net economics after cache compute, cache storage, origin requests, CDN egress and operations.
3. HTTP/CDN TTFB or time-to-first-frame improvements.
4. DRM interoperability, key rotation or license-service compatibility.
5. Real player/device compatibility and end-to-end subtitle rendering/synchronization.
6. Distributed cache stampede behavior or production object-store atomicity.
7. Retry/failure rates under a representative request workload; failure fallback is tested separately, not load-injected into the benchmark run.

Before any rollout recommendation, run the same immutable pre-encoded, legally approved corpus and profile on pinned hardware; capture raw repeated results and actual storage/CDN usage; test encrypted and clear variants across the required DRM systems; run real browser/native player playback and audio/subtitle sync; then compare p50/p95 latency and byte-hours under a declared request/TTL distribution. Until all relevant evidence exists, keep conventional pre-packaging as the only production path.

## Rollback

No production path or configuration is changed. Remove the experiment branch/prototype to roll back. If a future implementation is separately approved, its rollout must remain feature-flagged and switch immediately to the existing validated pre-packaged artifacts on cache, packaging, DRM, validation or storage failures.
