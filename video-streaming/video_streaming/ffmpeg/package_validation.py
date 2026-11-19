"""Validate local HLS and MPEG-DASH VOD packages before publication.

Publication invariant: a manifest is not uploaded until every artifact it
references is uploaded and readable. Child manifests precede parents; the
root manifest is the final publication point.

Status invariant: a playlist output cannot enter PROCESSING_FINISHED until its
local validation report is valid and the publication boundary has independently
verified every uploaded dependency and the root manifest.

Validation is inexpensive and structural (manifest/filesystem metadata and
optional ffprobe metadata); it never decodes or re-encodes media. Remote URLs,
path traversal, dynamic MPDs, and unresolved DASH templates fail closed.
"""

import json
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit

from video_streaming.execution.process_control import run_cancellable_process


class StreamingPackageValidationError(ValueError):
    """Raised when a generated streaming package is incomplete or invalid."""


@dataclass
class PackageValidationIssue:
    code: str
    location: str
    message: str


@dataclass
class PackageValidationReport:
    protocol: str
    manifest: str
    expected_renditions: List[str] = field(default_factory=list)
    actual_renditions: List[str] = field(default_factory=list)
    segments: int = 0
    errors: List[PackageValidationIssue] = field(default_factory=list)

    @property
    def valid(self):
        return not self.errors

    def as_dict(self):
        return {
            "protocol": self.protocol,
            "manifest": self.manifest,
            "expected_renditions": list(self.expected_renditions),
            "actual_renditions": list(self.actual_renditions),
            "segments": self.segments,
            "valid": self.valid,
            "errors": [
                {
                    "code": issue.code,
                    "location": issue.location,
                    "message": issue.message,
                }
                for issue in self.errors
            ],
        }

    def require_valid(self):
        if self.errors:
            first = self.errors[0]
            raise StreamingPackageValidationError(
                "{} at {}: {}".format(
                    first.code, first.location, first.message
                )
            )
        return self


_URI_ATTRIBUTE = re.compile(r'\bURI="([^"]+)"')
_TEMPLATE_TOKEN = re.compile(
    r'\$(RepresentationID|Bandwidth|Number|Time)(?:%0(\d+)d)?\$'
)


def _non_empty_file(root: Path, reference: str) -> Path:
    """Resolve a manifest reference inside root and require a non-empty file."""
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise StreamingPackageValidationError(
            "External or decorated media references are not supported: "
            + reference
        )
    if not reference or reference.startswith(("/", "\\")):
        raise StreamingPackageValidationError(
            "Media reference must be a non-empty relative path"
        )

    decoded = unquote(reference)
    if "\\" in decoded:
        raise StreamingPackageValidationError(
            "Backslashes are not valid package references: " + reference
        )

    unresolved = root / decoded
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        raise StreamingPackageValidationError(
            "Media reference escapes the package directory: " + reference
        )

    current = root
    for part in Path(decoded).parts:
        if part == "..":
            current = current.parent
        elif part != ".":
            current = current / part
        if current.is_symlink():
            raise StreamingPackageValidationError(
                "Symlink references are not allowed in a streaming package: "
                + reference
            )

    if not candidate.is_file() or candidate.stat().st_size <= 0:
        raise StreamingPackageValidationError(
            "Referenced media file is missing or empty: " + reference
        )
    return candidate


def _playlist_references(line: str):
    """Return URI attributes from an HLS tag."""
    return _URI_ATTRIBUTE.findall(line)


def _validate_hls_media_playlist(playlist: Path) -> int:
    try:
        lines = playlist.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StreamingPackageValidationError(
            "Cannot read HLS media playlist: " + str(playlist)
        ) from exc

    if not lines or lines[0].strip() != "#EXTM3U":
        raise StreamingPackageValidationError(
            "Invalid HLS media playlist header: " + str(playlist.name)
        )

    has_endlist = False
    has_target_duration = False
    pending_duration = False
    segment_count = 0

    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        if has_endlist:
            raise StreamingPackageValidationError(
                "HLS VOD playlist contains data after EXT-X-ENDLIST: "
                + playlist.name
            )

        if line == "#EXT-X-ENDLIST":
            has_endlist = True
            continue

        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                has_target_duration = int(line.split(":", 1)[1]) > 0
            except ValueError:
                has_target_duration = False
            continue

        if line.startswith("#EXTINF:"):
            if pending_duration:
                raise StreamingPackageValidationError(
                    "HLS playlist has multiple EXTINF tags without a segment URI: "
                    + playlist.name
                )
            try:
                duration = float(line.split(":", 1)[1].split(",", 1)[0])
            except ValueError:
                duration = 0
            if not math.isfinite(duration) or duration <= 0:
                raise StreamingPackageValidationError(
                    "HLS segment has a non-positive EXTINF duration: "
                    + playlist.name
                )
            pending_duration = True
            continue

        if line.startswith("#"):
            for reference in _playlist_references(line):
                _non_empty_file(playlist.parent, reference)
            continue

        if not pending_duration:
            raise StreamingPackageValidationError(
                "HLS media URI is not preceded by EXTINF: " + playlist.name
            )
        _non_empty_file(playlist.parent, line)
        segment_count += 1
        pending_duration = False

    if pending_duration:
        raise StreamingPackageValidationError(
            "HLS playlist ends after EXTINF without a segment URI: "
            + playlist.name
        )
    if not has_target_duration:
        raise StreamingPackageValidationError(
            "HLS VOD playlist has no valid TARGETDURATION: " + playlist.name
        )
    if not has_endlist:
        raise StreamingPackageValidationError(
            "HLS VOD playlist is not finalized (EXT-X-ENDLIST missing): "
            + playlist.name
        )
    if segment_count == 0:
        raise StreamingPackageValidationError(
            "HLS media playlist contains no media segments: " + playlist.name
        )
    return segment_count


def validate_hls_package(root: Path, manifest: Path) -> dict:
    """Validate the HLS master playlist, every rendition, and all references."""
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StreamingPackageValidationError(
            "Cannot read HLS master playlist: " + str(manifest)
        ) from exc

    if not lines or lines[0].strip() != "#EXTM3U":
        raise StreamingPackageValidationError("Invalid HLS master playlist")

    rendition_refs = []
    pending_stream_info = False
    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            if pending_stream_info:
                raise StreamingPackageValidationError(
                    "HLS master playlist has a STREAM-INF without a URI"
                )
            pending_stream_info = True
            continue
        if line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            rendition_refs.extend(_playlist_references(line))
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            rendition_refs.extend(_playlist_references(line))
            continue
        if line.startswith("#"):
            for reference in _playlist_references(line):
                _non_empty_file(root, reference)
            continue
        if pending_stream_info:
            rendition_refs.append(line)
            pending_stream_info = False

    if pending_stream_info:
        raise StreamingPackageValidationError(
            "HLS master playlist ends before a rendition URI"
        )
    if not rendition_refs:
        raise StreamingPackageValidationError(
            "HLS master playlist declares no renditions"
        )

    rendition_count = 0
    segment_count = 0
    rendition_names = []
    seen = set()
    for reference in rendition_refs:
        rendition = _non_empty_file(root, reference)
        if rendition.suffix.lower() != ".m3u8":
            raise StreamingPackageValidationError(
                "HLS rendition reference is not a playlist: " + reference
            )
        if rendition in seen:
            continue
        seen.add(rendition)
        segment_count += _validate_hls_media_playlist(rendition)
        rendition_names.append(rendition.relative_to(root).as_posix())
        rendition_count += 1

    if rendition_count == 0:
        raise StreamingPackageValidationError("HLS package has no valid renditions")

    return {
        "protocol": "hls",
        "manifest": manifest.name,
        "renditions": rendition_count,
        "rendition_names": sorted(rendition_names),
        "segments": segment_count,
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element, name):
    return [child for child in list(element) if _local_name(child.tag) == name]


def _first_child(element, name):
    matches = _children(element, name)
    return matches[0] if matches else None


def _duration_seconds(value: str) -> float:
    """Parse the ISO-8601 duration subset used by static DASH MPDs."""
    match = re.fullmatch(
        r"P(?:(\d+(?:\.\d+)?)D)?"
        r"(?:T(?:(\d+(?:\.\d+)?)H)?"
        r"(?:(\d+(?:\.\d+)?)M)?"
        r"(?:(\d+(?:\.\d+)?)S)?)?",
        value or "",
    )
    if not match:
        raise StreamingPackageValidationError(
            "Unsupported or missing DASH presentation duration"
        )
    days, hours, minutes, seconds = (
        float(part or 0) for part in match.groups()
    )
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _expand_dash_template(template: str, representation: ET.Element,
                          number: int = None, time_value: int = None) -> str:
    rep_id = representation.attrib.get("id", "")
    bandwidth = representation.attrib.get("bandwidth", "")

    def replace(match):
        token, width = match.group(1), match.group(2)
        values = {
            "RepresentationID": rep_id,
            "Bandwidth": bandwidth,
            "Number": number,
            "Time": time_value,
        }
        value = values[token]
        if value is None:
            raise StreamingPackageValidationError(
                "DASH template uses an unresolved $" + token + "$ token"
            )
        if width and token in ("Number", "Time"):
            return str(value).zfill(int(width))
        return str(value)

    # DASH uses $$ to escape a literal dollar sign.
    protected = template.replace("$$", "\x00")
    expanded = _TEMPLATE_TOKEN.sub(replace, protected)
    if "$" in expanded:
        raise StreamingPackageValidationError(
            "Unsupported DASH template expression: " + template
        )
    return expanded.replace("\x00", "$")


def _effective_template(elements):
    attributes = {}
    timeline = None
    for element in elements:
        if element is None:
            continue
        attributes.update(element.attrib)
        current_timeline = _first_child(element, "SegmentTimeline")
        if current_timeline is not None:
            timeline = current_timeline
    return attributes, timeline


def _dash_timeline_points(timeline, timescale, start_number):
    if timeline is None:
        raise StreamingPackageValidationError(
            "DASH SegmentTemplate must include SegmentTimeline"
        )

    points = []
    current_time = 0
    number = start_number
    for entry in _children(timeline, "S"):
        duration = int(entry.attrib.get("d", "0"))
        if duration <= 0:
            raise StreamingPackageValidationError(
                "DASH SegmentTimeline contains a non-positive duration"
            )
        if "t" in entry.attrib:
            current_time = int(entry.attrib["t"])
        repeat = int(entry.attrib.get("r", "0"))
        if repeat < 0:
            raise StreamingPackageValidationError(
                "Negative DASH timeline repeats are not supported safely"
            )
        for _ in range(repeat + 1):
            points.append((number, current_time))
            number += 1
            current_time += duration

    if not points or timescale <= 0:
        raise StreamingPackageValidationError(
            "DASH SegmentTimeline contains no usable segments"
        )
    return points


def _validate_dash_representation(root, representation, inherited,
                                  period_duration):
    elements = inherited + [representation]
    template_elements = [
        element for element in elements
        if _first_child(element, "SegmentTemplate") is not None
    ]
    if template_elements:
        templates = [
            _first_child(element, "SegmentTemplate")
            for element in elements
        ]
        attrs, timeline = _effective_template(templates)
        initialization = attrs.get("initialization")
        media = attrs.get("media")
        if not initialization or not media:
            raise StreamingPackageValidationError(
                "DASH SegmentTemplate requires initialization and media"
            )
        timescale = int(attrs.get("timescale", "1"))
        start_number = int(attrs.get("startNumber", "1"))
        points = _dash_timeline_points(timeline, timescale, start_number)
        _non_empty_file(
            root,
            _expand_dash_template(initialization, representation),
        )
        for number, time_value in points:
            _non_empty_file(
                root,
                _expand_dash_template(
                    media, representation, number=number,
                    time_value=time_value,
                ),
            )
        return len(points), representation.attrib.get("id", "")

    # SegmentList is also supported when every media URL is explicit.
    segment_lists = [
        _first_child(element, "SegmentList") for element in elements
    ]
    segment_lists = [item for item in segment_lists if item is not None]
    if segment_lists:
        initialization = None
        segment_urls = []
        for segment_list in segment_lists:
            init = _first_child(segment_list, "Initialization")
            if init is not None and init.attrib.get("sourceURL"):
                initialization = init.attrib["sourceURL"]
            segment_urls.extend(
                item.attrib.get("media")
                for item in _children(segment_list, "SegmentURL")
            )
        if initialization:
            _non_empty_file(root, initialization)
        segment_urls = [item for item in segment_urls if item]
        if not segment_urls:
            raise StreamingPackageValidationError(
                "DASH SegmentList contains no media URLs"
            )
        for reference in segment_urls:
            _non_empty_file(root, reference)
        return len(segment_urls), representation.attrib.get("id", "")

    raise StreamingPackageValidationError(
        "DASH representation has no supported SegmentTemplate or SegmentList"
    )


def validate_dash_package(root: Path, manifest: Path) -> dict:
    """Validate static MPD structure and every declared initialization/media file."""
    try:
        raw = manifest.read_bytes()
    except OSError as exc:
        raise StreamingPackageValidationError(
            "Cannot read DASH MPD: " + str(manifest)
        ) from exc
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise StreamingPackageValidationError(
            "DASH MPD must not contain a document type or entity declaration"
        )
    try:
        mpd = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise StreamingPackageValidationError("DASH MPD is not valid XML") from exc

    if _local_name(mpd.tag) != "MPD":
        raise StreamingPackageValidationError("DASH manifest root is not MPD")
    if _first_child(mpd, "BaseURL") is not None:
        raise StreamingPackageValidationError(
            "DASH BaseURL resolution is not supported; use package-relative templates"
        )
    if mpd.attrib.get("type", "static").lower() != "static":
        raise StreamingPackageValidationError(
            "Only static DASH VOD manifests can be published"
        )

    periods = _children(mpd, "Period")
    if not periods:
        raise StreamingPackageValidationError("DASH MPD contains no Period")

    total_representations = 0
    total_segments = 0
    representation_ids = []
    mpd_duration = mpd.attrib.get("mediaPresentationDuration")
    period_duration = (
        _duration_seconds(mpd_duration) if mpd_duration else None
    )

    for period in periods:
        if _first_child(period, "BaseURL") is not None:
            raise StreamingPackageValidationError(
                "DASH Period BaseURL resolution is not supported"
            )
        adaptation_sets = _children(period, "AdaptationSet")
        if not adaptation_sets:
            raise StreamingPackageValidationError(
                "DASH Period contains no AdaptationSet"
            )
        for adaptation in adaptation_sets:
            if _first_child(adaptation, "BaseURL") is not None:
                raise StreamingPackageValidationError(
                    "DASH AdaptationSet BaseURL resolution is not supported"
                )
            representations = _children(adaptation, "Representation")
            if not representations:
                raise StreamingPackageValidationError(
                    "DASH AdaptationSet contains no Representation"
                )
            inherited = [mpd, period, adaptation]
            for representation in representations:
                if _first_child(representation, "BaseURL") is not None:
                    raise StreamingPackageValidationError(
                        "DASH Representation BaseURL resolution is not supported"
                    )
                segment_count, representation_id = _validate_dash_representation(
                    root, representation, inherited, period_duration
                )
                total_segments += segment_count
                representation_ids.append(representation_id)
                total_representations += 1

    if total_representations == 0 or total_segments == 0:
        raise StreamingPackageValidationError(
            "DASH MPD contains no complete representations"
        )

    return {
        "protocol": "dash",
        "manifest": manifest.name,
        "renditions": total_representations,
        "rendition_ids": sorted(representation_ids),
        "segments": total_segments,
    }


def validate_streaming_package(directory: str, manifest_path: str,
                               protocol: str) -> dict:
    """Validate an HLS or DASH VOD package rooted at directory."""
    root = Path(directory).resolve()
    manifest_input = Path(manifest_path)
    if manifest_input.is_symlink():
        raise StreamingPackageValidationError(
            "Manifest must not be a symlink"
        )
    manifest = manifest_input.resolve()
    if not root.is_dir():
        raise StreamingPackageValidationError(
            "Streaming package directory does not exist"
        )
    try:
        manifest.relative_to(root)
    except ValueError as exc:
        raise StreamingPackageValidationError(
            "Manifest must be inside the package directory"
        ) from exc
    if manifest.is_symlink() or not manifest.is_file():
        raise StreamingPackageValidationError(
            "Manifest is missing or is not a regular file"
        )
    if manifest.stat().st_size <= 0:
        raise StreamingPackageValidationError("Manifest is empty")

    protocol = (protocol or "").lower()
    if protocol == "hls":
        if manifest.suffix.lower() != ".m3u8":
            raise StreamingPackageValidationError(
                "HLS manifest must use the .m3u8 extension"
            )
        return validate_hls_package(root, manifest)
    if protocol == "dash":
        if manifest.suffix.lower() != ".mpd":
            raise StreamingPackageValidationError(
                "DASH manifest must use the .mpd extension"
            )
        return validate_dash_package(root, manifest)
    raise StreamingPackageValidationError(
        "Unsupported streaming protocol: " + protocol
    )



def _validation_error_code(message: str) -> str:
    """Map validator failures to stable machine-readable categories."""
    lowered = message.lower()
    if any(token in lowered for token in (
            "escapes", "backslash", "external", "symlink", "baseurl")):
        return "unsafe_reference"
    if any(token in lowered for token in (
            "missing", "not found", "empty", "non-empty")):
        return "artifact_missing_or_empty"
    if "rendition" in lowered or "representation" in lowered:
        return "rendition_mismatch"
    if "extinf" in lowered or "playlist" in lowered:
        return "invalid_hls_playlist"
    if "dash" in lowered or "mpd" in lowered or "segmenttemplate" in lowered:
        return "invalid_dash_manifest"
    return "invalid_package"


def _validation_error_location(message: str, fallback: str) -> str:
    """Extract an artifact path from a validation message when present."""
    candidate = message.rsplit(": ", 1)[-1].strip()
    if candidate and (
            "/" in candidate or "\\" in candidate
            or candidate.startswith("..")
            or Path(candidate).suffix):
        return candidate
    return fallback


def validate_package(
        directory: str,
        package_type: str,
        expected_renditions: Optional[List[str]],
        manifest_path: str = None,
        ffprobe_bin_path: str = None,
        metadata_probe_timeout_seconds: int = 10,
        should_cancel=None) -> PackageValidationReport:
    """Return a structured reusable validation report for a VOD package.

    expected_renditions is a list of HLS rendition playlist paths relative
    to the package root, or DASH Representation IDs. An empty list means the
    caller expects no rendition; None disables the expectation check.
    This entry point performs structural validation only and does no decode.
    """
    protocol = (package_type or "").lower()
    manifest = manifest_path
    if manifest is None:
        extension = ".m3u8" if protocol == "hls" else ".mpd"
        candidates = sorted(Path(directory).glob("*" + extension))
        manifest = str(candidates[0]) if len(candidates) == 1 else ""

    report = PackageValidationReport(
        protocol=protocol,
        manifest=str(manifest or ""),
        expected_renditions=list(expected_renditions or []),
    )

    try:
        if not manifest:
            raise StreamingPackageValidationError(
                "Package root manifest could not be determined"
            )
        details = validate_streaming_package(
            directory=directory,
            manifest_path=manifest,
            protocol=protocol,
        )
        actual = (
            details.get("rendition_names", [])
            if protocol == "hls"
            else details.get("rendition_ids", [])
        )
        report.actual_renditions = list(actual)
        report.segments = int(details.get("segments", 0))
        if expected_renditions is not None:
            expected = sorted(str(item) for item in expected_renditions)
            actual_sorted = sorted(str(item) for item in actual)
            if expected != actual_sorted:
                report.errors.append(PackageValidationIssue(
                    code="expected_renditions_mismatch",
                    location=str(manifest),
                    message=(
                        "Expected renditions {} but found {}".format(
                            expected, actual_sorted
                        )
                    ),
                ))
        if ffprobe_bin_path and not report.errors:
            report.errors.extend(_probe_package_metadata(
                directory=directory,
                package_type=protocol,
                manifest_path=manifest,
                ffprobe_bin_path=ffprobe_bin_path,
                timeout_seconds=metadata_probe_timeout_seconds,
                should_cancel=should_cancel,
            ))
    except Exception as exc:
        message = str(exc)
        report.errors.append(PackageValidationIssue(
            code=_validation_error_code(message),
            location=_validation_error_location(message, str(manifest)),
            message=message,
        ))

    return report



def manifest_dependencies(
        directory: str,
        manifest_path: str,
        protocol: str) -> List[str]:
    """Return safe package-relative dependencies of one manifest.

    HLS dependencies are parsed directly from URI attributes and media URI
    lines. DASH has no nested manifests in this supported static profile, so
    the validated MPD depends on every non-MPD package object.
    """
    root = Path(directory).resolve()
    manifest = Path(manifest_path).resolve()
    try:
        manifest.relative_to(root)
    except ValueError as exc:
        raise StreamingPackageValidationError(
            "Manifest dependency path escapes package root"
        ) from exc

    protocol = (protocol or "").lower()
    dependencies = set()
    if protocol == "dash":
        for current, directories, filenames in os.walk(
                str(root), followlinks=False):
            for dirname in directories:
                if (Path(current) / dirname).is_symlink():
                    raise StreamingPackageValidationError(
                        "Symlink directory in DASH package"
                    )
            for filename in filenames:
                candidate = Path(current) / filename
                if candidate.is_symlink():
                    raise StreamingPackageValidationError(
                        "Symlink file in DASH package: " + filename
                    )
                if candidate.resolve() != manifest and candidate.suffix.lower() != ".mpd":
                    dependencies.add(candidate.resolve().relative_to(root).as_posix())
        return sorted(dependencies)

    if protocol != "hls":
        raise StreamingPackageValidationError(
            "Unsupported package protocol: " + protocol
        )

    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise StreamingPackageValidationError(
            "Cannot read HLS manifest dependencies: " + str(manifest)
        ) from exc

    for raw_line in lines[1:]:
        line = raw_line.strip()
        if not line:
            continue
        references = _playlist_references(line) if line.startswith("#") else [line]
        for reference in references:
            candidate = _non_empty_file(manifest.parent, reference)
            dependencies.add(candidate.relative_to(root).as_posix())

    return sorted(dependencies)


def ordered_package_files(
        directory: str,
        manifest_name: str,
        protocol: str) -> List[tuple]:
    """Topologically order a package so every manifest dependency is prior.

    The returned tuple items are (absolute path, package-relative path, size).
    Unreferenced local artifacts and orphan playlists fail closed rather than
    accidentally being uploaded from a previous retry.
    """
    root = Path(directory).resolve()
    if not root.is_dir():
        raise StreamingPackageValidationError(
            "Streaming package directory does not exist"
        )
    root_manifest = (root / manifest_name).resolve()
    try:
        root_manifest.relative_to(root)
    except ValueError as exc:
        raise StreamingPackageValidationError(
            "Root manifest escapes package directory"
        ) from exc
    if not root_manifest.is_file() or root_manifest.is_symlink():
        raise StreamingPackageValidationError(
            "Root manifest is missing or unsafe"
        )

    inventory = {}
    for current, directories, filenames in os.walk(
            str(root), followlinks=False):
        directories.sort()
        for dirname in directories:
            if (Path(current) / dirname).is_symlink():
                raise StreamingPackageValidationError(
                    "Symlink directories are not allowed in a package"
                )
        for filename in sorted(filenames):
            path = Path(current) / filename
            if path.is_symlink() or not path.is_file():
                raise StreamingPackageValidationError(
                    "Symlink or non-regular output file: " + filename
                )
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            if size <= 0:
                raise StreamingPackageValidationError(
                    "Package contains an empty output file: " + relative
                )
            inventory[relative] = (str(path), size)

    if protocol == "dash":
        if any(
                relative.endswith(".mpd") and relative != manifest_name
                for relative in inventory):
            raise StreamingPackageValidationError(
                "DASH package contains an unexpected secondary MPD"
            )
        referenced = set(manifest_dependencies(
            str(root), str(root_manifest), protocol
        ))
        actual = set(inventory) - {manifest_name}
        if actual != referenced:
            raise StreamingPackageValidationError(
                "DASH package contains unreferenced or missing artifacts"
            )
        ordered = sorted(actual) + [manifest_name]
    elif protocol == "hls":
        visiting = set()
        visited = set()
        postorder = []
        referenced_media = set()

        def visit(relative_manifest):
            if relative_manifest in visiting:
                raise StreamingPackageValidationError(
                    "HLS playlist dependency cycle: " + relative_manifest
                )
            if relative_manifest in visited:
                return
            if relative_manifest not in inventory:
                raise StreamingPackageValidationError(
                    "HLS child playlist is missing: " + relative_manifest
                )
            visiting.add(relative_manifest)
            absolute_manifest = root / relative_manifest
            dependencies = manifest_dependencies(
                str(root), str(absolute_manifest), "hls"
            )
            for dependency in dependencies:
                if dependency.lower().endswith(".m3u8"):
                    visit(dependency)
                else:
                    referenced_media.add(dependency)
            visiting.remove(relative_manifest)
            visited.add(relative_manifest)
            postorder.append(relative_manifest)

        root_relative = root_manifest.relative_to(root).as_posix()
        visit(root_relative)
        local_playlists = {
            relative for relative in inventory
            if relative.lower().endswith(".m3u8")
        }
        if local_playlists != visited:
            orphaned = sorted(local_playlists - visited)
            raise StreamingPackageValidationError(
                "HLS package contains unreferenced playlists: "
                + ", ".join(orphaned)
            )
        local_media = set(inventory) - local_playlists
        if local_media != referenced_media:
            orphaned = sorted(local_media - referenced_media)
            missing = sorted(referenced_media - local_media)
            raise StreamingPackageValidationError(
                "HLS media inventory mismatch; unreferenced={}, missing={}".format(
                    orphaned, missing
                )
            )
        ordered = sorted(local_media) + postorder
    else:
        raise StreamingPackageValidationError(
            "Unsupported package protocol: " + str(protocol)
        )

    if not ordered or ordered[-1] != manifest_name:
        raise StreamingPackageValidationError(
            "Root manifest must be the last publication artifact"
        )
    return [
        (inventory[relative][0], relative, inventory[relative][1])
        for relative in ordered
    ]


def reset_package_directory(directory: str, expected_parent: str):
    """Remove only the output-id directory owned by the current VOD job.

    The target must be a direct child of the caller-provided job output root;
    symlinks and paths outside that root are rejected.
    """
    target_input = Path(directory)
    parent_input = Path(expected_parent)
    if parent_input.is_symlink():
        raise StreamingPackageValidationError(
            "Refusing to reset an output directory with a symlinked job root"
        )
    parent = parent_input.resolve()
    if target_input.is_symlink():
        raise StreamingPackageValidationError(
            "Refusing to reset a symlinked package directory"
        )
    target = target_input.resolve()
    if target.parent != parent:
        raise StreamingPackageValidationError(
            "Package retry directory is not an output-id child of the job root"
        )
    if target.exists():
        if not target.is_dir():
            raise StreamingPackageValidationError(
                "Package retry target is not a directory"
            )
        import shutil
        shutil.rmtree(str(target))
    target.mkdir(parents=True, exist_ok=True)
    return str(target)



def _probe_media(path, ffprobe_bin_path, timeout_seconds, should_cancel=None):
    command = [
        ffprobe_bin_path,
        "-v", "error",
        "-show_entries",
        "stream=codec_type,codec_name,width,height,bit_rate:"
        "format=duration,bit_rate",
        "-of", "json",
        str(path),
    ]
    stdout, _ = run_cancellable_process(
        command,
        timeout_seconds=timeout_seconds,
        should_cancel=should_cancel,
    )
    return json.loads(stdout.decode("utf-8") or "{}")


def _hls_attribute_map(line):
    value = line.split(":", 1)[1]
    fields = []
    current = []
    quoted = False
    for character in value:
        if character == '"':
            quoted = not quoted
        if character == "," and not quoted:
            fields.append("".join(current))
            current = []
        else:
            current.append(character)
    if current:
        fields.append("".join(current))
    result = {}
    for field_value in fields:
        if "=" not in field_value:
            continue
        key, item = field_value.split("=", 1)
        result[key.strip().upper()] = item.strip().strip('"')
    return result


def _codec_family(value):
    token = (value or "").lower().split(".", 1)[0]
    aliases = {
        "avc1": "h264",
        "avc3": "h264",
        "hvc1": "hevc",
        "hev1": "hevc",
        "vp09": "vp9",
        "vp9": "vp9",
        "mp4a": "aac",
        "ac-3": "ac3",
        "ec-3": "eac3",
    }
    return aliases.get(token, token)


def _hls_master_attributes(manifest):
    attributes_by_path = {}
    pending = None
    for raw_line in manifest.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = _hls_attribute_map(line)
        elif line.startswith("#EXT-X-I-FRAME-STREAM-INF:"):
            attrs = _hls_attribute_map(line)
            uri = attrs.get("URI")
            if uri:
                attributes_by_path[uri] = attrs
        elif line and not line.startswith("#") and pending is not None:
            attributes_by_path[line] = pending
            pending = None
    return attributes_by_path


def _hls_extinf_summary(playlist):
    durations = []
    target_duration = 0.0
    for raw_line in playlist.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("#EXTINF:"):
            try:
                durations.append(float(line.split(":", 1)[1].split(",", 1)[0]))
            except ValueError:
                pass
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target_duration = float(line.split(":", 1)[1])
            except ValueError:
                pass
    return durations, target_duration



def _hls_segment_paths(playlist):
    """Return the validated standalone media segment paths in an HLS rendition."""
    paths = []
    pending_extinf = False
    for raw_line in playlist.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            pending_extinf = True
            continue
        if line.startswith("#"):
            continue
        if pending_extinf:
            paths.append(_non_empty_file(playlist.parent, line))
            pending_extinf = False
    return paths


def _probe_hls_media_duration(
        playlist, ffprobe_bin_path, timeout_seconds, should_cancel=None):
    """Derive media duration from actual packet timestamps, not EXTINF tags.

    Fragmented MP4 media fragments require their initialization section and are
    not independently duration-probeable; in that case duration comparison is
    omitted rather than treating the manifest's own EXTINF values as evidence.
    """
    segments = _hls_segment_paths(playlist)
    if not segments or any(path.suffix.lower() == ".m4s" for path in segments):
        return None

    with tempfile.TemporaryDirectory(prefix="hls-probe-") as temporary_directory:
        temporary_root = Path(temporary_directory)
        lines = ["ffconcat version 1.0"]
        for index, segment in enumerate(segments):
            link = temporary_root / "segment_{:06d}{}".format(
                index, segment.suffix.lower()
            )
            os.symlink(str(segment), str(link))
            lines.append("file {}".format(link))
        concat_path = temporary_root / "segments.ffconcat"
        concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        command = [
            ffprobe_bin_path,
            "-v", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_path),
            "-show_packets",
            "-show_entries", "packet=pts_time,duration_time",
            "-of", "csv=p=0",
        ]
        stdout, _ = run_cancellable_process(
            command,
            timeout_seconds=timeout_seconds,
            should_cancel=should_cancel,
        )
        first_pts = None
        last_packet_end = None
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            fields = line.strip().split(",")
            if not fields:
                continue
            try:
                pts = float(fields[0])
            except (TypeError, ValueError):
                continue
            try:
                packet_duration = float(fields[1])
            except (IndexError, TypeError, ValueError):
                packet_duration = 0.0
            first_pts = pts if first_pts is None else min(first_pts, pts)
            packet_end = pts + max(0.0, packet_duration)
            last_packet_end = (
                packet_end if last_packet_end is None
                else max(last_packet_end, packet_end)
            )
        if first_pts is None or last_packet_end is None:
            return None
        return max(0.0, last_packet_end - first_pts)


def _probe_package_metadata(
        directory,
        package_type,
        manifest_path,
        ffprobe_bin_path,
        timeout_seconds,
        should_cancel=None):
    """Probe headers/metadata only; never decode or re-encode media."""
    root = Path(directory).resolve()
    manifest = Path(manifest_path).resolve()
    issues = []

    def add(code, location, message):
        issues.append(PackageValidationIssue(code, str(location), message))

    try:
        if package_type == "hls":
            attributes = _hls_master_attributes(manifest)
            for rendition_path in sorted(attributes):
                local_path = (root / rendition_path).resolve()
                metadata = _probe_media(
                    local_path, ffprobe_bin_path, timeout_seconds,
                    should_cancel=should_cancel,
                )
                streams = metadata.get("streams", [])
                video_stream = next(
                    (stream for stream in streams
                     if stream.get("codec_type") == "video"),
                    None,
                )
                attrs = attributes[rendition_path]
                location = local_path.relative_to(root).as_posix()

                resolution = attrs.get("RESOLUTION")
                if resolution and video_stream:
                    expected_width, expected_height = [
                        int(value) for value in resolution.lower().split("x", 1)
                    ]
                    actual_width = int(video_stream.get("width", 0))
                    actual_height = int(video_stream.get("height", 0))
                    if (expected_width, expected_height) != (
                            actual_width, actual_height):
                        add(
                            "resolution_mismatch",
                            location,
                            "Manifest resolution {}x{} does not match media {}x{}"
                            .format(
                                expected_width, expected_height,
                                actual_width, actual_height,
                            ),
                        )

                codecs = attrs.get("CODECS")
                if codecs:
                    expected_families = {
                        _codec_family(value)
                        for value in codecs.split(",")
                    }
                    actual_families = {
                        _codec_family(stream.get("codec_name"))
                        for stream in streams
                        if stream.get("codec_name")
                    }
                    if not expected_families.issubset(actual_families):
                        add(
                            "codec_mismatch",
                            location,
                            "Manifest CODECS {} do not match probed streams {}"
                            .format(
                                sorted(expected_families),
                                sorted(actual_families),
                            ),
                        )

                bandwidth = attrs.get("BANDWIDTH")
                measured = (metadata.get("format") or {}).get("bit_rate")
                if bandwidth and measured:
                    declared = int(bandwidth)
                    actual_bitrate = int(measured)
                    if actual_bitrate > declared * 1.10:
                        add(
                            "bitrate_mismatch",
                            location,
                            "Measured bitrate {} exceeds declared BANDWIDTH {}"
                            .format(actual_bitrate, declared),
                        )

                durations, target_duration = _hls_extinf_summary(local_path)
                measured_duration = _probe_hls_media_duration(
                    local_path,
                    ffprobe_bin_path,
                    timeout_seconds,
                    should_cancel=should_cancel,
                )
                if durations and measured_duration:
                    manifest_duration = sum(durations)
                    actual_duration = float(measured_duration)
                    tolerance = max(
                        (target_duration or 0.0) * 0.10,
                        actual_duration * 0.02,
                        0.25,
                    )
                    if abs(manifest_duration - actual_duration) > tolerance:
                        add(
                            "duration_mismatch",
                            location,
                            "EXTINF duration {:.3f}s differs from probed media {:.3f}s"
                            .format(manifest_duration, actual_duration),
                        )

        elif package_type == "dash":
            metadata = _probe_media(
                manifest, ffprobe_bin_path, timeout_seconds,
                should_cancel=should_cancel,
            )
            streams = [
                stream for stream in metadata.get("streams", [])
                if stream.get("codec_type") == "video"
            ]
            mpd = ET.fromstring(manifest.read_bytes())
            representations = []
            for period in _children(mpd, "Period"):
                for adaptation in _children(period, "AdaptationSet"):
                    inherited_codecs = adaptation.attrib.get("codecs")
                    for representation in _children(
                            adaptation, "Representation"):
                        item = dict(representation.attrib)
                        item.setdefault("codecs", inherited_codecs)
                        representations.append(item)

            if streams and len(streams) != len(representations):
                add(
                    "representation_count_mismatch",
                    manifest,
                    "MPD declares {} representations but ffprobe found {} video streams"
                    .format(len(representations), len(streams)),
                )
            for index, (representation, stream) in enumerate(
                    zip(representations, streams)):
                location = "{}#Representation[{}]".format(manifest, index)
                declared_width = representation.get("width")
                declared_height = representation.get("height")
                if declared_width and declared_height:
                    actual_size = (
                        int(stream.get("width", 0)),
                        int(stream.get("height", 0)),
                    )
                    declared_size = (
                        int(declared_width), int(declared_height)
                    )
                    if declared_size != actual_size:
                        add(
                            "resolution_mismatch",
                            location,
                            "MPD resolution {} does not match probed media {}"
                            .format(declared_size, actual_size),
                        )

                declared_codecs = representation.get("codecs")
                actual_codec = stream.get("codec_name")
                if declared_codecs and actual_codec:
                    declared_families = {
                        _codec_family(value)
                        for value in declared_codecs.split(",")
                    }
                    if _codec_family(actual_codec) not in declared_families:
                        add(
                            "codec_mismatch",
                            location,
                            "MPD codec {} does not match probed codec {}"
                            .format(declared_codecs, actual_codec),
                        )

                bandwidth = representation.get("bandwidth")
                actual_bitrate = stream.get("bit_rate")
                if bandwidth and actual_bitrate:
                    if int(actual_bitrate) > int(bandwidth) * 1.10:
                        add(
                            "bitrate_mismatch",
                            location,
                            "Measured bitrate {} exceeds MPD bandwidth {}"
                            .format(actual_bitrate, bandwidth),
                        )

            mpd_duration = mpd.attrib.get("mediaPresentationDuration")
            actual_duration = (metadata.get("format") or {}).get("duration")
            if mpd_duration and actual_duration:
                declared_duration = _duration_seconds(mpd_duration)
                actual_duration = float(actual_duration)
                if abs(declared_duration - actual_duration) > max(
                        1.0, actual_duration * 0.02):
                    add(
                        "duration_mismatch",
                        manifest,
                        "MPD duration {:.3f}s differs from probed media {:.3f}s"
                        .format(declared_duration, actual_duration),
                    )
    except subprocess.TimeoutExpired:
        add(
            "metadata_probe_timeout",
            manifest,
            "ffprobe metadata inspection exceeded {} seconds".format(
                timeout_seconds
            ),
        )
    except (OSError, subprocess.CalledProcessError, ValueError, ET.ParseError) as exc:
        add(
            "metadata_probe_failed",
            manifest,
            "ffprobe metadata inspection failed: {}".format(exc),
        )

    return issues
