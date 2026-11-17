#!/usr/bin/env python3
"""Generate and FFprobe-validate the synthetic VOD regression corpus."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path


HERE = Path(__file__).resolve().parent
MANIFEST_PATH = HERE / "manifest.json"


def require_binary(name):
    binary = shutil.which(name)
    if not binary:
        raise RuntimeError("{} was not found on PATH".format(name))
    return binary


def run(command):
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(
            "Command failed with exit {}: {}\n{}".format(
                exc.returncode, " ".join(command), detail
            )
        ) from exc


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def probe_media(ffprobe_path, media_path):
    result = run([
        ffprobe_path,
        "-v", "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
        "-of", "json",
        str(media_path),
    ])
    return json.loads(result.stdout)


def validate_fixture(fixture, probe):
    streams = probe.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise RuntimeError(
            "{}: expected exactly one video stream, found {}".format(
                fixture["id"], len(videos)
            )
        )

    video = videos[0]
    actual_dimensions = (
        int(video.get("width") or 0),
        int(video.get("height") or 0),
    )
    expected_dimensions = (fixture["width"], fixture["height"])
    if actual_dimensions != expected_dimensions:
        raise RuntimeError(
            "{}: expected dimensions {}, got {}".format(
                fixture["id"], expected_dimensions, actual_dimensions
            )
        )

    actual_rate = Fraction(video.get("avg_frame_rate", "0/1"))
    expected_rate = Fraction(fixture["frame_rate"])
    if actual_rate != expected_rate:
        raise RuntimeError(
            "{}: expected frame rate {}, got {}".format(
                fixture["id"], expected_rate, actual_rate
            )
        )

    if video.get("codec_name") != fixture["video_codec"]:
        raise RuntimeError(
            "{}: expected video codec {}, got {}".format(
                fixture["id"], fixture["video_codec"], video.get("codec_name")
            )
        )

    expected_audio = fixture.get("audio_codec")
    if expected_audio:
        if len(audios) != 1 or audios[0].get("codec_name") != expected_audio:
            raise RuntimeError(
                "{}: expected one {} audio stream".format(
                    fixture["id"], expected_audio
                )
            )
    elif audios:
        raise RuntimeError(
            "{}: expected no audio stream".format(fixture["id"])
        )

    duration = float(probe.get("format", {}).get("duration") or 0.0)
    tolerance = fixture["duration_tolerance_seconds"]
    if abs(duration - fixture["duration_seconds"]) > tolerance:
        raise RuntimeError(
            "{}: duration {}s outside expected {}s ±{}s".format(
                fixture["id"], duration, fixture["duration_seconds"], tolerance
            )
        )

    return {
        "video_codec": video.get("codec_name"),
        "width": actual_dimensions[0],
        "height": actual_dimensions[1],
        "frame_rate": str(actual_rate),
        "duration_seconds": duration,
        "audio_codec": audios[0].get("codec_name") if audios else None,
    }


def generate_fixture(ffmpeg_path, fixture, output_path):
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel", "error",
        "-f", "lavfi",
        "-i", fixture["video_source"],
    ]

    if fixture.get("audio_source"):
        command.extend([
            "-f", "lavfi",
            "-i", fixture["audio_source"],
            "-map", "0:v:0",
            "-map", "1:a:0",
        ])
    else:
        command.append("-an")

    command.extend([
        "-c:v", fixture["video_codec"],
        "-q:v", "5",
        "-g", str(fixture["video_gop"]),
        "-pix_fmt", "yuv420p",
    ])

    if fixture.get("audio_codec"):
        command.extend(["-c:a", fixture["audio_codec"], "-b:a", "96k"])

    command.extend(["-movflags", "+faststart", "-y", str(output_path)])
    run(command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    ffmpeg_path = require_binary("ffmpeg")
    ffprobe_path = require_binary("ffprobe")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    artifacts = []
    for fixture in manifest["fixtures"]:
        output_path = output_dir / fixture["filename"]
        generate_fixture(ffmpeg_path, fixture, output_path)
        if not output_path.is_file() or output_path.stat().st_size <= 0:
            raise RuntimeError(
                "{}: FFmpeg did not create a non-empty output".format(
                    fixture["id"]
                )
            )
        media_probe = probe_media(ffprobe_path, output_path)
        metadata = validate_fixture(fixture, media_probe)
        artifacts.append({
            "id": fixture["id"],
            "filename": fixture["filename"],
            "size_bytes": output_path.stat().st_size,
            "sha256": sha256_file(output_path),
            "probe": metadata,
        })

    ffmpeg_version = run([ffmpeg_path, "-version"]).stdout.splitlines()[0]
    generated_manifest = {
        "dataset_id": manifest["dataset_id"],
        "schema_version": manifest["schema_version"],
        "ffmpeg_build": ffmpeg_version,
        "artifacts": artifacts,
    }
    result_path = output_dir / "dataset-manifest.json"
    result_path.write_text(
        json.dumps(generated_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("Generated and validated {} fixtures in {}".format(
        len(artifacts), output_dir
    ))
    print("Manifest: {}".format(result_path))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as exc:
        print("Synthetic corpus generation failed: {}".format(exc), file=sys.stderr)
        sys.exit(1)
