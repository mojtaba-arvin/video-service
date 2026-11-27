#!/usr/bin/env python3
"""Research-only JIT CMAF packaging benchmark. Never used by the request path.

The input is already encoded MP4. FFmpeg is invoked with -c copy; this tool
measures packaging/remuxing only and never performs video/audio re-encoding.
"""
import argparse
import fcntl
import hashlib
import json
import os
import resource
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from video_streaming.ffmpeg.package_validation import validate_streaming_package

SCHEMA_VERSION = 1
PACKAGER_VERSION = "jit-cmaf-prototype-v1"
MANIFESTS = {"hls": "index.m3u8", "dash": "manifest.mpd"}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_inventory(root):
    files = []
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        files.append({
            "path": path.relative_to(root).as_posix(),
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        })
    return {
        "file_count": len(files),
        "total_bytes": sum(item["size_bytes"] for item in files),
        "files": files,
    }


def ffmpeg_version(binary):
    result = subprocess.run(
        [binary, "-version"], check=True, capture_output=True, text=True, timeout=15
    )
    return result.stdout.splitlines()[0]


def build_package(source, output_dir, protocol, ffmpeg="ffmpeg", segment_seconds=2):
    """Package existing encoded streams without codec conversion."""
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("source must be an existing non-empty encoded asset")
    if protocol not in MANIFESTS:
        raise ValueError("unsupported protocol")
    output_dir.mkdir(parents=True, exist_ok=False)
    if protocol == "hls":
        manifest = output_dir / MANIFESTS[protocol]
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-f", "hls", "-hls_time", str(segment_seconds),
            "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(output_dir / "segment_%05d.m4s"),
            "-y", str(manifest),
        ]
    else:
        manifest = output_dir / MANIFESTS[protocol]
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-f", "dash", "-seg_duration", str(segment_seconds),
            "-use_timeline", "1", "-use_template", "1",
            "-init_seg_name", "init-$RepresentationID$.$ext$",
            "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.$ext$",
            "-y", str(manifest),
        ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    if not manifest.is_file() or manifest.stat().st_size == 0:
        raise RuntimeError("packager did not create a non-empty root manifest")
    validation = validate_streaming_package(
        str(output_dir), str(manifest), protocol
    )
    return {
        "manifest": manifest.name,
        "manifest_sha256": sha256_file(manifest),
        "validation": validation,
        **tree_inventory(output_dir),
    }


class AtomicPackageCache:
    """Filesystem prototype for immutable, versioned, atomic package publication."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(source_sha256, protocol, profile_version, packager_version,
                 ffmpeg_build, options=None):
        identity = {
            "schema_version": SCHEMA_VERSION,
            "source_sha256": source_sha256,
            "protocol": protocol,
            "profile_version": profile_version,
            "packager_version": packager_version,
            "ffmpeg_build": ffmpeg_build,
            "options": options or {},
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def get_or_build(self, key, builder):
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("cache key must be a lowercase SHA-256 digest")
        target = self.root / key
        lock_path = self.root / (key + ".lock")
        started = time.monotonic()
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            marker = target / ".complete.json"
            if marker.is_file():
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                if metadata.get("key") != key:
                    raise RuntimeError("cache completion marker does not match key")
                return target, True, (time.monotonic() - started) * 1000
            staging = Path(tempfile.mkdtemp(prefix=key + ".staging-", dir=str(self.root)))
            try:
                builder(staging)
                manifest_names = list(MANIFESTS.values())
                if not any((staging / name).is_file() and (staging / name).stat().st_size
                           for name in manifest_names):
                    raise RuntimeError("builder did not produce a root manifest")
                (staging / ".complete.json").write_text(
                    json.dumps({"schema_version": SCHEMA_VERSION, "key": key},
                               sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                if target.exists():
                    shutil.rmtree(target)
                os.replace(str(staging), str(target))
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            return target, False, (time.monotonic() - started) * 1000

    def invalidate(self, key):
        target = self.root / key
        lock_path = self.root / (key + ".lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            shutil.rmtree(target, ignore_errors=True)


def package_or_fallback(cache, key, builder, fallback_dir):
    """Return a complete JIT package, or the pre-packaged artifact on any JIT error."""
    try:
        path, hit, elapsed_ms = cache.get_or_build(key, builder)
        return {"path": str(path), "mode": "jit-cache-hit" if hit else "jit-cold",
                "elapsed_ms": elapsed_ms}
    except Exception as exc:
        fallback = Path(fallback_dir).resolve()
        if not any((fallback / name).is_file() and (fallback / name).stat().st_size
                   for name in MANIFESTS.values()):
            raise RuntimeError("JIT failed and no valid pre-packaged fallback exists") from exc
        return {"path": str(fallback), "mode": "prepackaged-fallback",
                "elapsed_ms": None, "fallback_reason": type(exc).__name__}


def first_manifest_byte(path):
    started = time.monotonic()
    with Path(path).open("rb") as stream:
        first = stream.read(1)
    if not first:
        raise RuntimeError("manifest is empty")
    return (time.monotonic() - started) * 1000


def cpu_seconds():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def percentile(values, percentile_value):
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = (len(ordered) - 1) * percentile_value / 100.0
    low, high = int(index), min(int(index) + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def cache_residency_hours(request_count, workload_hours, ttl_hours):
    """Deterministic evenly-spaced request model; returns cache occupancy hours."""
    if request_count <= 0 or workload_hours <= 0 or ttl_hours <= 0:
        return 0.0, 0
    if request_count == 1:
        return min(workload_hours, ttl_hours), 0
    spacing = workload_hours / request_count
    hits = max(0, request_count - 1) if spacing <= ttl_hours else 0
    intervals = []
    for index in range(request_count):
        at = index * spacing
        intervals.append((at, min(workload_hours, at + ttl_hours)))
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged), hits


def benchmark_fixture(source, output_root, ffmpeg, repetitions, workload_hours,
                      cache_ttl_hours, hls_requests, dash_requests):
    source = Path(source).resolve()
    source_hash = sha256_file(source)
    build = ffmpeg_version(ffmpeg)
    protocols = {}
    for protocol in ("hls", "dash"):
        prepackaged = output_root / (source.stem + "-prepackaged-" + protocol)
        if prepackaged.exists():
            shutil.rmtree(prepackaged)
        pre_cpu = cpu_seconds()
        pre_started = time.monotonic()
        pre = build_package(source, prepackaged, protocol, ffmpeg)
        pre_latency = (time.monotonic() - pre_started) * 1000
        pre_cpu_seconds = max(0.0, cpu_seconds() - pre_cpu)

        cold_latencies, cold_cpu, warm_latencies, cold_ttfb, warm_ttfb = [], [], [], [], []
        stable_hashes = []
        for repetition in range(repetitions):
            cache_root = output_root / (source.stem + "-jit-" + protocol + "-" + str(repetition))
            if cache_root.exists():
                shutil.rmtree(cache_root)
            cache = AtomicPackageCache(cache_root)
            key = cache.make_key(
                source_hash, protocol, "cmaf-copy-v1", PACKAGER_VERSION, build,
                {"segment_seconds": 2},
            )
            before_cpu = cpu_seconds()
            path, hit, elapsed = cache.get_or_build(
                key, lambda staging: build_package(source, staging, protocol, ffmpeg)
            )
            cpu_delta = max(0.0, cpu_seconds() - before_cpu)
            cold_latencies.append(elapsed)
            cold_cpu.append(cpu_delta)
            cold_ttfb.append(elapsed + first_manifest_byte(path / MANIFESTS[protocol]))
            stable_hashes.append(sha256_file(path / MANIFESTS[protocol]))
            warm_path, warm_hit, warm_elapsed = cache.get_or_build(
                key, lambda staging: build_package(source, staging, protocol, ffmpeg)
            )
            warm_latencies.append(warm_elapsed)
            warm_ttfb.append(warm_elapsed + first_manifest_byte(warm_path / MANIFESTS[protocol]))
            if hit or not warm_hit:
                raise RuntimeError("cache cold/warm state transition was unexpected")

        requests = hls_requests if protocol == "hls" else dash_requests
        resident_hours, modeled_hits = cache_residency_hours(
            requests, workload_hours, cache_ttl_hours
        )
        protocols[protocol] = {
            "prepackaged": {
                "bytes": pre["total_bytes"], "files": pre["file_count"],
                "cold_packaging_ms": pre_latency, "cpu_seconds": pre_cpu_seconds,
                "manifest_sha256": pre["manifest_sha256"],
            },
            "jit": {
                "bytes": pre["total_bytes"],
                "cold_packaging_ms_p50": statistics.median(cold_latencies),
                "cold_packaging_ms_p95": percentile(cold_latencies, 95),
                "warm_cache_lookup_ms_p50": statistics.median(warm_latencies),
                "cold_time_to_first_manifest_byte_ms_p50": statistics.median(cold_ttfb),
                "warm_time_to_first_manifest_byte_ms_p50": statistics.median(warm_ttfb),
                "cpu_seconds_p50": statistics.median(cold_cpu),
                "manifest_deterministic_across_runs": len(set(stable_hashes)) == 1,
                "manifest_sha256_by_run": stable_hashes,
            },
            "workload_model": {
                "requests": requests, "modeled_cache_hits": modeled_hits,
                "modeled_cache_hit_ratio": (modeled_hits / requests) if requests else None,
                "cache_ttl_hours": cache_ttl_hours,
                "cache_resident_hours": resident_hours,
                "prepackaged_byte_hours": pre["total_bytes"] * workload_hours,
                "jit_cache_byte_hours": pre["total_bytes"] * resident_hours,
            },
            "structural_validation": pre["validation"],
        }
    return {
        "fixture": source.name, "source_sha256": source_hash,
        "source_bytes": source.stat().st_size, "ffmpeg_build": build,
        "protocols": protocols,
    }


def run_benchmark(input_dir, output_dir, repetitions=3, workload_hours=24,
                  cache_ttl_hours=1, hls_requests=100, dash_requests=20):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg and FFprobe are required")
    dataset_path = Path(input_dir) / "dataset-manifest.json"
    if not dataset_path.is_file():
        raise RuntimeError("missing generated dataset-manifest.json")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for artifact in dataset.get("artifacts", []):
        source = Path(input_dir) / artifact["filename"]
        if not source.is_file() or sha256_file(source) != artifact["sha256"]:
            raise RuntimeError("source fixture checksum mismatch")
        results.append(benchmark_fixture(
            source, output, ffmpeg, repetitions, workload_hours, cache_ttl_hours,
            hls_requests, dash_requests,
        ))
    report = {
        "schema_version": SCHEMA_VERSION,
        "experiment": "issue-81-jit-cmaf-packaging",
        "packager_version": PACKAGER_VERSION,
        "request_path_reencoding": False,
        "methodology": {
            "input": "already-encoded synthetic MP4 fixtures; all package commands use -c copy",
            "repetitions": repetitions,
            "workload_hours": workload_hours,
            "cache_ttl_hours": cache_ttl_hours,
            "hls_requests_per_asset": hls_requests,
            "dash_requests_per_asset": dash_requests,
            "byte_hours": "package bytes multiplied by modeled cache-residency hours; canonical source bytes are common to both cases and excluded",
            "ttfb": "local filesystem first-byte measurement, not HTTP/CDN/player TTFB",
        },
        "limitations": {
            "representative_storage_savings": "not established by synthetic fixtures",
            "drm": "not tested; no encryption, key rotation, license or entitlement integration",
            "player_compatibility": "not tested; structural manifest/artifact validation only",
            "audio_subtitle_sync": "audio stream packaging is structurally validated; subtitle tracks and player A/V sync require a licensed representative corpus",
            "cache": "local filesystem prototype; not a distributed cache or object-store implementation",
        },
        "fixtures": results,
    }
    report_path = output / "jit-packaging-benchmark.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--workload-hours", type=float, default=24)
    parser.add_argument("--cache-ttl-hours", type=float, default=1)
    parser.add_argument("--hls-requests", type=int, default=100)
    parser.add_argument("--dash-requests", type=int, default=20)
    args = parser.parse_args()
    if args.repetitions < 1 or args.workload_hours <= 0 or args.cache_ttl_hours <= 0:
        parser.error("repetitions and workload/cache durations must be positive")
    report = run_benchmark(
        args.input_dir, args.output_dir, args.repetitions, args.workload_hours,
        args.cache_ttl_hours, args.hls_requests, args.dash_requests,
    )
    print(json.dumps({
        "fixtures": len(report["fixtures"]),
        "report": str(Path(args.output_dir).resolve() / "jit-packaging-benchmark.json"),
        "request_path_reencoding": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()
