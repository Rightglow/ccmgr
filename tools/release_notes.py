#!/usr/bin/env python3
"""Render one GitHub Release body from Railmux's changelog."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_SECTION_RE = re.compile(r"^## \[([^]]+)](?:\s+-\s+.+)?\s*$")
_LINK_RE = re.compile(r"^\[([^]]+)]\s*:\s*(\S+)\s*$")


def normalize_version(value: str) -> str:
    """Return a plain semantic version accepted by the changelog format."""
    version = value.removeprefix("v")
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid release version: {value!r}")
    return version


def render_release_notes(changelog: str, requested_version: str) -> str:
    """Extract a released section and its optional comparison link."""
    version = normalize_version(requested_version)
    lines = changelog.splitlines()
    start: int | None = None
    end = len(lines)

    for index, line in enumerate(lines):
        match = _SECTION_RE.fullmatch(line)
        if match and match.group(1) == version:
            start = index + 1
            break
    if start is None:
        raise ValueError(f"CHANGELOG.md has no [{version}] release section")

    for index in range(start, len(lines)):
        if _SECTION_RE.fullmatch(lines[index]) or _LINK_RE.fullmatch(lines[index]):
            end = index
            break

    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md [{version}] release section is empty")

    comparison_url = None
    for line in lines:
        match = _LINK_RE.fullmatch(line)
        if match and match.group(1) == version:
            comparison_url = match.group(2)
            break
    # The first published release links to its own Release page for changelog
    # navigation. That is not a useful "Full Changelog" link in the body.
    if comparison_url and "/compare/" in comparison_url:
        body += f"\n\n**Full Changelog**: {comparison_url}"
    return body + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a GitHub Release body from CHANGELOG.md.")
    parser.add_argument("version", help="release version or tag, for example 0.2.3 or v0.2.3")
    parser.add_argument(
        "--changelog", type=Path, default=Path("CHANGELOG.md"),
        help="changelog path (default: CHANGELOG.md)")
    parser.add_argument(
        "--output", type=Path,
        help="write to this path instead of stdout")
    args = parser.parse_args(argv)

    try:
        notes = render_release_notes(
            args.changelog.read_text(encoding="utf-8"), args.version)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.output is None:
        sys.stdout.write(notes)
    else:
        args.output.write_text(notes, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
