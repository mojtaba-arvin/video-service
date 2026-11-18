# Reproducible codec and cost benchmark

## Purpose

Compare software H.264, HEVC and AV1 encoders at a matched perceptual-quality
target. The harness records repeatable observations; it does not select a
production default automatically and does not claim provider billing accuracy.

The repository's `synthetic-vod-v1` is a correctness fixture. It is not a
representative perceptual-quality dataset. Use media that the operator is
licensed to benchmark, with an explicit source manifest.

## Source manifest

Create a JSON manifest (outside source control when it references proprietary
or licensed content):

~~~json
{
  "schema_version": 1,
  "dataset_id": "licensed-vod-reference-v1",
  "sources": [
    {
      "id": "sports-clip-01",
      "path": "/data/approved/sports-clip-01.mp4",
      "content_class": "sports",
      "license": "internal-cleared-for-benchmarking",
      "sha256": "REPLACE_WITH_THE_64_CHARACTER_SHA256"
    }
  ]
}
~~~

The script resolves each path relative to the manifest when it is not
absolute, requires a content class and license/provenance label, computes the
SHA-256 digest, and rejects a supplied digest that does not match. Store the
manifest, raw measurements and report with the benchmark run; do not commit
customer media or proprietary source manifests.

## Run

The FFmpeg build must include `libx264`, `libx265`, `libsvtav1`, FFprobe,
and the `libvmaf` filter. Pin the actual FFmpeg package/build in the
benchmark environment. Example:

~~~sh
python scripts/benchmark_codecs.py \
  --manifest /secure/benchmark/licensed-vod-reference-v1.json \
  --output-dir /secure/benchmark/results/run-001 \
  --target-vmaf 93 \
  --vmaf-model vmaf_v0.6.1 \
  --repetitions 3 \
  --max-crf-evaluations 7 \
  --timeout-seconds 3600
~~~

For each source and software encoder, the harness searches the encoder's CRF
range for the highest CRF that still reaches the target VMAF, then repeats the
selected CRF. Search and repetition records contain source ID/hash, encoder,
preset, CRF, VMAF, encode wall time, process-tree CPU seconds, sampled peak RSS,
output SHA-256/size/bitrate and source duration. The summary reports medians and
population standard deviations. Outputs are temporary; their hashes and
measurement metadata are recorded in the raw JSONL stream/report, not retained
as media deliverables.

## Outputs

- `measurements.jsonl`: one record for every CRF search evaluation and every
  selected-CRF repetition.
- `benchmark-report.json`: dataset and manifest hashes, FFmpeg/FFprobe version,
  FFmpeg build-config hash, CPU/platform, detected GPU metadata, VMAF model,
  target, repetitions, medians, variance and excluded dimensions.

Do not combine results from different FFmpeg builds, CPU/GPU types, presets,
VMAF models, source hashes or target-quality definitions without identifying
those differences. Re-run the same configuration several times before
interpreting small differences.

## Cost interpretation

The first benchmark implementation measures software encoder wall time, child
CPU seconds, output size/bitrate and VMAF. It records hardware/runtime metadata.
It does not yet meter GPU encoder utilization, network ingress/egress, power,
idle workers, provider storage retention or provider invoices. Therefore it
cannot produce a complete cloud-cost figure.

The workflow's versioned price card separately estimates attempt-level CPU,
measured peak-RSS GiB-seconds and published output GiB. Memory GiB-seconds use
the measured encoder-processing interval when available; otherwise the whole
durable Attempt interval is a conservative fallback. Each attempt persists
the exact card ID/version/rates and component coverage. Partial amounts remain
diagnostic; an aggregate total is reported only when every attempt is fully
priced under a consistent card version and currency. RUNIT is a relative
resource index, not USD/EUR or provider billing.

## Decision gate

No encoder becomes the default from a single run or generic internet ratios.
A codec decision needs matched target VMAF across representative content
classes, repeat-run variance, throughput and output size, decode/player
compatibility, device support and operational constraints. Keep a result as
"not comparable" when a codec cannot reach the target inside its configured
search range.

For the current status and scope boundaries see
[Issue #75](https://github.com/mojtaba-arvin/video-service/issues/75).
