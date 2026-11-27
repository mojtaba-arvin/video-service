"""Experimental packaging-only CMAF cache primitives.

This module is deliberately not wired into the production request path. It
packages already-encoded inputs with stream copy only; it never invokes an
encoder or silently falls back to re-encoding.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import threading
import time
from typing import Callable, Optional, Union


@dataclass(frozen=True)
class PackageIdentity:
    """All inputs that can change the bytes or semantics of a package."""

    asset_id: str
    asset_version: str
    profile_version: str
    source_sha256: str
    protocol: str
    packaging_version: str = "jit-cmaf-prototype-v1"
    drm_profile: str = "clear"

    def cache_key(self) -> str:
        if self.protocol not in ("hls", "dash"):
            raise ValueError("protocol must be hls or dash")
        values = {
            "asset_id": self.asset_id,
            "asset_version": self.asset_version,
            "profile_version": self.profile_version,
            "source_sha256": self.source_sha256,
            "protocol": self.protocol,
            "packaging_version": self.packaging_version,
            "drm_profile": self.drm_profile,
        }
        if any(not isinstance(value, str) or not value for value in values.values()):
            raise ValueError("package identity fields must be non-empty strings")
        canonical = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PackageResult:
    directory: Path
    manifest: Path
    cache_hit: bool
    build_seconds: float


class AtomicPackageCache:
    """Publish immutable package directories only after complete validation.

    A per-key advisory file lock coordinates threads and processes on the
    same filesystem. The final directory appears in one rename, so readers
    cannot observe a partially populated package.
    """

    def __init__(self, root: Union[str, Path], lock_timeout_seconds: float = 120.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_timeout_seconds = lock_timeout_seconds

    def _paths(self, key: str):
        return self.root / key, self.root / (key + ".lock")

    def _locked(self, lock_path: Path):
        class Lock:
            def __enter__(inner):
                inner.handle = lock_path.open("a+b")
                deadline = time.monotonic() + self.lock_timeout_seconds
                while True:
                    try:
                        fcntl.flock(inner.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return inner
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            inner.handle.close()
                            raise TimeoutError("timed out waiting for package cache lock")
                        time.sleep(0.02)

            def __exit__(inner, *_):
                fcntl.flock(inner.handle.fileno(), fcntl.LOCK_UN)
                inner.handle.close()
        return Lock()

    @staticmethod
    def _manifest_path(directory: Path, relative_manifest: str) -> Path:
        candidate = (directory / relative_manifest).resolve()
        if directory.resolve() not in candidate.parents:
            raise ValueError("manifest must remain inside the package directory")
        return candidate

    def get_or_create(
        self,
        identity: PackageIdentity,
        build: Callable[[Path], str],
        validate: Optional[Callable[[Path, Path], None]] = None,
    ) -> PackageResult:
        key = identity.cache_key()
        final_dir, lock_path = self._paths(key)
        with self._locked(lock_path):
            existing_manifest = final_dir / ("master.m3u8" if identity.protocol == "hls" else "manifest.mpd")
            if existing_manifest.is_file() and existing_manifest.stat().st_size:
                try:
                    if validate:
                        validate(final_dir, existing_manifest)
                    return PackageResult(final_dir, existing_manifest, True, 0.0)
                except Exception:
                    # An invalid entry is never served; rebuild it under lock.
                    shutil.rmtree(final_dir, ignore_errors=True)

            started = time.perf_counter()
            staging = Path(tempfile.mkdtemp(prefix=key + ".tmp-", dir=str(self.root)))
            try:
                relative_manifest = build(staging)
                manifest = self._manifest_path(staging, relative_manifest)
                if not manifest.is_file() or manifest.stat().st_size == 0:
                    raise RuntimeError("packager did not produce a non-empty root manifest")
                if validate:
                    validate(staging, manifest)
                # The cache key is immutable. A stale/invalid entry is removed
                # only while holding its key lock; publication is one rename.
                if final_dir.exists():
                    shutil.rmtree(final_dir)
                os.replace(str(staging), str(final_dir))
                published_manifest = self._manifest_path(final_dir, relative_manifest)
                return PackageResult(
                    final_dir, published_manifest, False, time.perf_counter() - started
                )
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise

    def invalidate(self, identity: PackageIdentity) -> bool:
        final_dir, lock_path = self._paths(identity.cache_key())
        with self._locked(lock_path):
            if not final_dir.exists():
                return False
            shutil.rmtree(final_dir)
            return True


def ffmpeg_copy_command(
    ffmpeg_bin: str,
    input_path: Union[str, Path],
    output_dir: Union[str, Path],
    protocol: str,
    segment_seconds: int = 4,
    drm_profile: str = "clear",
) -> list[str]:
    """Build a packaging-only command. -c copy is a hard no-reencode boundary."""
    if protocol not in ("hls", "dash"):
        raise ValueError("protocol must be hls or dash")
    if drm_profile != "clear":
        raise ValueError("DRM packaging is not implemented by this prototype")
    if segment_seconds < 1:
        raise ValueError("segment_seconds must be positive")
    source = str(Path(input_path).resolve())
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    common = [ffmpeg_bin, "-hide_banner", "-nostdin", "-y", "-i", source, "-map", "0", "-c", "copy"]
    if protocol == "hls":
        return common + [
            "-f", "hls",
            "-hls_time", str(segment_seconds),
            "-hls_playlist_type", "vod",
            "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(output / "segment_%05d.m4s"),
            "-hls_flags", "independent_segments",
            str(output / "master.m3u8"),
        ]
    return common + [
        "-f", "dash",
        "-seg_duration", str(segment_seconds),
        "-use_template", "1",
        "-use_timeline", "1",
        "-init_seg_name", "init-$RepresentationID$.m4s",
        "-media_seg_name", "chunk-$RepresentationID$-$Number%05d$.m4s",
        str(output / "manifest.mpd"),
    ]


def run_ffmpeg_copy_package(
    ffmpeg_bin: str,
    input_path: Union[str, Path],
    output_dir: Union[str, Path],
    protocol: str,
    segment_seconds: int = 4,
    timeout_seconds: int = 180,
    drm_profile: str = "clear",
) -> dict:
    command = ffmpeg_copy_command(
        ffmpeg_bin, input_path, output_dir, protocol, segment_seconds, drm_profile
    )
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True,
        timeout=timeout_seconds,
    )
    elapsed = time.perf_counter() - started
    if completed.returncode:
        # Keep diagnostics bounded; never expose command arguments in logs.
        raise RuntimeError(
            "packaging-only FFmpeg failed (exit {}): {}".format(
                completed.returncode, (completed.stderr or "")[-2000:]
            )
        )
    return {
        "wall_seconds": elapsed,
        "process_cpu_seconds": max(0.0, (lambda after: after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime)(resource.getrusage(resource.RUSAGE_CHILDREN))),
        "command": command,
    }


def package_or_fallback(jit_package, conventional_package):
    """Use the conventional pre-packaged result if JIT packaging fails."""
    try:
        return jit_package()
    except Exception:
        return conventional_package()


def directory_bytes(directory: Union[str, Path]) -> int:
    root = Path(directory)
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())



def validate_track_timing(streams, tolerance_seconds=0.25):
    """Reject audio/video/subtitle tracks whose declared starts drift apart."""
    timed = []
    for stream in streams:
        if stream.get("codec_type") not in ("video", "audio", "subtitle"):
            continue
        value = stream.get("start_time")
        if value is None:
            raise ValueError("timed media stream is missing start_time")
        timed.append(float(value))
    if len(timed) < 2:
        return True
    if max(timed) - min(timed) > tolerance_seconds:
        raise ValueError("audio/video/subtitle start times are not synchronized")
    return True



def validate_package_files(directory, manifest):
    """Validate local HLS/DASH references before an immutable cache publish."""
    root = Path(directory).resolve()

    def local_file(reference):
        if not reference or "://" in reference or reference.startswith("/"):
            raise ValueError("package contains a non-local media reference")
        candidate = (root / reference.split("?", 1)[0]).resolve()
        if root not in candidate.parents:
            raise ValueError("package reference escapes its root")
        if not candidate.is_file() or candidate.stat().st_size == 0:
            raise ValueError("package reference is missing or empty: {}".format(reference))
        return candidate

    if Path(manifest).suffix.lower() == ".m3u8":
        visited = set()

        def validate_hls(playlist):
            playlist = Path(playlist).resolve()
            if playlist in visited:
                return
            visited.add(playlist)
            for raw in playlist.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line.startswith("#EXT-X-MAP:"):
                    match = re.search(r'URI="([^"]+)"', line)
                    if not match:
                        raise ValueError("HLS map tag has no URI")
                    local_file(match.group(1))
                elif line and not line.startswith("#"):
                    referenced = local_file(line)
                    if referenced.suffix.lower() == ".m3u8":
                        validate_hls(referenced)
        validate_hls(manifest)
        return True

    if Path(manifest).suffix.lower() != ".mpd":
        raise ValueError("unsupported package manifest")
    tree = ET.parse(str(manifest))
    mpd = tree.getroot()
    parent = {child: node for node in mpd.iter() for child in node}
    templates = [node for node in mpd.iter()
                 if node.tag.rsplit("}", 1)[-1] == "SegmentTemplate"]
    for template in templates:
        representation = parent.get(template)
        while representation is not None and representation.tag.rsplit("}", 1)[-1] != "Representation":
            representation = parent.get(representation)
        representations = [representation] if representation is not None else [
            node for node in mpd.iter()
            if node.tag.rsplit("}", 1)[-1] == "Representation"
        ]
        if not representations:
            representations = [None]
        for item in representations:
            rep_id = item.attrib.get("id", "") if item is not None else ""
            bandwidth = item.attrib.get("bandwidth", "") if item is not None else ""
            initialization = template.attrib.get("initialization")
            media = template.attrib.get("media")
            for value in (initialization, media):
                if not value:
                    continue
                value = value.replace("$RepresentationID$", rep_id)
                value = value.replace("$Bandwidth$", bandwidth)
                value = re.sub(r"\$Number(?:%0\d+d)?\$", "*", value)
                value = value.replace("$Time$", "*")
                if "*" in value:
                    matches = glob.glob(str(root / value))
                    if not matches or not all(Path(match).is_file() and Path(match).stat().st_size for match in matches):
                        raise ValueError("DASH template has no complete media objects")
                else:
                    local_file(value)
    for node in mpd.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag in ("Initialization", "SegmentURL"):
            reference = node.attrib.get("sourceURL") or node.attrib.get("media")
            if reference:
                local_file(reference)
    return True
