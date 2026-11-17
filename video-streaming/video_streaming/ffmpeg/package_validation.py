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

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import unquote, urlsplit


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
            if duration != duration or duration <= 0:
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



def ordered_package_files(directory: str, manifest_name: str):
    """Return package files with all manifests uploaded after their media.

    HLS media playlists are uploaded after segments and the master playlist is
    uploaded last. DASH MPD is uploaded last. Relative paths are preserved.
    """
    root = Path(directory).resolve()
    if not root.is_dir():
        raise StreamingPackageValidationError(
            "Streaming package directory does not exist"
        )

    files = []
    for current, directories, filenames in os.walk(str(root), followlinks=False):
        directories.sort()
        for name in list(directories):
            if (Path(current) / name).is_symlink():
                raise StreamingPackageValidationError(
                    "Symlink directories are not allowed in a streaming package"
                )
        for name in sorted(filenames):
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise StreamingPackageValidationError(
                    "Symlink or non-regular output file is not allowed: " + name
                )
            relative = path.relative_to(root).as_posix()
            size = path.stat().st_size
            if size <= 0:
                raise StreamingPackageValidationError(
                    "Package contains an empty output file: " + relative
                )
            if relative == manifest_name:
                rank = 2
            elif path.suffix.lower() in (".m3u8", ".mpd"):
                rank = 1
            else:
                rank = 0
            files.append((rank, str(path), relative, size))

    files.sort(key=lambda item: (item[0], item[2]))
    return [(path, relative, size) for _, path, relative, size in files]



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


def validate_package(
        directory: str,
        package_type: str,
        expected_renditions: Optional[List[str]],
        manifest_path: str = None) -> PackageValidationReport:
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
    except Exception as exc:
        message = str(exc)
        report.errors.append(PackageValidationIssue(
            code=_validation_error_code(message),
            location=str(manifest),
            message=message,
        ))

    return report
