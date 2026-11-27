#!/usr/bin/env python3
"""Research-only JIT CMAF packaging benchmark. Never used by the request path.

The input is already encoded MP4. FFmpeg is invoked with -c copy; this tool
measures packaging/remuxing only and never performs video/audio re-encoding.
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import resource
import re
import shutil
import statistics
import subprocess
import tempfile
import time
from pathlib import Path
import xml.etree.ElementTree as ET

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


def parse_vtt_timestamp(value):
    match = re.fullmatch(r"(?:(\d{2}):)?(\d{2}):(\d{2})\.(\d{3})", value.strip())
    if not match:
        raise ValueError("invalid WebVTT timestamp")
    hours = int(match.group(1) or 0)
    minutes, seconds, millis = int(match.group(2)), int(match.group(3)), int(match.group(4))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def validate_webvtt_timeline(path, media_duration, tolerance_seconds=0.25):
    """Check subtitle cue timestamps against the canonical media timeline."""
    text = Path(path).read_text(encoding="utf-8")
    if not text.startswith("WEBVTT"):
        raise ValueError("subtitle sidecar is not WebVTT")
    cues = []
    for line in text.splitlines():
        if "-->" not in line:
            continue
        left, right = [part.strip().split()[0] for part in line.split("-->", 1)]
        start, end = parse_vtt_timestamp(left), parse_vtt_timestamp(right)
        if start < 0 or end <= start or end > media_duration + tolerance_seconds:
            raise ValueError("subtitle cue falls outside the media timeline")
        cues.append({"start_seconds": start, "end_seconds": end})
    if not cues:
        raise ValueError("subtitle sidecar contains no cues")
    return {"cue_count": len(cues), "cues": cues, "media_duration_seconds": media_duration}


def probe_av_sync(manifest_path, ffprobe="ffprobe", tolerance_seconds=0.25):
    result = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_type,start_time", "-of", "json", str(manifest_path)],
        check=True, capture_output=True, text=True, timeout=30,
    )
    streams = json.loads(result.stdout).get("streams", [])
    starts = {
        item.get("codec_type"): float(item.get("start_time") or 0.0)
        for item in streams if item.get("codec_type") in ("video", "audio")
    }
    if "video" in starts and "audio" in starts:
        delta = abs(starts["video"] - starts["audio"])
        if delta > tolerance_seconds:
            raise RuntimeError("packaged audio/video start times exceed sync tolerance")
        return {"video_start_seconds": starts["video"],
                "audio_start_seconds": starts["audio"],
                "absolute_start_delta_seconds": delta,
                "tolerance_seconds": tolerance_seconds, "passed": True}
    return {"passed": None, "reason": "audio or video stream absent"}


def _expand_dash_template(template, representation_id, number, media_time):
    value = template.replace("$RepresentationID$", representation_id)
    value = value.replace("$Time$", str(media_time))
    value = re.sub(
        r"\$Number%0(\d+)d\$",
        lambda match: str(number).zfill(int(match.group(1))),
        value,
    )
    value = value.replace("$Number$", str(number))
    return value


def build_shared_cmaf(source, output_dir, ffmpeg="ffmpeg", segment_seconds=2):
    """Pre-package one CMAF media-object set and point both HLS and DASH at it."""
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    mpd = output_dir / "manifest.mpd"
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
        "-f", "dash", "-seg_duration", str(segment_seconds),
        "-use_timeline", "1", "-use_template", "1",
        "-adaptation_sets", "id=0,streams=v id=1,streams=a",
        "-init_seg_name", "init-$RepresentationID$.$ext$",
        "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.$ext$",
        "-y", str(mpd),
    ]
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    if not mpd.is_file() or mpd.stat().st_size == 0:
        raise RuntimeError("DASH muxer did not create a static CMAF MPD")

    tree = ET.parse(str(mpd))
    root = tree.getroot()
    namespace = "{urn:mpeg:dash:schema:mpd:2011}"
    tracks = []
    for adaptation in root.findall(".//" + namespace + "AdaptationSet"):
        representation = adaptation.find(namespace + "Representation")
        if representation is None:
            continue
        template = representation.find(namespace + "SegmentTemplate")
        if template is None:
            template = adaptation.find(namespace + "SegmentTemplate")
        if template is None:
            raise RuntimeError("CMAF MPD is missing a SegmentTemplate")
        timeline = template.find(namespace + "SegmentTimeline")
        if timeline is None:
            raise RuntimeError("CMAF MPD is missing a static SegmentTimeline")
        rep_id = representation.get("id")
        timescale = int(template.get("timescale", "1"))
        start_number = int(template.get("startNumber", "1"))
        initialization = _expand_dash_template(
            template.get("initialization", ""), rep_id, start_number, 0
        )
        media_template = template.get("media", "")
        segments = []
        number = start_number
        for item in timeline.findall(namespace + "S"):
            duration_units = int(item.get("d", "0"))
            repeat = int(item.get("r", "0"))
            if duration_units <= 0 or repeat < 0:
                raise RuntimeError("Unsupported CMAF segment timeline")
            media_time = int(item.get("t", "0"))
            for _ in range(repeat + 1):
                segments.append({
                    "path": _expand_dash_template(
                        media_template, rep_id, number, media_time
                    ),
                    "duration_seconds": duration_units / float(timescale),
                })
                number += 1
                media_time += duration_units
        if not segments:
            raise RuntimeError("CMAF MPD contains no media segments")
        mime_type = representation.get("mimeType", adaptation.get("mimeType", ""))
        content_type = adaptation.get("contentType") or (
            "audio" if "audio" in mime_type else "video"
        )
        tracks.append({
            "content_type": content_type,
            "representation_id": rep_id,
            "initialization": initialization,
            "segments": segments,
            "bandwidth": int(representation.get("bandwidth", "0")),
            "codecs": representation.get("codecs", ""),
            "width": representation.get("width"),
            "height": representation.get("height"),
        })

    video = next((track for track in tracks if track["content_type"] == "video"), None)
    audio = next((track for track in tracks if track["content_type"] == "audio"), None)
    if video is None:
        raise RuntimeError("CMAF MPD has no video adaptation set")
    audio_video_duration_delta = None
    if audio:
        video_duration = sum(item["duration_seconds"] for item in video["segments"])
        audio_duration = sum(item["duration_seconds"] for item in audio["segments"])
        audio_video_duration_delta = abs(video_duration - audio_duration)
        if audio_video_duration_delta > 0.25:
            raise RuntimeError("CMAF audio/video segment timelines are not aligned")

    def write_media_playlist(track, name):
        durations = [item["duration_seconds"] for item in track["segments"]]
        lines = [
            "#EXTM3U", "#EXT-X-VERSION:7",
            "#EXT-X-TARGETDURATION:{}".format(int(math.ceil(max(durations)))),
            "#EXT-X-MEDIA-SEQUENCE:1", "#EXT-X-PLAYLIST-TYPE:VOD",
            '#EXT-X-MAP:URI="{}"'.format(track["initialization"]),
        ]
        for segment in track["segments"]:
            lines.extend([
                "#EXTINF:{:.6f},".format(segment["duration_seconds"]),
                segment["path"],
            ])
        lines.append("#EXT-X-ENDLIST")
        (output_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    write_media_playlist(video, "video.m3u8")
    master = ["#EXTM3U", "#EXT-X-VERSION:7"]
    if audio:
        write_media_playlist(audio, "audio.m3u8")
        master.append(
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="audio",'
            'DEFAULT=YES,AUTOSELECT=YES,URI="audio.m3u8"'
        )
    codecs = [video["codecs"]]
    if audio and audio["codecs"]:
        codecs.append(audio["codecs"])
    bandwidth = video["bandwidth"] + (audio["bandwidth"] if audio else 0)
    attributes = ['BANDWIDTH={}'.format(max(1, bandwidth)),
                  'CODECS="{}"'.format(",".join(item for item in codecs if item))]
    if video.get("width") and video.get("height"):
        attributes.append('RESOLUTION={}x{}'.format(video["width"], video["height"]))
    if audio:
        attributes.append('AUDIO="audio"')
    master.extend(["#EXT-X-STREAM-INF:" + ",".join(attributes), "video.m3u8"])
    (output_dir / "master.m3u8").write_text("\n".join(master) + "\n", encoding="utf-8")

    hls_validation = validate_streaming_package(
        str(output_dir), str(output_dir / "master.m3u8"), "hls"
    )
    dash_validation = validate_streaming_package(
        str(output_dir), str(mpd), "dash"
    )
    inventory = tree_inventory(output_dir)
    media_files = [
        item for item in inventory["files"]
        if item["path"].endswith((".m4s", ".mp4"))
    ]
    return {
        "total_bytes": inventory["total_bytes"],
        "file_count": inventory["file_count"],
        "manifest_sha256": sha256_file(mpd),
        "hls_manifest_sha256": sha256_file(output_dir / "master.m3u8"),
        "hls_validation": hls_validation,
        "dash_validation": dash_validation,
        "shared_media_files": media_files,
        "shared_media_bytes": sum(item["size_bytes"] for item in media_files),
        "audio_video_sync": probe_av_sync(mpd),
        "audio_video_segment_duration_delta_seconds": audio_video_duration_delta,
        "inventory": inventory,
    }


def build_package(source, output_dir, protocol, ffmpeg="ffmpeg", segment_seconds=2):
    """Package existing encoded streams without codec conversion."""
    source = Path(source).resolve()
    output_dir = Path(output_dir).resolve()
    if not source.is_file() or source.stat().st_size == 0:
        raise ValueError("source must be an existing non-empty encoded asset")
    if protocol not in MANIFESTS:
        raise ValueError("unsupported protocol")
    output_dir.mkdir(parents=True, exist_ok=True)
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
    av_sync = probe_av_sync(manifest)
    return {
        "manifest": manifest.name,
        "manifest_sha256": sha256_file(manifest),
        "validation": validation,
        "audio_video_sync": av_sync,
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

    @staticmethod
    def _validate_key(key):
        if not isinstance(key, str) or len(key) != 64 or any(
                character not in "0123456789abcdef" for character in key):
            raise ValueError("cache key must be a lowercase SHA-256 digest")

    def get_or_build(self, key, builder):
        self._validate_key(key)
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
                if not any((target / name).is_file() and (target / name).stat().st_size
                           for name in MANIFESTS.values()):
                    raise RuntimeError("completed cache entry has no non-empty root manifest")
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
        self._validate_key(key)
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
                      cache_ttl_hours, hls_requests, dash_requests,
                      subtitle_path=None, media_duration=None):
    source = Path(source).resolve()
    source_hash = sha256_file(source)
    build = ffmpeg_version(ffmpeg)
    subtitle_validation = (
        validate_webvtt_timeline(subtitle_path, media_duration)
        if subtitle_path and media_duration else None
    )
    shared_dir = output_root / (source.stem + "-prepackaged-cmaf")
    if shared_dir.exists():
        shutil.rmtree(shared_dir)
    shared_cpu_started = cpu_seconds()
    shared_wall_started = time.monotonic()
    shared_cmaf = build_shared_cmaf(source, shared_dir, ffmpeg)
    shared_cmaf["packaging_ms"] = (time.monotonic() - shared_wall_started) * 1000
    shared_cmaf["cpu_seconds"] = max(0.0, cpu_seconds() - shared_cpu_started)

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
        cache_bytes = None
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
            cache_bytes = tree_inventory(path)["total_bytes"]
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
                "bytes": cache_bytes,
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
                "jit_cache_byte_hours": cache_bytes * resident_hours,
                "modeled_cache_misses": max(0, requests - modeled_hits),
            },
            "structural_validation": pre["validation"],
            "audio_video_sync": pre["audio_video_sync"],
            "subtitle_timeline": subtitle_validation,
        }
    separate_prepackaged_bytes = sum(
        protocols[name]["prepackaged"]["bytes"] for name in ("hls", "dash")
    )
    jit_cache_byte_hours = sum(
        protocols[name]["workload_model"]["jit_cache_byte_hours"]
        for name in ("hls", "dash")
    )
    modeled_cold_cpu_seconds = sum(
        protocols[name]["jit"]["cpu_seconds_p50"]
        * protocols[name]["workload_model"]["modeled_cache_misses"]
        for name in ("hls", "dash")
    )
    return {
        "fixture": source.name, "source_sha256": source_hash,
        "source_bytes": source.stat().st_size, "ffmpeg_build": build,
        "prepackaged_cmaf": {
            "bytes": shared_cmaf["total_bytes"],
            "shared_media_bytes": shared_cmaf["shared_media_bytes"],
            "files": shared_cmaf["file_count"],
            "packaging_ms": shared_cmaf["packaging_ms"],
            "cpu_seconds": shared_cmaf["cpu_seconds"],
            "hls_manifest_sha256": shared_cmaf["hls_manifest_sha256"],
            "dash_manifest_sha256": shared_cmaf["manifest_sha256"],
            "audio_video_sync": shared_cmaf["audio_video_sync"],
            "audio_video_segment_duration_delta_seconds":
                shared_cmaf["audio_video_segment_duration_delta_seconds"],
            "hls_validation": shared_cmaf["hls_validation"],
            "dash_validation": shared_cmaf["dash_validation"],
            "shared_media_files": shared_cmaf["shared_media_files"],
        },
        "storage_comparison": {
            "workload_hours": workload_hours,
            "conventional_separate_hls_dash_bytes": separate_prepackaged_bytes,
            "prepackaged_cmaf_byte_hours": shared_cmaf["total_bytes"] * workload_hours,
            "jit_cache_byte_hours": jit_cache_byte_hours,
            "modeled_byte_hour_difference_vs_cmaf":
                shared_cmaf["total_bytes"] * workload_hours - jit_cache_byte_hours,
            "modeled_jit_cold_cpu_seconds": modeled_cold_cpu_seconds,
            "note": "byte-hours and cold CPU are workload-model estimates; source bytes are common and excluded",
        },
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
        subtitle_path = (
            Path(input_dir) / artifact["subtitle_filename"]
            if artifact.get("subtitle_filename") else None
        )
        if subtitle_path and sha256_file(subtitle_path) != artifact.get("subtitle_sha256"):
            raise RuntimeError("subtitle fixture checksum mismatch")
        results.append(benchmark_fixture(
            source, output, ffmpeg, repetitions, workload_hours, cache_ttl_hours,
            hls_requests, dash_requests, subtitle_path,
            float(artifact.get("duration_seconds") or 0) or None,
        ))
    report = {
        "schema_version": SCHEMA_VERSION,
        "experiment": "issue-81-jit-cmaf-packaging",
        "packager_version": PACKAGER_VERSION,
        "request_path_reencoding": False,
        "methodology": {
            "input": "already-encoded synthetic H.264/AAC MP4 fixtures; all CMAF, HLS and DASH packaging commands use -c copy",
            "repetitions": repetitions,
            "workload_hours": workload_hours,
            "cache_ttl_hours": cache_ttl_hours,
            "hls_requests_per_asset": hls_requests,
            "dash_requests_per_asset": dash_requests,
            "byte_hours": "pre-packaged CMAF media objects retained for the full horizon versus protocol-specific JIT cache objects retained over modeled request/TTL residency; canonical source bytes are common and excluded",
            "ttfb": "local filesystem first-byte measurement, not HTTP/CDN/player TTFB",
        },
        "limitations": {
            "representative_storage_savings": "not established by synthetic fixtures",
            "drm": "not tested; no encryption, key rotation, license or entitlement integration",
            "player_compatibility": "not tested; structural manifest/artifact validation only",
            "audio_subtitle_sync": "packaged audio/video start-time alignment is checked with FFprobe and synthetic WebVTT cue times are checked against media duration; this is not a real player subtitle-rendering test",
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
