#!/usr/bin/env python3
"""Validate shipped versions and build the deterministic HACS release archive.

Reads `.release.json` (see release_config.py). `validate_versions` is the
independent reader the release gate relies on; the writer is set_version.py.
Stable ordering, timestamps, permissions, and compression make the same source
tree produce the same SHA-256 digest on every runner.

Usage:
    python scripts/build_release_artifacts.py --validate-only
    python scripts/build_release_artifacts.py --output dist/<name>.zip
"""

from __future__ import annotations

import argparse
import hashlib
import stat
import sys
import zipfile
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from release_config import load, validate_versions  # noqa: E402

_FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_SKIP_PARTS = {"__pycache__", "node_modules"}


def _release_files(source: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in source.rglob("*")
            if path.is_file()
            and not _SKIP_PARTS.intersection(path.relative_to(source).parts)
            and path.suffix not in {".pyc", ".pyo"}
        ),
        key=lambda path: path.relative_to(source).as_posix(),
    )


def build_archive(repository: Path, output: Path) -> tuple[str, str]:
    config = load(repository)
    version = validate_versions(config)
    if not config.archive_source:
        raise ValueError(".release.json has no archive section; this repository releases from the tagged tree")
    source = config.repository / config.archive_source
    files = _release_files(source)
    if not files or source / "manifest.json" not in files:
        raise ValueError("Release source is empty or missing manifest.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, strict_timestamps=True) as archive:
        archive.comment = f"{source.name} {version}".encode("ascii")
        for path in files:
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=_FIXED_ZIP_TIME)
            info.create_system = 3
            executable = bool(path.stat().st_mode & stat.S_IXUSR)
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.flag_bits |= 0x800
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return version, hashlib.sha256(output.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=None, help="archive path; defaults to dist/<archive.name>")
    parser.add_argument("--validate-only", action="store_true", help="print the version and exit")
    args = parser.parse_args()
    config = load(args.repository)
    if args.validate_only or not config.archive_source:
        print(validate_versions(config))
        return 0
    output = args.output or (config.repository / "dist" / (config.archive_name or f"{Path(config.archive_source).name}.zip"))
    if not output.is_absolute():
        output = config.repository / output
    version, digest = build_archive(args.repository, output)
    print(f"archive={output}")
    print(f"version={version}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
