#!/usr/bin/env python3
"""Repeatable matched-VMAF CPU benchmark for H.264, HEVC and AV1 encoders.

The input corpus must contain media that the operator is licensed to benchmark.
Synthetic correctness fixtures are intentionally not treated as representative
perceptual-quality evidence.
"""

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
VIDEO_STREAMING_ROOT = REPOSITORY_ROOT / "video-streaming"
if str(VIDEO_STREAMING_ROOT) not in sys.path:
    sys.path.insert(0, str(VIDEO_STREAMING_ROOT))

from video_streaming.execution.process_control import run_cancellable_process


CODECS = (
    {
        "codec": "h264",
        "encoder": "libx264",
        "preset": "medium",
        "crf_min": 18,
        "crf_max": 40,
    },
    {
        "codec": "hevc",
        "encoder": "libx265",
        "preset": "medium",
        "crf_min": 20,
        "crf_max": 42,
    },
    {
        "codec": "av1",
        "encoder": "libsvtav1",
        "preset": "6",
        "crf_min": 24,
        "crf_max": 50,
    },
)

BENCHMARK_SCHEMA_VERSION = 1
EXCLUDED_COST_DIMENSIONS = [
    "gpu_compute",
    "network_egress",
    "provider_storage_billing",
    "provider_transcoding_billing",
    "worker_idle_and_control_plane",
]


class BenchmarkError(RuntimeError):
    """Raised when a benchmark cannot produce an auditable measurement."""


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(manifest_path):
    """Load and validate a licensed, versioned reference-media manifest."""
    manifest_path = Path(manifest_path).expanduser().resolve()
    if not manifest_path.is_file():
        raise BenchmarkError("Source manifest does not exist: {}".format(manifest_path))

    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError("Could not parse source manifest: {}".format(exc))

    if data.get("schema_version") != 1:
        raise BenchmarkError("Source manifest schema_version must be 1")
    dataset_id = str(data.get("dataset_id") or "").strip()
    if not dataset_id:
        raise BenchmarkError("Source manifest requires dataset_id")
    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        raise BenchmarkError("Source manifest requires a non-empty sources array")

    normalized = []
    seen_ids = set()
    for item in sources:
        source_id = str(item.get("id") or "").strip()
        content_class = str(item.get("content_class") or "").strip()
        license_name = str(item.get("license") or "").strip()
        if not source_id or not content_class or not license_name:
            raise BenchmarkError(
                "Every source requires id, content_class and license provenance"
            )
        if source_id in seen_ids:
            raise BenchmarkError("Duplicate source id: {}".format(source_id))
        seen_ids.add(source_id)

        source_path = Path(str(item.get("path") or "")).expanduser()
        if not source_path.is_absolute():
            source_path = manifest_path.parent / source_path
        source_path = source_path.resolve()
        if not source_path.is_file():
            raise BenchmarkError(
                "Source media does not exist for {}: {}".format(
                    source_id, source_path
                )
            )
        source_hash = sha256_file(source_path)
        expected_hash = str(item.get("sha256") or "").lower()
        if expected_hash and expected_hash != source_hash:
            raise BenchmarkError(
                "SHA-256 mismatch for source {}".format(source_id)
            )
        normalized.append({
            "id": source_id,
            "path": str(source_path),
            "content_class": content_class,
            "license": license_name,
            "sha256": source_hash,
        })

    return {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "sources": normalized,
    }


def run_command(command, timeout_seconds):
    try:
        result = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise BenchmarkError(
            "Command exceeded {} seconds: {}".format(timeout_seconds, command[0])
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise BenchmarkError(
            "Command failed ({}): {}\n{}".format(
                exc.returncode, " ".join(str(part) for part in command), detail[-4000:]
            )
        ) from exc
    return result


def discover_environment(ffmpeg_path, ffprobe_path, vmaf_model):
    version = run_command(
        [ffmpeg_path, "-version"], timeout_seconds=30
    ).stdout.splitlines()[0]
    build_config = run_command(
        [ffmpeg_path, "-buildconf"], timeout_seconds=30
    ).stdout
    encoders = run_command(
        [ffmpeg_path, "-hide_banner", "-encoders"], timeout_seconds=30
    ).stdout
    filters = run_command(
        [ffmpeg_path, "-hide_banner", "-filters"], timeout_seconds=30
    ).stdout
    if "libvmaf" not in filters:
        raise BenchmarkError(
            "This FFmpeg build has no libvmaf filter. Use a pinned FFmpeg build "
            "compiled with libvmaf; do not substitute SSIM/PSNR for this run."
        )

    missing = [
        item["encoder"] for item in CODECS
        if item["encoder"] not in encoders
    ]
    if missing:
        raise BenchmarkError(
            "FFmpeg is missing required encoders: {}".format(", ".join(missing))
        )

    gpu = None
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            gpu = run_command([
                nvidia_smi,
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader",
            ], timeout_seconds=10).stdout.strip()
        except BenchmarkError:
            gpu = "nvidia-smi available but GPU metadata query failed"

    build_hash = hashlib.sha256(build_config.encode("utf-8")).hexdigest()
    return {
        "ffmpeg_version": version,
        "ffmpeg_build_config_sha256": build_hash,
        "ffprobe_version": run_command(
            [ffprobe_path, "-version"], timeout_seconds=30
        ).stdout.splitlines()[0],
        "vmaf_filter": "libvmaf",
        "vmaf_model": vmaf_model,
        "cpu": {
            "processor": platform.processor() or platform.machine(),
            "platform": platform.platform(),
            "logical_cores": os.cpu_count(),
        },
        "gpu": gpu,
        "execution_mode": "software_encoders_cpu_measurement",
    }


def probe_media(ffprobe_path, media_path, timeout_seconds):
    result = run_command([
        ffprobe_path,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,codec_name:"
                        "format=duration,bit_rate,size",
        "-of", "json",
        str(media_path),
    ], timeout_seconds)
    document = json.loads(result.stdout)
    streams = document.get("streams", [])
    if len(streams) != 1:
        raise BenchmarkError(
            "Expected exactly one video stream in {}".format(media_path)
        )
    info = document.get("format", {})
    duration = float(info.get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise BenchmarkError("Invalid source duration: {}".format(media_path))
    return {
        "width": int(streams[0].get("width") or 0),
        "height": int(streams[0].get("height") or 0),
        "frame_rate": streams[0].get("avg_frame_rate"),
        "duration_seconds": duration,
        "codec": streams[0].get("codec_name"),
    }


def measure_vmaf(ffmpeg_path, distorted_path, reference_path,
                 vmaf_model, output_log, timeout_seconds):
    filter_graph = (
        "[0:v:0]setpts=PTS-STARTPTS[dist];"
        "[1:v:0]setpts=PTS-STARTPTS[ref];"
        "[dist][ref]libvmaf=model=version={}:log_fmt=json:log_path={}"
    ).format(vmaf_model, output_log)
    run_command([
        ffmpeg_path,
        "-hide_banner",
        "-nostats",
        "-i", str(distorted_path),
        "-i", str(reference_path),
        "-lavfi", filter_graph,
        "-f", "null",
        "-",
    ], timeout_seconds)
    try:
        document = json.loads(Path(output_log).read_text(encoding="utf-8"))
        metric = document["pooled_metrics"]["vmaf"]
        score = float(metric.get("mean"))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BenchmarkError("Could not read pooled VMAF result: {}".format(exc))
    if not math.isfinite(score):
        raise BenchmarkError("VMAF returned a non-finite score")
    return score


def encode_and_score(source, source_info, codec, crf, ffmpeg_path,
                     ffprobe_path, vmaf_model, temp_dir, timeout_seconds):
    output_path = Path(temp_dir) / (
        "{}-crf-{}-{}.mp4".format(codec["codec"], crf, uuid.uuid4().hex[:8])
    )
    encode_command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", source["path"],
        "-map", "0:v:0",
        "-an", "-sn", "-dn",
        "-c:v", codec["encoder"],
        "-preset", codec["preset"],
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ]

    usage_samples = []
    started = time.monotonic()
    run_cancellable_process(
        encode_command,
        timeout_seconds=timeout_seconds,
        poll_interval=0.1,
        on_usage=usage_samples.append,
    )
    encode_wall_seconds = time.monotonic() - started
    cpu_seconds = max(
        (item["cpu_seconds"] for item in usage_samples),
        default=0.0,
    )
    peak_memory_bytes = max(
        (item["peak_memory_bytes"] for item in usage_samples),
        default=None,
    )

    output_info = probe_media(ffprobe_path, output_path, timeout_seconds)
    output_bytes = output_path.stat().st_size
    vmaf_log = Path(temp_dir) / (
        "{}-crf-{}-{}.vmaf.json".format(codec["codec"], crf, uuid.uuid4().hex[:8])
    )
    vmaf = measure_vmaf(
        ffmpeg_path,
        output_path,
        source["path"],
        vmaf_model,
        vmaf_log,
        timeout_seconds,
    )
    return {
        "codec": codec["codec"],
        "encoder": codec["encoder"],
        "preset": codec["preset"],
        "crf": crf,
        "vmaf": vmaf,
        "encode_wall_seconds": encode_wall_seconds,
        "encode_cpu_seconds": cpu_seconds,
        "encode_peak_memory_bytes": peak_memory_bytes,
        "output_sha256": sha256_file(output_path),
        "input_duration_seconds": source_info["duration_seconds"],
        "throughput_x_realtime": (
            source_info["duration_seconds"] / encode_wall_seconds
            if encode_wall_seconds > 0 else None
        ),
        "output_bytes": output_bytes,
        "output_bitrate": output_info.get("bit_rate"),
        "output_codec": output_info.get("codec"),
        "output_width": output_info.get("width"),
        "output_height": output_info.get("height"),
    }


def median_or_none(values):
    clean = [float(value) for value in values if value is not None]
    return statistics.median(clean) if clean else None


def run_benchmark(args):
    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if not ffmpeg_path or not ffprobe_path:
        raise BenchmarkError("FFmpeg and FFprobe must both be available on PATH")
    if args.repetitions < 1:
        raise BenchmarkError("--repetitions must be at least 1")
    if args.max_crf_evaluations < 1:
        raise BenchmarkError("--max-crf-evaluations must be at least 1")
    if not (0 < args.target_vmaf <= 100):
        raise BenchmarkError("--target-vmaf must be greater than 0 and at most 100")

    manifest = load_manifest(args.manifest)
    environment = discover_environment(
        ffmpeg_path, ffprobe_path, args.vmaf_model
    )
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_path = output_dir / "measurements.jsonl"
    report_path = output_dir / "benchmark-report.json"
    summaries = []

    with raw_path.open("w", encoding="utf-8") as raw:
        def record(payload):
            raw.write(json.dumps(payload, sort_keys=True) + "\n")
            raw.flush()

        for source in manifest["sources"]:
            source_info = probe_media(
                ffprobe_path, source["path"], args.timeout_seconds
            )
            source_result = {
                "source_id": source["id"],
                "source_sha256": source["sha256"],
                "content_class": source["content_class"],
                "license": source["license"],
                "source_info": source_info,
                "codecs": [],
            }

            for codec in CODECS:
                low, high = codec["crf_min"], codec["crf_max"]
                search_trials = []
                qualifying = []
                for iteration in range(args.max_crf_evaluations):
                    if low > high:
                        break
                    crf = (low + high) // 2
                    with tempfile.TemporaryDirectory(
                        prefix="codec-bench-search-"
                    ) as temp_dir:
                        measurement = encode_and_score(
                            source, source_info, codec, crf,
                            ffmpeg_path, ffprobe_path, args.vmaf_model,
                            temp_dir, args.timeout_seconds,
                        )
                    measurement.update({
                        "phase": "quality_search",
                        "source_id": source["id"],
                        "source_sha256": source["sha256"],
                        "iteration": iteration + 1,
                        "target_vmaf": args.target_vmaf,
                    })
                    search_trials.append(measurement)
                    record(measurement)

                    if measurement["vmaf"] >= args.target_vmaf:
                        qualifying.append(measurement)
                        low = crf + 1
                    else:
                        high = crf - 1

                if qualifying:
                    selected = max(qualifying, key=lambda item: item["crf"])
                    target_achieved = True
                else:
                    selected = max(search_trials, key=lambda item: item["vmaf"])
                    target_achieved = False

                repeated = []
                for repetition in range(args.repetitions):
                    with tempfile.TemporaryDirectory(
                        prefix="codec-bench-repeat-"
                    ) as temp_dir:
                        measurement = encode_and_score(
                            source, source_info, codec,
                            selected["crf"], ffmpeg_path, ffprobe_path,
                            args.vmaf_model, temp_dir, args.timeout_seconds,
                        )
                    measurement.update({
                        "phase": "selected_crf_repetition",
                        "source_id": source["id"],
                        "source_sha256": source["sha256"],
                        "repetition": repetition + 1,
                        "target_vmaf": args.target_vmaf,
                    })
                    repeated.append(measurement)
                    record(measurement)

                summary = {
                    "codec": codec["codec"],
                    "encoder": codec["encoder"],
                    "preset": codec["preset"],
                    "selected_crf": selected["crf"],
                    "target_vmaf": args.target_vmaf,
                    "target_achieved": target_achieved,
                    "search_evaluations": len(search_trials),
                    "repetitions": len(repeated),
                    "median_vmaf": median_or_none(
                        item["vmaf"] for item in repeated
                    ),
                    "vmaf_population_stddev": (
                        statistics.pstdev(item["vmaf"] for item in repeated)
                        if len(repeated) > 1 else 0.0
                    ),
                    "median_encode_wall_seconds": median_or_none(
                        item["encode_wall_seconds"] for item in repeated
                    ),
                    "encode_wall_population_stddev": (
                        statistics.pstdev(
                            item["encode_wall_seconds"] for item in repeated
                        ) if len(repeated) > 1 else 0.0
                    ),
                    "median_encode_cpu_seconds": median_or_none(
                        item["encode_cpu_seconds"] for item in repeated
                    ),
                    "median_output_bytes": median_or_none(
                        item["output_bytes"] for item in repeated
                    ),
                    "median_output_bitrate": median_or_none(
                        item["output_bitrate"] for item in repeated
                    ),
                    "search_trials": search_trials,
                    "repetitions_raw": repeated,
                }
                source_result["codecs"].append(summary)

            summaries.append(source_result)

    report = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": "matched-vmaf-codec-cost-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_vmaf": args.target_vmaf,
        "vmaf_model": args.vmaf_model,
        "repetitions": args.repetitions,
        "max_crf_evaluations": args.max_crf_evaluations,
        "dataset": manifest,
        "environment": environment,
        "measurements_file": str(raw_path),
        "excluded_cost_dimensions": EXCLUDED_COST_DIMENSIONS,
        "decision_policy": (
            "No encoder default is selected automatically. Compare only results "
            "that reached the same VMAF target and review compatibility and "
            "variance before changing production policy."
        ),
        "results": summaries,
    }
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report_path, raw_path, summaries


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--target-vmaf", type=float, default=93.0)
    parser.add_argument("--vmaf-model", default="vmaf_v0.6.1")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-crf-evaluations", type=int, default=7)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()

    report_path, raw_path, summaries = run_benchmark(args)
    print(json.dumps({
        "report": str(report_path),
        "raw_measurements": str(raw_path),
        "source_count": len(summaries),
        "codec_count": len(CODECS),
        "all_targets_achieved": all(
            codec["target_achieved"]
            for source in summaries
            for codec in source["codecs"]
        ),
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, BenchmarkError, subprocess.TimeoutExpired) as exc:
        print("Codec benchmark failed: {}".format(exc), file=sys.stderr)
        sys.exit(1)
