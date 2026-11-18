#!/usr/bin/env python3
"""Validate repository-local Markdown links in changed Markdown files.

External URLs and in-page anchors are deliberately not fetched. This keeps the
documentation check deterministic, offline, and fast.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
LINK_RE = re.compile(
    r"!?(?:\[[^\]]*\])\(\s*(?:<([^>]+)>|([^\s)]+))"
)
REFERENCE_RE = re.compile(
    r"^\s*\[[^\]]+\]:\s*(?:<([^>]+)>|([^\s]+))",
    re.MULTILINE,
)
FENCED_CODE_RE = re.compile(
    r"(?ms)^\s*(```|~~~).*?^\s*\1\s*$"
)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def changed_markdown(base, head):
    result = subprocess.run(
        ["git", "diff", "--name-only", "-z", base, head],
        check=True,
        stdout=subprocess.PIPE,
    )
    return [
        path.decode("utf-8", errors="replace")
        for path in result.stdout.split(b"\0")
        if path and path.decode("utf-8", errors="replace").lower().endswith(".md")
    ]


def extract_destinations(markdown):
    markdown = FENCED_CODE_RE.sub("", markdown)
    markdown = INLINE_CODE_RE.sub("", markdown)
    destinations = []
    for match in LINK_RE.finditer(markdown):
        destinations.append(match.group(1) or match.group(2))
    for match in REFERENCE_RE.finditer(markdown):
        destinations.append(match.group(1) or match.group(2))
    return destinations


def validate_link(source_file, destination):
    destination = destination.strip()
    parsed = urlsplit(destination)

    if parsed.scheme or parsed.netloc or not parsed.path:
        return None

    raw_path = unquote(parsed.path)
    if raw_path.startswith("/"):
        candidate = ROOT / raw_path.lstrip("/")
    else:
        candidate = source_file.parent / raw_path

    resolved = candidate.resolve()
    try:
        resolved.relative_to(ROOT)
    except ValueError:
        return "resolves outside the repository"

    if not resolved.exists():
        return "target does not exist"
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    if args.all or not args.base or not args.head:
        paths = subprocess.run(
            ["git", "ls-files", "-z"],
            check=True,
            stdout=subprocess.PIPE,
        ).stdout.split(b"\0")
        markdown_paths = [
            item.decode("utf-8", errors="replace")
            for item in paths
            if item and item.decode("utf-8", errors="replace").lower().endswith(".md")
        ]
    else:
        markdown_paths = changed_markdown(args.base, args.head)

    errors = []
    for relative in markdown_paths:
        source = (ROOT / relative).resolve()
        if not source.is_file():
            continue
        content = source.read_text(encoding="utf-8")
        for destination in extract_destinations(content):
            error = validate_link(source, destination)
            if error:
                errors.append("{}: {} ({})".format(relative, destination, error))

    if errors:
        for error in errors:
            print("ERROR:", error)
        return 1

    print("Validated {} Markdown file(s); no broken repository-local links.".format(
        len(markdown_paths)
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
