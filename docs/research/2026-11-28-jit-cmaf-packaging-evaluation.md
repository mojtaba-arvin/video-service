# JIT CMAF packaging prototype evaluation

- **Status:** Experimental prototype; not production-enabled
- **Issue:** #81
- **Branch:** `experiment/issue-81-jit-cmaf-packaging`
- **Decision:** Do not replace conventional pre-packaging on current evidence

## Existing pipeline and boundaries

The current `create_playlist` Celery task builds HLS or DASH output through
the existing FFmpeg streaming library, runs the shared package validator, and
completes a processing Attempt only after validation. A separate publication
Attempt uploads attempt-scoped objects, verifies storage readback, and
publishes the root manifest last. This is the safe conventional path and
remains unchanged.

The experimental module is not imported by the production task, gRPC handler,
or request path. It accepts an already encoded input and invokes FFmpeg with
`-c copy`; it does not select an encoder, invoke `-c:v libx264`, or re-encode
media. The prototype is a packaging-only cache experiment, not a production
origin or CDN implementation.

## What the prototype provides

- A content-addressed, immutable cache key over asset ID/version, profile
  version, source SHA-256, protocol, packaging implementation version, and
  DRM profile.
- Per-key process/thread locking, staging directories, validation callback,
  and atomic directory rename. The root manifest is required before publish;
  failed builds never become visible.
- Explicit invalidation for one exact immutable key.
- HLS fMP4 and DASH segmenter command builders with stream copy only.
- Fail-closed handling for non-clear DRM profiles. Such requests must continue
  through the conventional packaging path.
- A generic conventional-fallback helper.
- A local benchmark runner reporting bytes, scenario storage byte-hours,
  cold packaging wall/CPU time, warm cache lookup latency/hit ratio, and
  loopback HTTP first-byte latency.

The prototype currently produces protocol-specific HLS or DASH packages.
It does **not** yet prove that one identical CMAF segment set can be referenced
by both HLS and DASH manifests. That shared-object optimization requires
explicit track/segment mapping and player validation before it can be claimed.

## Reproduction

Use the repository's generated synthetic corpus only for deterministic
regression checks:

```sh
python video-streaming/tests/fixtures/synthetic-vod-v1/generate.py \
  --output-dir /tmp/synthetic-vod-v1

PYTHONPATH=video-streaming python -m unittest \
  video-streaming.tests.test_jit_packaging -v
```

For a benchmark run, provide a conventional package generated from the same
encoded source, rendition/profile, protocol, and segment duration. The
pre-packaged directory must contain `master.m3u8` for HLS or `manifest.mpd`
for DASH. Use a new, empty cache directory for every cold run:

```sh
PYTHONPATH=video-streaming python video-streaming/tools/benchmark_jit_packaging.py \
  --input /tmp/synthetic-vod-v1/portrait-audio-30fps.mp4 \
  --prepackaged-dir /path/to/conventional-package \
  --cache-dir /tmp/jit-cache-cold-001 \
  --output-json /tmp/jit-hls-report.json \
  --asset-id synthetic-portrait \
  --asset-version fixture-v1 \
  --profile-version h264-aac-360x640-v1 \
  --protocol hls \
  --source-retention-hours 720 \
  --package-retention-hours 720 \
  --jit-cache-retention-hours 24 \
  --warm-requests 10
```

Run the same workload for DASH and repeat at least five times, alternating
pre-packaged and JIT run order on the same host. Preserve the JSON reports,
input checksum, FFmpeg/FFprobe build strings, host/hardware, filesystem/cache
state, package profile, and retention assumptions. Do not mix results from
different profiles or source hashes.

The runner requires an already encoded input. The output command includes
`-c copy` for both HLS and DASH. A missing/unsupported stream or packager
failure is a hard failure; there is no encode fallback.

## Measurement definitions and limitations

| Measure | Current prototype evidence | Not established |
|---|---|---|
| Storage bytes | Actual source and package directory byte counts | Object-store billing, replication, metadata/minimum object charges |
| Storage byte-hours | Measured bytes multiplied by explicit source/package/cache retention hours | Actual byte-hour telemetry, request distribution, lifecycle/eviction behavior |
| Cold packaging latency | Wall time for one FFmpeg copy-only package build | Production request queueing, worker startup, multi-tenant contention |
| Warm cache | Local immutable cache lookup latency and hit ratio | CDN/origin-shield hit ratio or cross-region cache behavior |
| First byte | HTTP loopback time until first manifest byte | Client-network TTFB, CDN TTFB, time-to-first-frame |
| CPU | Child process CPU seconds reported by the OS | Priced compute, full worker overhead, CPU throttling/host contention |
| Playback | FFprobe can inspect generated HLS/DASH manifests and media streams in CI | Real browser, iOS/tvOS, Android, smart-TV and hardware-decoder playback |
| Audio/subtitles | Track start-time helper rejects missing or out-of-tolerance starts | Full segment-boundary drift, language selection, subtitle rendering and long-duration sync |
| DRM | Non-clear profiles are rejected | FairPlay, Widevine, PlayReady, CENC/CBCS, key rotation, license acquisition or secure key delivery |

The benchmark's storage byte-hours are a transparent scenario model, not an
invoice. CPU seconds are a resource measurement, not a cloud-cost estimate.
Loopback HTTP first-byte is not network TTFB. Synthetic fixtures are not a
representative catalogue and are not licensed media evidence for product
economics. The harness must be run on a defined representative workload
before making any savings or rollout claim.

## Compatibility and operational constraints

- **DRM:** the prototype is clear-only. Preserve conventional encrypted
  packaging for FairPlay/Widevine/PlayReady and all key rotation workflows.
- **Legacy HLS:** CMAF/fMP4 is not assumed to cover MPEG-TS-only devices.
  Preserve the existing package and its established fallback behavior.
- **Audio/subtitles:** track start alignment is a necessary but insufficient
  check. Validate full timelines, segment boundaries, language/rendition
  signaling, WebVTT/IMSC behavior, and real players.
- **Cache isolation:** cache keys include all known byte-affecting profile and
  source versions. A new asset/profile/packager/DRM key/segment-duration version receives a new
  immutable key. Invalidation removes only the exact key, never a shared prefix.
- **Publication:** readers see only a fully built directory. Production
  adoption must additionally preserve durable Attempt ownership, validation,
  storage readback, manifest-last publication, authorization, and cleanup.
- **Fallback:** any unsupported profile or JIT error must route to the existing
  pre-packaged artifact. Do not retry by encoding on the playback request path.
- **Observability:** production rollout would need bounded-cardinality metrics
  for cache hits/misses, packaging duration, CPU, failure class, fallback
  count, package version, and origin response latency. Asset IDs and URLs
  belong in traces/logs, not metric labels.

## Interoperability references

- Apple Developer, [About CMAF with HLS](https://developer.apple.com/documentation/http-live-streaming/about-the-common-media-application-format-with-http-live-streaming-hls): CMAF track/header/fragment model, per-track HLS media playlists, initialization-map references, and audio/subtitle rendition signaling.
- CTA-5005, [Web Application Video Ecosystem — DASH/HLS Interop](https://cdn.cta.tech/cta/media/media/resources/standards/pdfs/cta-5005-final.pdf): constraints for reusing DASH CMAF addressable objects with HLS and vice versa, including byte-range addressability and segment-object aggregation.

These specifications establish interoperability constraints; they do not prove that this prototype's outputs pass a particular DRM or player implementation.

## Current conclusion and next evidence gate

This change establishes an isolated copy-only packager/cache prototype and
regression coverage for deterministic keys/manifests, concurrent fills,
atomic publication, invalidation, versioning, failure cleanup, fallback, and
audio/video/subtitle start-time alignment.

It does **not** establish that JIT reduces storage byte-hours after cache
retention and packaging compute, that the shared CMAF segment set is
interoperable across both protocols, or that DRM/player behavior is correct.
No savings percentage or production recommendation is justified.

Before enabling any request-path use, the next gate is a representative,
licensed workload with matched source/profile and at least five repeated
runs, CDN-equivalent cache simulation, real network TTFB, complete player
matrix, DRM/key-rotation tests, and storage/compute prices reconciled to
provider billing. Until then, the conventional package path remains the
production path and the only supported fallback.
