#!/usr/bin/env python3
"""Generate HLS and DASH packages from the synthetic VOD corpus and validate every reference."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from video_streaming.ffmpeg.package_validation import validate_streaming_package


def run(command, timeout=120):
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError("Command failed ({}): {}\n{}".format(
            exc.returncode, " ".join(command), detail
        )) from exc


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_dimensions(ffprobe, source):
    result = run([
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height", "-of", "json", str(source),
    ])
    streams = json.loads(result.stdout).get("streams", [])
    if len(streams) != 1:
        raise RuntimeError("Expected one video stream in {}".format(source.name))
    width, height = int(streams[0]["width"]), int(streams[0]["height"])
    if width < 4 or height < 4 or width % 2 or height % 2:
        raise RuntimeError("Unsupported video dimensions in {}".format(source.name))
    return width, height


def rendition_sizes(width, height):
    return (
        (width, height),
        (max(2, width // 2) // 2 * 2, max(2, height // 2) // 2 * 2),
    )


def create_hls(ffmpeg, source, package_dir, sizes):
    package_dir.mkdir(parents=True)
    variants = []
    for index, (width, height) in enumerate(sizes):
        variant_dir = package_dir / "v{}".format(index)
        variant_dir.mkdir()
        playlist = variant_dir / "index.m3u8"
        segment_pattern = variant_dir / "segment_%03d.ts"
        run([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-map", "0:v:0", "-an",
            "-vf", "scale={}:{}".format(width, height),
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-hls_time", "1", "-hls_playlist_type", "vod",
            "-hls_segment_filename", str(segment_pattern),
            "-y", str(playlist),
        ])
        variants.append((width, height, playlist.relative_to(package_dir).as_posix()))

    master = package_dir / "master.m3u8"
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for index, (width, height, reference) in enumerate(variants):
        bandwidth = 500000 if index == 0 else 250000
        lines.extend([
            "#EXT-X-STREAM-INF:BANDWIDTH={},RESOLUTION={}x{}".format(
                bandwidth, width, height
            ),
            reference,
        ])
    master.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return validate_streaming_package(str(package_dir), str(master), "hls"), master


def create_dash(ffmpeg, source, package_dir, sizes):
    package_dir.mkdir(parents=True)
    manifest = package_dir / "manifest.mpd"
    run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:v:0",
        "-filter:v:0", "scale={}:{}".format(*sizes[0]),
        "-filter:v:1", "scale={}:{}".format(*sizes[1]),
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-b:v:0", "500k", "-b:v:1", "250k",
        "-f", "dash", "-seg_duration", "1",
        "-use_timeline", "1", "-use_template", "1",
        "-init_seg_name", "init_$RepresentationID$.$ext$",
        "-media_seg_name", "chunk_$RepresentationID$_$Number%05d$.$ext$",
        "-adaptation_sets", "id=0,streams=v",
        "-y", str(manifest),
    ])
    return validate_streaming_package(str(package_dir), str(manifest), "dash"), manifest


def package_inventory(package_dir):
    files = []
    total_bytes = 0
    for path in sorted(item for item in package_dir.rglob("*") if item.is_file()):
        relative = path.relative_to(package_dir).as_posix()
        size = path.stat().st_size
        files.append({"path": relative, "size_bytes": size, "sha256": sha256_file(path)})
        total_bytes += size
    return {"file_count": len(files), "total_bytes": total_bytes, "files": files}


def run_harness(input_dir, output_dir):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and FFprobe are required")
    input_dir, output_dir = Path(input_dir).resolve(), Path(output_dir).resolve()
    dataset_path = input_dir / "dataset-manifest.json"
    if not dataset_path.is_file():
        raise RuntimeError("Missing generated dataset-manifest.json")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for artifact in dataset.get("artifacts", []):
        source = input_dir / artifact["filename"]
        if not source.is_file() or sha256_file(source) != artifact["sha256"]:
            raise RuntimeError("Input fixture missing or hash mismatch: {}".format(source.name))
        sizes = rendition_sizes(*probe_dimensions(ffprobe, source))
        fixture_dir = output_dir / artifact["id"]
        hls_result, hls_manifest = create_hls(ffmpeg, source, fixture_dir / "hls", sizes)
        dash_result, dash_manifest = create_dash(ffmpeg, source, fixture_dir / "dash", sizes)
        results.append({
            "fixture_id": artifact["id"],
            "source_sha256": artifact["sha256"],
            "rendition_sizes": [{"width": w, "height": h} for w, h in sizes],
            "hls": {
                **hls_result,
                "manifest_sha256": sha256_file(hls_manifest),
                **package_inventory(hls_manifest.parent),
            },
            "dash": {
                **dash_result,
                "manifest_sha256": sha256_file(dash_manifest),
                **package_inventory(dash_manifest.parent),
            },
        })
    if len(results) != len(dataset.get("artifacts", [])) or not results:
        raise RuntimeError("No synthetic VOD fixtures were validated")
    report = {
        "schema_version": 1,
        "dataset_id": dataset.get("dataset_id"),
        "ffmpeg_version": run([ffmpeg, "-version"]).stdout.splitlines()[0],
        "fixtures": results,
    }
    report_path = output_dir / "package-validation-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    report = run_harness(args.input_dir, args.output_dir)
    print(json.dumps({
        "fixtures": len(report["fixtures"]),
        "formats": ["hls", "dash"],
        "report": str(Path(args.output_dir).resolve() / "package-validation-report.json"),
    }, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        print("Package harness failed: {}".format(exc), file=sys.stderr)
        sys.exit(1)
