#!/usr/bin/env python3
"""Calculate and apply a synchronized CalVer release version.

This is the only writer for the version fields listed in `.release.json`;
build_release_artifacts.validate_versions is the independent reader the
release gate uses, so a defect here cannot validate itself.

Usage:
    python scripts/set_version.py --next-from-tags            # today's next sequence
    python scripts/set_version.py --version 2026.09.03.2      # explicit
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_config import CALVER_RE, ReleaseConfig, load, validate_versions  # noqa: E402


def parse_calver(value: str) -> tuple[date, int]:
    match = CALVER_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"Invalid CalVer release version: {value}")
    sequence = int(match["sequence"]) if match["sequence"] else 0
    return date(int(match["year"]), int(match["month"]), int(match["day"])), sequence


def next_calver(existing: list[str], release_date: date, prefix: str) -> str:
    sequences: list[int] = []
    for value in existing:
        bare = value[len(prefix):] if prefix and value.startswith(prefix) else value
        try:
            version_date, sequence = parse_calver(bare)
        except ValueError:
            continue
        if version_date == release_date:
            sequences.append(sequence)
    return f"{release_date:%Y.%m.%d}.{max(sequences, default=0) + 1}"


def versions_from_git_tags(repository: Path, prefix: str) -> list[str]:
    result = subprocess.run(["git", "tag", "--list", f"{prefix}[0-9]*"], cwd=repository, check=True, capture_output=True, text=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _replace_one(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f"Expected exactly one {label}; found {count}")
    return updated


def set_version(config: ReleaseConfig, version: str) -> None:
    parse_calver(version)
    rendered: dict[Path, str] = {}
    for f in config.version_fields:
        path = config.repository / f.path
        text = path.read_text(encoding="utf-8")
        if f.kind == "json":
            data = json.loads(text)
            if not isinstance(data.get(f.key), str):
                raise ValueError(f"{f.path} has no string {f.key}")
            data[f.key] = version
            rendered[path] = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        else:
            rendered[path] = _replace_one(text, f.pattern or "", (f.template or "").format(version=version), f.path)
    for path, text in rendered.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    if validate_versions(config) != version:
        raise ValueError("Version synchronization failed after writing files")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--version", help="explicit YYYY.MM.DD.N version")
    group.add_argument("--next-from-tags", action="store_true", help="calculate today's next sequence from local git tags")
    parser.add_argument("--timezone", default=None, help="IANA zone; defaults to .release.json timezone")
    args = parser.parse_args()
    config = load(args.repository)
    version = args.version
    if args.next_from_tags:
        zone = args.timezone or config.timezone
        try:
            release_date = datetime.now(ZoneInfo(zone)).date()
        except ZoneInfoNotFoundError as err:
            # Linux runners ship zone data; a Windows workstation without the
            # tzdata package does not. Fall back to the local clock and say so.
            print(f"warning: zone {zone} unavailable ({err}); using the local system date. Install tzdata to fix.", file=sys.stderr)
            release_date = datetime.now().astimezone().date()
        version = next_calver(versions_from_git_tags(config.repository, config.tag_prefix), release_date, config.tag_prefix)
    assert version is not None
    set_version(config, version)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
