#!/usr/bin/env python3
"""Create small pre-encoded H.264/AAC CMAF-compatible benchmark inputs.

Encoding happens once in this offline fixture-preparation step. The packaging
benchmark consumes the resulting MP4 files and uses stream copy only.
"""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

FIXTURES = [
    {
        "id": "landscape-avc-aac-6s",
        "filename": "landscape-avc-aac-6s.mp4",
        "video": "testsrc2=size=640x360:rate=24:duration=6",
        "audio": "sine=frequency=997:sample_rate=48000:duration=6",
        "width": 640, "height": 360, "duration_seconds": 6.0,
    },
    {
        "id": "portrait-avc-aac-5s",
        "filename": "portrait-avc-aac-5s.mp4",
        "video": "testsrc2=size=360x640:rate=30:duration=5",
        "audio": "sine=frequency=440:sample_rate=48000:duration=5",
        "width": 360, "height": 640, "duration_seconds": 5.0,
    },
]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def run(command):
    subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise RuntimeError("FFmpeg and FFprobe are required")
    root = Path(args.output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for item in FIXTURES:
        target = root / item["filename"]
        run([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", item["video"],
            "-f", "lavfi", "-i", item["audio"],
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p", "-g", "48", "-keyint_min", "48",
            "-sc_threshold", "0", "-threads", "1",
            "-c:a", "aac", "-b:a", "96k",
            "-map_metadata", "-1", "-movflags", "+faststart",
            "-y", str(target),
        ])
        probe = subprocess.run([
            ffprobe, "-v", "error", "-show_entries",
            "format=duration:stream=codec_type,codec_name,width,height,start_time",
            "-of", "json", str(target),
        ], check=True, capture_output=True, text=True, timeout=30)
        media = json.loads(probe.stdout)
        streams = media.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        if not video or not audio or video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
            raise RuntimeError("fixture does not contain the expected H.264/AAC streams")
        if (int(video["width"]), int(video["height"])) != (item["width"], item["height"]):
            raise RuntimeError("fixture dimensions do not match the manifest")
        subtitle = root / (target.stem + ".vtt")
        subtitle.write_text(
            "WEBVTT\n\n"
            "cue-1\n00:00:00.500 --> 00:00:01.500\nSynthetic caption one\n\n"
            "cue-2\n00:00:02.500 --> 00:00:03.500\nSynthetic caption two\n",
            encoding="utf-8",
        )
        artifacts.append({
            **item, "size_bytes": target.stat().st_size,
            "sha256": digest(target), "subtitle_filename": subtitle.name,
            "subtitle_sha256": digest(subtitle),
            "video_start_time": float(video.get("start_time") or 0),
            "audio_start_time": float(audio.get("start_time") or 0),
            "duration_seconds": float(media.get("format", {}).get("duration") or 0),
        })
    manifest = {
        "schema_version": 1, "dataset_id": "synthetic-cmaf-v1",
        "source_policy": "Generated at test time from FFmpeg lavfi; no third-party media.",
        "encoding_is_offline_fixture_preparation_only": True,
        "artifacts": artifacts,
    }
    (root / "dataset-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("Generated {} pre-encoded CMAF-compatible fixtures in {}".format(len(artifacts), root))


if __name__ == "__main__":
    main()
