#!/usr/bin/env python3
"""Shared reader for `.release.json`, the per-repository release description.

`.release.json` lives in the repository root and is the single place a
repository states what it ships. Example for an integration with a companion
app:

{
  "tag_prefix": "v",
  "timezone": "America/Chicago",
  "archive": {"source": "custom_components/ha_soc", "name": "ha_soc.zip"},
  "release_paths": ["custom_components/ha_soc", "ha_soc_probe"],
  "version_fields": [
    {"path": "custom_components/ha_soc/manifest.json", "kind": "json", "key": "version"},
    {"path": "ha_soc_probe/config.yaml", "kind": "regex", "pattern": "^version:\\s*\"([^\"]+)\"", "template": "version: \"{version}\""},
    {"path": "ha_soc_probe/rootfs/etc/services.d/ha_soc_probe/run", "kind": "regex", "pattern": "^SCANNER_VERSION=\"([^\"]+)\"", "template": "SCANNER_VERSION=\"{version}\""}
  ]
}

`archive` is optional: omit it for repositories HACS installs from the tagged
tree (no `zip_release`). `tag_prefix` may be an empty string for repositories
whose tags carry the bare CalVer number.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# YYYY.MM.DD.N is the written form. A bare YYYY.MM.DD is accepted when reading
# so repositories that released before the sequence was added still validate;
# set_version.py always writes the four-part form.
CALVER_RE = re.compile(r"^(?P<year>[0-9]{4})\.(?P<month>[0-9]{2})\.(?P<day>[0-9]{2})(?:\.(?P<sequence>[1-9][0-9]*))?$")


@dataclass(frozen=True)
class VersionField:
    path: str
    kind: str  # json or regex
    key: str | None = None
    pattern: str | None = None
    template: str | None = None


@dataclass(frozen=True)
class ReleaseConfig:
    repository: Path
    tag_prefix: str
    timezone: str
    release_paths: list[str]
    version_fields: list[VersionField]
    archive_source: str | None = None
    archive_name: str | None = None
    extra: dict = field(default_factory=dict)


def load(repository: Path) -> ReleaseConfig:
    path = repository / ".release.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} is missing; see release_config.py for the format")
    data = json.loads(path.read_text(encoding="utf-8"))
    fields = [VersionField(**f) for f in data["version_fields"]]
    if not fields:
        raise ValueError(".release.json must list at least one version field")
    for f in fields:
        if f.kind == "json" and not f.key:
            raise ValueError(f"json field {f.path} needs a key")
        if f.kind == "regex" and not (f.pattern and f.template):
            raise ValueError(f"regex field {f.path} needs pattern and template")
        if f.kind not in {"json", "regex"}:
            raise ValueError(f"unknown field kind {f.kind}")
    archive = data.get("archive") or {}
    return ReleaseConfig(
        repository=repository.resolve(),
        tag_prefix=data.get("tag_prefix", "v"),
        timezone=data.get("timezone", "America/Chicago"),
        release_paths=list(data.get("release_paths", [])),
        version_fields=fields,
        archive_source=archive.get("source"),
        archive_name=archive.get("name"),
    )


def read_field(repository: Path, f: VersionField) -> str:
    text = (repository / f.path).read_text(encoding="utf-8")
    if f.kind == "json":
        value = json.loads(text).get(f.key)
        return value if isinstance(value, str) else "<missing>"
    match = re.search(f.pattern or "", text, re.MULTILINE)
    return match.group(1) if match else "<missing>"


def read_versions(config: ReleaseConfig) -> dict[str, str]:
    return {f.path: read_field(config.repository, f) for f in config.version_fields}


def validate_versions(config: ReleaseConfig) -> str:
    """Return the single shipped version or raise on drift or bad format."""
    versions = read_versions(config)
    distinct = set(versions.values())
    if len(distinct) != 1:
        rendered = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise ValueError(f"Component versions do not match: {rendered}")
    version = next(iter(distinct))
    if not CALVER_RE.fullmatch(version):
        raise ValueError(f"Invalid release version: {version}")
    return version
