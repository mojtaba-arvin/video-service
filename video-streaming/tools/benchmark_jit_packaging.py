#!/usr/bin/env python3
"""Measure copy-only JIT packaging against an existing pre-packaged directory.

The local HTTP first-byte measurement is an origin-loopback proxy for TTFB,
not a CDN/player measurement. Storage byte-hours are scenario calculations
from measured bytes and explicit retention windows, not cloud billing.
"""
import argparse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from video_streaming.ffmpeg.jit_packaging import (
    AtomicPackageCache,
    PackageIdentity,
    directory_bytes,
    run_ffmpeg_copy_package,
    validate_track_timing,
    validate_package_files,
)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_timing(ffprobe_bin, source):
    completed = subprocess.run(
        [
            ffprobe_bin, "-v", "error", "-show_entries",
            "stream=codec_type,start_time,duration", "-of", "json", str(source),
        ],
        check=True, capture_output=True, text=True, timeout=30,
    )
    streams = json.loads(completed.stdout).get("streams", [])
    validate_track_timing(streams)
    return streams


def local_http_first_byte_ms(directory, manifest):
    relative = Path(manifest).resolve().relative_to(Path(directory).resolve())
    handler = lambda *args, **kwargs: QuietHandler(
        *args, directory=str(directory), **kwargs
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        started = time.perf_counter()
        with urlopen(
            "http://127.0.0.1:{}/{}".format(
                server.server_port, relative.as_posix()
            ),
            timeout=5,
        ) as response:
            first_byte = response.read(1)
        if not first_byte:
            raise RuntimeError("manifest HTTP response was empty")
        return (time.perf_counter() - started) * 1000.0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="already encoded MP4/fMP4 input")
    parser.add_argument("--prepackaged-dir", required=True)
    parser.add_argument("--cache-dir", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--asset-version", required=True)
    parser.add_argument("--profile-version", required=True)
    parser.add_argument("--protocol", choices=("hls", "dash"), required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--segment-seconds", type=int, default=4)
    parser.add_argument("--source-retention-hours", type=float, default=720)
    parser.add_argument("--package-retention-hours", type=float, default=720)
    parser.add_argument("--jit-cache-retention-hours", type=float, default=24)
    parser.add_argument("--drm-profile", default="clear")
    parser.add_argument("--drm-key-version", default="none")
    parser.add_argument("--warm-requests", type=int, default=10)
    args = parser.parse_args()

    source = Path(args.input).resolve()
    prepackaged = Path(args.prepackaged_dir).resolve()
    if not source.is_file() or not prepackaged.is_dir():
        parser.error("--input must be a file and --prepackaged-dir must be a directory")
    if args.warm_requests < 1:
        parser.error("--warm-requests must be positive")

    source_hash = sha256_file(source)
    source_bytes = source.stat().st_size
    streams = probe_timing(args.ffprobe, source)
    identity = PackageIdentity(
        asset_id=args.asset_id,
        asset_version=args.asset_version,
        profile_version=args.profile_version,
        source_sha256=source_hash,
        protocol=args.protocol,
        drm_profile=args.drm_profile,
        drm_key_version=args.drm_key_version,
        segment_duration_seconds=args.segment_seconds,
    )
    cache_root = Path(args.cache_dir)
    if cache_root.exists() and any(cache_root.iterdir()):
        parser.error("--cache-dir must be new or empty to measure cold packaging")
    cache = AtomicPackageCache(cache_root)
    build_metrics = {}

    def build(staging):
        build_metrics.update(run_ffmpeg_copy_package(
            args.ffmpeg, source, staging, args.protocol,
            args.segment_seconds, drm_profile=args.drm_profile,
        ))
        return "master.m3u8" if args.protocol == "hls" else "manifest.mpd"

    jit_result = cache.get_or_create(identity, build, validate_package_files)
    warm_latencies_ms = []
    warm_hits = 0
    for _ in range(args.warm_requests):
        started = time.perf_counter()
        result = cache.get_or_create(identity, build, validate_package_files)
        warm_latencies_ms.append((time.perf_counter() - started) * 1000.0)
        warm_hits += int(result.cache_hit)

    prepackaged_manifest_name = "master.m3u8" if args.protocol == "hls" else "manifest.mpd"
    prepackaged_manifest = prepackaged / prepackaged_manifest_name
    if not prepackaged_manifest.is_file():
        raise RuntimeError("pre-packaged directory is missing {}".format(prepackaged_manifest_name))

    prepackaged_bytes = directory_bytes(prepackaged)
    jit_package_bytes = directory_bytes(jit_result.directory)
    source_byte_hours = source_bytes * args.source_retention_hours
    baseline_byte_hours = source_byte_hours + prepackaged_bytes * args.package_retention_hours
    jit_byte_hours = (
        source_byte_hours + jit_package_bytes * args.jit_cache_retention_hours
    )

    ffmpeg_version = subprocess.run([args.ffmpeg, "-version"], check=True, capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
    ffprobe_version = subprocess.run([args.ffprobe, "-version"], check=True, capture_output=True, text=True, timeout=10).stdout.splitlines()[0]
    report = {
        "schema_version": 1,
        "workload": {
            "asset_id": args.asset_id,
            "asset_version": args.asset_version,
            "profile_version": args.profile_version,
            "protocol": args.protocol,
            "source_sha256": source_hash,
            "source_bytes": source_bytes,
            "source_streams": streams,
            "segment_seconds": args.segment_seconds,
            "ffmpeg": ffmpeg_version,
            "ffprobe": ffprobe_version,
            "drm_profile": args.drm_profile,
            "drm_key_version": args.drm_key_version,
        },
        "measurements": {
            "prepackaged_bytes": prepackaged_bytes,
            "jit_package_bytes": jit_package_bytes,
            "baseline_storage_byte_hours": baseline_byte_hours,
            "jit_storage_byte_hours": jit_byte_hours,
            "storage_byte_hours_delta": jit_byte_hours - baseline_byte_hours,
            "cold_packaging_wall_ms": jit_result.build_seconds * 1000.0,
            "cold_packaging_cpu_seconds": build_metrics.get("process_cpu_seconds"),
            "warm_cache_lookup_p50_ms": sorted(warm_latencies_ms)[len(warm_latencies_ms) // 2],
            "warm_cache_hits": warm_hits,
            "warm_cache_requests": args.warm_requests,
            "warm_cache_hit_ratio": warm_hits / float(args.warm_requests),
            "prepackaged_local_http_first_byte_ms": local_http_first_byte_ms(
                prepackaged, prepackaged_manifest
            ),
            "jit_local_http_first_byte_ms": local_http_first_byte_ms(
                jit_result.directory, jit_result.manifest
            ),
        },
        "limitations": {
            "network_ttfb": "not measured; loopback HTTP first-byte is not CDN/network TTFB",
            "playback": "not tested by this harness; run the repository player/device matrix",
            "drm": "clear-only prototype; encrypted profiles are rejected and must use conventional packaging",
            "subtitles": "source start-time alignment is checked when timestamps exist; subtitle rendering/player behavior is not verified",
            "cost": "byte-hours are a retention scenario, not provider billing; CPU seconds are not priced",
            "cache": "local filesystem cache; CDN cache-hit behavior is not represented",
        },
    }
    output = Path(args.output_json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
