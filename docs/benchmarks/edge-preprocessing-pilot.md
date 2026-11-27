# Edge preprocessing pilot (Issue #83)

## Decision and workload

This is a bounded experiment, not an Edge fleet or production routing feature.
The target workload is VOD intake preview: inspect media metadata and produce a
small, bounded set of JPEG keyframes for an operator/preview workflow. Final
transcoding, durable Workflow state, Artifact registration and publication
remain in the cloud.

The first permitted public workload is the 52-second, 1920x1080 Sintel trailer
published by the Blender Foundation and hosted by Wikimedia Commons:

- Source page: https://commons.wikimedia.org/wiki/File:Sintel_trailer-1080p.ogv
- Direct media: https://upload.wikimedia.org/wikipedia/commons/0/06/Sintel_trailer-1080p.ogv
- Rights: Creative Commons Attribution 3.0 Unported (CC BY 3.0).
- Required credit: © copyright Blender Foundation | www.sintel.org
- Changes: keyframes are extracted; source media is not modified.

The source page reports a 52-second, 1920x1080, 42.39 MB file and identifies
the Blender Foundation as author and CC BY 3.0 as its license. The workflow
records the downloaded source SHA-256 with every report. This one animated
trailer is a real, permitted pilot input, but it is not representative of
sports, user-generated content, long-form VOD, live media, other codecs or
edge hardware.

## Compared paths

Both paths use the same source hash, FFprobe inspection, sampling policy
(keyframes-v1), frame-count bound, width cap and FFmpeg parameters.

1. Cloud-only reference: the complete source is transferred to the cloud
   boundary; FFprobe and sampled-keyframe extraction run in the cloud.
2. Edge preprocessing prototype: FFprobe and bounded keyframe extraction run
   at the source-side boundary; only the provenance manifest and sampled JPEGs
   are transferred to the cloud boundary. Cloud remains responsible for
   accepting the hand-off, durable state, transcoding and publication.

The prototype does not introduce a network listener, remote worker, arbitrary
command interface, lease, or control-plane mutation. The benchmark models
transfer bytes as the exact payload size at the boundary. It does not measure
network transfer time, provider egress billing, cloud storage cost or separately
provisioned edge hardware. Time-to-first-result and completion latency are
local process timings, not end-to-end network SLOs.

## Reproduction

Requires Python 3.9+, FFmpeg, FFprobe and the already-pinned psutil dependency.
The workflow runs on a GitHub-hosted Ubuntu runner and stores raw JSON evidence
as a workflow artifact.

```sh
curl -fL --retry 3 \
  'https://upload.wikimedia.org/wikipedia/commons/0/06/Sintel_trailer-1080p.ogv' \
  -o /tmp/sintel-trailer-1080p.ogv

python scripts/benchmark_edge_preprocessing.py \
  --input /tmp/sintel-trailer-1080p.ogv \
  --output-dir /tmp/edge-pilot-results \
  --source-url 'https://commons.wikimedia.org/wiki/File:Sintel_trailer-1080p.ogv' \
  --license 'CC-BY-3.0' \
  --attribution '© copyright Blender Foundation | www.sintel.org' \
  --repetitions 3 \
  --exercise-disconnection
```

The report contains paired cloud and edge runs per repetition, alternating
execution order to reduce order/cache bias, plus a disconnection/fallback
exercise. Each preprocessing manifest records source SHA-256 and size,
license/attribution, metadata, sampling policy/version, parameters,
FFmpeg/FFprobe versions and every output frame SHA-256. The report records
transfer bytes, time-to-first-frame, completion latency, FFmpeg CPU seconds,
peak RSS, output parity and explicit unknown dimensions. Successful reports show
zero failures among completed paired attempts; any path failure aborts the run
and fails CI, while the injected disconnect/fallback is counted separately.

The source SHA is computed from the downloaded Commons file and retained in
the report rather than guessed in source control. Comparisons are invalid if
the source SHA, FFmpeg build, runner hardware or parameters differ.

## Failure and recovery

- FFprobe/FFmpeg subprocesses have a hard timeout and process-group termination.
- Sampling is capped by both interval and max_frames; output width is capped.
- Existing output directories are never overwritten.
- Simulated edge disconnection after preprocessing causes the caller to discard
  the edge hand-off and rerun from the original source through the cloud path.
  The source SHA is checked again; edge processing commits no durable workflow
  state or public artifact.
- Non-disconnection errors fail closed. They do not silently become cloud
  fallback, hiding corrupt input or a broken FFmpeg installation.
- Unit tests cover disconnect fallback and unrelated failures.

## Evidence and Go/No-Go

A positive result is only a same-host, single-workload preprocessing signal.
It is not evidence that real edge execution is faster, cheaper or more reliable.
A Go decision requires a separately approved representative corpus and
controlled edge host/network, repeated paired runs, measured upload and cloud
queue time, actual resource/energy metering, downstream preview quality/utility,
failure/recovery rates, security review, and comparison of coordination and
support overhead against measured benefit.

Do not add leases until an experiment requires independently scheduled remote
edge agents. Do not change production routing or enable any feature flag from
this pilot.
