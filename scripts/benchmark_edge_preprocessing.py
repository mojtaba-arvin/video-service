#!/usr/bin/env python3
"""Paired cloud-reference / bounded edge keyframe preprocessing pilot."""
import argparse
import hashlib
import json
import math
import os
import platform
import signal
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import psutil

POLICY_VERSION = "keyframes-v1"
MAX_FRAME_WIDTH = 640
MAX_FRAME_COUNT = 8


class BenchmarkError(RuntimeError):
    pass


class EdgeDisconnectedError(RuntimeError):
    pass


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sampling_rate(duration, interval, max_frames):
    if duration <= 0 or interval <= 0 or max_frames < 1 or max_frames > MAX_FRAME_COUNT:
        raise ValueError("duration and interval must be positive; max_frames must be 1..{}".format(MAX_FRAME_COUNT))
    return min(1.0 / interval, max_frames / duration)


def _usage(process):
    cpu = 0.0
    rss = 0
    try:
        children = [process] + process.children(recursive=True)
    except psutil.Error:
        children = [process]
    for child in children:
        try:
            times = child.cpu_times()
            cpu += times.user + times.system
            rss += child.memory_info().rss
        except psutil.Error:
            continue
    return cpu, rss


def _run_bounded(command, timeout, first_pattern=None):
    started = time.monotonic()
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, start_new_session=True,
    )
    root = psutil.Process(process.pid)
    cpu_peak = 0.0
    rss_peak = 0
    first_ms = None
    while process.poll() is None:
        cpu, rss = _usage(root)
        cpu_peak = max(cpu_peak, cpu)
        rss_peak = max(rss_peak, rss)
        if first_pattern and first_ms is None and list(Path(first_pattern).parent.glob(Path(first_pattern).name)):
            first_ms = (time.monotonic() - started) * 1000
        if time.monotonic() - started > timeout:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
            stderr = process.stderr.read().decode("utf-8", "replace")[-3000:]
            raise BenchmarkError("command timed out: {}".format(stderr))
        time.sleep(0.02)
    stderr = process.stderr.read().decode("utf-8", "replace")[-3000:]
    cpu, rss = _usage(root)
    cpu_peak = max(cpu_peak, cpu)
    rss_peak = max(rss_peak, rss)
    if process.returncode:
        raise BenchmarkError("command exited {}: {}".format(process.returncode, stderr))
    elapsed = (time.monotonic() - started) * 1000
    if first_pattern and first_ms is None and list(Path(first_pattern).parent.glob(Path(first_pattern).name)):
        first_ms = elapsed
    return {"elapsed_ms": elapsed, "first_ms": first_ms, "cpu_seconds": cpu_peak, "peak_rss_bytes": rss_peak}


def _version(binary):
    result = subprocess.run(
        [binary, "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        check=True, timeout=15,
    )
    return result.stdout.decode("utf-8", "replace").splitlines()[0]


def probe(input_path, ffprobe, timeout):
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "format=duration,format_name,size,bit_rate:stream=index,codec_type,codec_name,width,height,avg_frame_rate",
         "-of", "json", str(input_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=timeout,
    )
    try:
        data = json.loads(result.stdout.decode("utf-8"))
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as error:
        raise BenchmarkError("FFprobe returned incomplete metadata") from error
    if not math.isfinite(duration) or duration <= 0:
        raise BenchmarkError("media duration must be finite and positive")
    return data, duration


def run_preprocessing(input_path, output_dir, mode, args):
    source = Path(input_path).resolve()
    output = Path(output_dir)
    if output.exists():
        raise BenchmarkError("refusing to overwrite {}".format(output))
    output.mkdir(parents=True)
    started = time.monotonic()
    source_hash = sha256_file(source)
    source_bytes = source.stat().st_size
    metadata, duration = probe(source, args.ffprobe, args.timeout)
    probe_ms = (time.monotonic() - started) * 1000
    rate = sampling_rate(duration, args.interval, args.max_frames)
    frames_dir = output / "frames"
    frames_dir.mkdir()
    pattern = frames_dir / "frame-%03d.jpg"
    metrics = _run_bounded(
        [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", str(source), "-vf",
         "fps={:.12g},scale='min({},iw)':-2".format(rate, MAX_FRAME_WIDTH),
         "-frames:v", str(args.max_frames), "-q:v", "3", str(pattern)],
        args.timeout, str(frames_dir / "frame-*.jpg"),
    )
    frames = sorted(frames_dir.glob("frame-*.jpg"))
    if not frames:
        raise BenchmarkError("preprocessing produced no keyframe samples")
    records = [{"name": p.name, "sha256": sha256_file(p), "size_bytes": p.stat().st_size} for p in frames]
    manifest = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "mode": mode,
        "source": {
            "id": args.source_id, "url": args.source_url, "sha256": source_hash,
            "size_bytes": source_bytes, "license": args.license,
            "attribution": args.attribution,
        },
        "parameters": {
            "interval_seconds": args.interval, "max_frames": args.max_frames,
            "max_frame_width": MAX_FRAME_WIDTH, "max_frame_count": MAX_FRAME_COUNT, "sampling_rate_fps": rate,
            "ffmpeg_version": _version(args.ffmpeg), "ffprobe_version": _version(args.ffprobe),
        },
        "input_metadata": metadata,
        "frames": records,
    }
    manifest_path = output / "preprocessing-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload_bytes = manifest_path.stat().st_size + sum(item["size_bytes"] for item in records)
    return {
        "mode": mode, "source_sha256": source_hash, "source_bytes": source_bytes,
        "transfer_bytes": source_bytes if mode == "cloud" else payload_bytes,
        "payload_bytes": payload_bytes,
        "time_to_first_result_ms": probe_ms + metrics["first_ms"] if metrics["first_ms"] is not None else None,
        "completion_latency_ms": (time.monotonic() - started) * 1000,
        "cpu_seconds": metrics["cpu_seconds"], "peak_rss_bytes": metrics["peak_rss_bytes"],
        "frame_count": len(records), "frame_hashes": [item["sha256"] for item in records],
        "manifest": str(manifest_path), "network_transfer_time_ms": None,
    }


def run_with_cloud_fallback(edge_operation, cloud_operation):
    try:
        return {"selected_path": "edge", "result": edge_operation()}
    except EdgeDisconnectedError:
        return {"selected_path": "cloud_fallback", "fallback_reason": "edge_disconnected",
                "result": cloud_operation()}


def benchmark(args):
    source = Path(args.input).resolve()
    if not source.is_file():
        raise BenchmarkError("input file does not exist")
    root = Path(args.output_dir).resolve()
    if root.exists():
        raise BenchmarkError("refusing to overwrite output directory")
    root.mkdir(parents=True)
    runs = []
    for index in range(args.repetitions):
        run_root = root / "run-{:02d}".format(index + 1)
        order = ("cloud", "edge") if index % 2 == 0 else ("edge", "cloud")
        measured = {}
        for mode in order:
            measured[mode] = run_preprocessing(source, run_root / mode, mode, args)
        cloud, edge = measured["cloud"], measured["edge"]
        runs.append({
            "run": index + 1, "order": list(order), "cloud": cloud, "edge": edge,
            "frame_hash_parity": cloud["frame_hashes"] == edge["frame_hashes"],
            "transfer_bytes_delta": cloud["transfer_bytes"] - edge["transfer_bytes"],
        })
    fallback = None
    if args.exercise_disconnection:
        test_root = root / "disconnection-test"
        def disconnect():
            run_preprocessing(source, test_root / "edge", "edge", args)
            raise EdgeDisconnectedError("simulated disconnect before hand-off")
        fallback = run_with_cloud_fallback(
            disconnect,
            lambda: run_preprocessing(source, test_root / "cloud-fallback", "cloud", args),
        )
        if fallback["result"]["source_sha256"] != sha256_file(source):
            raise BenchmarkError("fallback did not preserve source identity")
    report = {
        "schema_version": 1, "run_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "execution_environment": {
            "platform": platform.platform(), "python": sys.version.split()[0],
            "machine": platform.machine(), "edge_hardware_separately_provisioned": False,
        },
        "methodology": {
            "workload": "VOD intake preview: metadata probe and bounded sampled JPEG keyframes",
            "policy_version": POLICY_VERSION, "repetitions": args.repetitions,
            "same_input_and_sampling_policy": True,
            "cloud_transfer_bytes": "full source media bytes at hand-off boundary",
            "edge_transfer_bytes": "manifest plus sampled JPEG bytes at hand-off boundary",
            "network_transfer_time_ms": None,
            "quality_measure": "exact SHA-256 parity of sampled JPEG outputs; not perceptual quality",
            "failure_rate_scope": "zero failures among completed paired attempts; any path failure aborts the run and fails CI; injected disconnect is reported separately",
            "limitations": [
                "network transfer time, provider egress charges and real edge hardware are not measured",
                "resource measurements cover FFmpeg child processes; cloud storage/control-plane costs are excluded",
                "one public animated trailer is a pilot input, not a representative production corpus",
            ],
        },
        "summary": {
            "all_frame_hashes_match": all(run["frame_hash_parity"] for run in runs),
            "cloud_completion_latency_median_ms": statistics.median([r["cloud"]["completion_latency_ms"] for r in runs]),
            "edge_completion_latency_median_ms": statistics.median([r["edge"]["completion_latency_ms"] for r in runs]),
            "cloud_transfer_bytes_median": statistics.median([r["cloud"]["transfer_bytes"] for r in runs]),
            "edge_transfer_bytes_median": statistics.median([r["edge"]["transfer_bytes"] for r in runs]),
            "measured_network_savings": False, "generalized_savings_claim_permitted": False,
        },
        "failure_metrics": {
            "cloud_attempts": args.repetitions,
            "cloud_failures": 0,
            "cloud_failure_rate": 0.0,
            "edge_preprocessing_attempts": args.repetitions,
            "edge_preprocessing_failures": 0,
            "edge_preprocessing_failure_rate": 0.0,
            "fault_injection": {
                "edge_disconnects_injected": 1 if args.exercise_disconnection else 0,
                "cloud_fallbacks_attempted": 1 if args.exercise_disconnection else 0,
                "cloud_fallbacks_succeeded": 1 if fallback and fallback["selected_path"] == "cloud_fallback" else 0,
            },
        },
        "runs": runs, "disconnection_recovery": fallback,
    }
    report_path = root / "benchmark-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "summary": report["summary"]}, indent=2))
    if not report["summary"]["all_frame_hashes_match"]:
        raise BenchmarkError("cloud/edge frame output parity failed")
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-id", default="sintel-trailer-1080p")
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--license", required=True)
    parser.add_argument("--attribution", required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--max-frames", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--exercise-disconnection", action="store_true")
    args = parser.parse_args(argv)
    if args.interval <= 0 or args.max_frames < 1 or args.max_frames > MAX_FRAME_COUNT or args.repetitions < 1 or args.timeout < 1 or args.timeout > 3600:
        parser.error("interval/repetitions must be positive; max-frames must be 1..8 and timeout 1..3600 seconds")
    return args


if __name__ == "__main__":
    try:
        benchmark(parse_args())
    except (BenchmarkError, OSError, subprocess.SubprocessError) as error:
        print("edge benchmark failed: {}".format(error), file=sys.stderr)
        raise SystemExit(1)
