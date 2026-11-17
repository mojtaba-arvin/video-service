---
name: cost-codec-benchmarking
description: Use when comparing codecs, profiles, packaging, worker types, resource usage, throughput, quality, latency, or attributable cost.
---

# Cost and codec benchmarking

Use synthetic-vod-v1 for deterministic regression tests only. Perceptual
quality conclusions need a representative, legally redistributable media
corpus with documented provenance and content classes.

Record input checksums and rights, codec/encoder versions, FFmpeg build,
flags/profile/preset, hardware, warm/cold cache policy, repetitions, and raw
results.

Measure separately: queue wait, worker startup, wall time, CPU seconds, peak
RSS, scratch-disk peak, bytes read/written, output bytes, retries, failures,
and quality metrics such as VMAF where applicable.

Separate measured resource usage, provider-price estimates with timestamped
price sheets, and actual reconciled billing. Never infer cloud cost from CPU
time alone or claim codec savings from output size without quality,
compatibility, and compute trade-offs.

Compare the same input corpus under the same output quality/compatibility
constraints. Alternate run order where cache/thermal effects matter. Report
distributions such as p50/p95, not one favorable run. Publish machine-readable
raw results and methodology.
