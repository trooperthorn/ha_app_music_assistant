"""Build, verify, and stage a complete Music Assistant data recovery set.

Operate on a quiesced copy of /data (for example, an extracted Home Assistant
backup), never the running server directory. No command replaces live data.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path, PurePosixPath

MANIFEST = "recovery-set.json"
COMPLETION = "COMPLETED"
DATA = "data"
ARCHIVE = "library_enrichment/enrichment.db"
FORMAT_VERSION = 1
MAX_ARCHIVE_SCHEMA = 11
PINNED_SERVER_VERSION = "2.10.4"
PINNED_LIBRARY_SCHEMA = 58
REQUIRED = {"settings.json", "library.db", "auth.db", ARCHIVE}


def _is_link(path: Path) -> bool:
    """Reject symlinks and Windows junctions before traversing recovery data."""
    return path.is_symlink() or path.is_junction()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _files(root: Path) -> dict[str, dict[str, int | str]]:
    if not root.is_dir() or _is_link(root):
        raise ValueError("Recovery data root must be a real directory")
    files: dict[str, dict[str, int | str]] = {}
    for current, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            path = Path(current) / name
            if _is_link(path):
                raise ValueError(f"Recovery data contains a link or junction: {path}")
        for name in names:
            path = Path(current) / name
            if not path.is_file():
                raise ValueError(f"Recovery data contains a non-file: {path}")
            relative = path.relative_to(root).as_posix()
            files[relative] = {"size": path.stat().st_size, "sha256": _sha256(path)}
    if not REQUIRED <= files.keys():
        raise ValueError("Music Assistant settings, library, auth, or enrichment data is missing")
    return dict(sorted(files.items()))


def _archive_identity(path: Path) -> dict[str, int | str]:
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Library Enrichment database failed integrity check")
            schema = db.execute("PRAGMA user_version").fetchone()[0]
            row = db.execute("SELECT value FROM metadata WHERE key='store_uuid'").fetchone()
            if not 1 <= schema <= MAX_ARCHIVE_SCHEMA or row is None:
                raise ValueError("Unsupported Library Enrichment database identity or schema")
            uuid.UUID(row[0])
            if db.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Library Enrichment foreign keys are invalid")
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            schema_sql = [
                entry[0]
                for entry in db.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
                )
            ]
            schema_digest = hashlib.sha256(
                json.dumps(schema_sql, ensure_ascii=False).encode()
            ).hexdigest()
            if metadata.get("schema_digest") != schema_digest:
                raise ValueError("Library Enrichment schema digest mismatch")
            for version_id, total, expected in db.execute(
                "SELECT id,total,content_digest FROM versions"
            ):
                payloads = [
                    occurrence[0]
                    for occurrence in db.execute(
                        "SELECT payload FROM occurrences WHERE version_id=? ORDER BY position",
                        (version_id,),
                    )
                ]
                content_digest = hashlib.sha256(
                    json.dumps(payloads, ensure_ascii=False).encode()
                ).hexdigest()
                if len(payloads) != total or content_digest != expected:
                    raise ValueError("Library Enrichment version content digest mismatch")
            return {"store_uuid": row[0], "schema_version": schema}
    except sqlite3.DatabaseError as err:
        raise ValueError("Library Enrichment database cannot be opened") from err


def _verify_databases(root: Path, files: dict) -> None:
    for name in files:
        if not name.endswith(".db"):
            continue
        path = root / name
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError(f"Recovery database failed integrity check: {name}")
        except sqlite3.DatabaseError as err:
            raise ValueError(f"Recovery database cannot be opened: {name}") from err


def _verify_core(root: Path) -> None:
    try:
        settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("Music Assistant settings must be a JSON object")
        server_id = settings.get("server_id")
        encryption_key = settings.get("encryption_key")
        if not isinstance(server_id, str) or not server_id.strip():
            raise ValueError("Music Assistant server identity is missing")
        if not isinstance(encryption_key, str):
            raise ValueError("Music Assistant encryption key is missing")
        try:
            key_bytes = base64.b64decode(encryption_key, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as err:
            raise ValueError("Music Assistant encryption key is invalid") from err
        if len(key_bytes) != 32:
            raise ValueError("Music Assistant encryption key is invalid")
    except (UnicodeError, json.JSONDecodeError) as err:
        raise ValueError("Music Assistant settings cannot be parsed") from err
    required_tables = {
        "library.db": {"settings", "artists", "albums", "tracks", "playlists", "provider_mappings"},
        "auth.db": {"settings", "users", "user_auth_providers", "auth_tokens"},
    }
    for name, expected_tables in required_tables.items():
        with closing(sqlite3.connect((root / name).resolve().as_uri() + "?mode=ro", uri=True)) as db:
            tables = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not expected_tables <= tables:
                raise ValueError(f"Music Assistant database is missing required tables: {name}")
            if name == "library.db":
                version = db.execute("SELECT value FROM settings WHERE key='version'").fetchone()
                if version is None or version[0] != str(PINNED_LIBRARY_SCHEMA):
                    raise ValueError("Music Assistant library schema differs from pinned server 2.10.4")


def _write_exclusive(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def create(source: Path, destination: Path, versions: dict[str, str]) -> dict:
    """Publish a verified set from an already quiesced data directory."""
    if _is_link(source):
        raise ValueError("Recovery source must not be a link or junction")
    source = source.resolve(strict=True)
    destination = destination.parent.resolve() / destination.name
    if os.path.lexists(destination) or destination == source or destination.is_relative_to(source):
        raise ValueError("Recovery destination must be new and outside the source")
    if set(versions) != {"app", "server", "frontend"} or not all(
        isinstance(value, str) and value.strip() for value in versions.values()
    ):
        raise ValueError("Installed app, server, and frontend versions are required")
    if versions["server"] != PINNED_SERVER_VERSION:
        raise ValueError("Recovery set creation requires the validated server version 2.10.4")
    before = _files(source)
    identity = _archive_identity(source / ARCHIVE)
    _verify_databases(source, before)
    _verify_core(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = destination.parent / f".{destination.name}.{uuid.uuid4()}.tmp"
    try:
        candidate.mkdir()
        shutil.copytree(source, candidate / DATA, symlinks=False)
        copied = _files(candidate / DATA)
        after = _files(source)
        if before != copied or before != after:
            raise ValueError("Recovery source changed or copy differs; no set published")
        if _archive_identity(candidate / DATA / ARCHIVE) != identity:
            raise ValueError("Copied archive identity differs")
        _verify_databases(candidate / DATA, copied)
        _verify_core(candidate / DATA)
        manifest = {
            "format_version": FORMAT_VERSION,
            "versions": versions,
            "archive": identity,
            "files": copied,
        }
        manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        _write_exclusive(candidate / MANIFEST, manifest_bytes)
        _write_exclusive(candidate / COMPLETION, (hashlib.sha256(manifest_bytes).hexdigest() + "\n").encode())
        os.rename(candidate, destination)
        return manifest
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


def verify(recovery_set: Path, expected_versions: dict[str, str] | None = None) -> dict:
    """Verify every published byte and the archive database without mutations."""
    if _is_link(recovery_set):
        raise ValueError("Recovery set must not be a link or junction")
    root = recovery_set.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Recovery set must be a real directory")
    expected_top = {DATA, MANIFEST, COMPLETION}
    if {path.name for path in root.iterdir()} != expected_top:
        raise ValueError("Recovery set is incomplete or has unexpected entries")
    if any(_is_link(path) for path in root.iterdir()):
        raise ValueError("Recovery set contains a top-level link or junction")
    manifest_bytes = (root / MANIFEST).read_bytes()
    expected_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if (root / COMPLETION).read_text(encoding="ascii").strip() != expected_digest:
        raise ValueError("Recovery completion marker does not match the manifest")
    manifest = json.loads(manifest_bytes)
    if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
        raise ValueError("Unsupported recovery set format")
    versions = manifest.get("versions")
    if not isinstance(versions, dict) or set(versions) != {"app", "server", "frontend"} or any(
        not isinstance(value, str) or not value.strip() for value in versions.values()
    ):
        raise ValueError("Invalid recovery set versions")
    if versions["server"] != PINNED_SERVER_VERSION:
        raise ValueError("Recovery set server version is not supported by this verifier")
    if expected_versions is not None and manifest.get("versions") != expected_versions:
        raise ValueError("Recovery set versions differ from the requested installation")
    files = manifest.get("files")
    if not isinstance(files, dict) or any(
        not isinstance(name, str)
        or not name
        or PurePosixPath(name).is_absolute()
        or ".." in PurePosixPath(name).parts
        or not isinstance(entry, dict)
        or type(entry.get("size")) is not int
        or not isinstance(entry.get("sha256"), str)
        for name, entry in files.items()
    ):
        raise ValueError("Invalid recovery file manifest")
    if _files(root / DATA) != files:
        raise ValueError("Recovery file hashes or membership differ")
    if _archive_identity(root / DATA / ARCHIVE) != manifest.get("archive"):
        raise ValueError("Recovery archive identity differs")
    _verify_databases(root / DATA, files)
    _verify_core(root / DATA)
    return manifest


def stage_restore(recovery_set: Path, destination: Path, expected_versions: dict[str, str]) -> dict:
    """Copy a verified set to a new directory; never overwrite a live installation."""
    manifest = verify(recovery_set, expected_versions)
    if manifest["archive"]["schema_version"] != MAX_ARCHIVE_SCHEMA:
        raise ValueError("Archive schema needs an explicit migration review before restore")
    destination = destination.parent.resolve() / destination.name
    source = recovery_set.resolve(strict=True)
    if os.path.lexists(destination) or destination == source or destination.is_relative_to(source):
        raise ValueError("Restore staging directory must be new and outside the recovery set")
    destination.parent.mkdir(parents=True, exist_ok=True)
    candidate = destination.parent / f".{destination.name}.{uuid.uuid4()}.tmp"
    try:
        shutil.copytree(source / DATA, candidate, symlinks=False)
        if _files(candidate) != manifest["files"] or _archive_identity(candidate / ARCHIVE) != manifest["archive"]:
            raise ValueError("Staged restore differs from the verified recovery set")
        _verify_databases(candidate, manifest["files"])
        _verify_core(candidate)
        os.rename(candidate, destination)
        return manifest
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


def cutover(
    recovery_set: Path, staged_data: Path, current_data: Path, rollback_data: Path,
    expected_versions: dict[str, str], *, stopped_confirmed: bool,
) -> dict:
    """Swap verified staged data into a stopped installation, retaining the old directory."""
    if not stopped_confirmed:
        raise ValueError("The Music Assistant app must be confirmed stopped before cutover")
    manifest = verify(recovery_set, expected_versions)
    if manifest["archive"]["schema_version"] != MAX_ARCHIVE_SCHEMA:
        raise ValueError("Archive schema needs an explicit migration review before cutover")
    for name, path in (("staged", staged_data), ("current", current_data)):
        if _is_link(path) or not path.is_dir():
            raise ValueError(f"{name.capitalize()} data must be a real directory")
    staged = staged_data.resolve(strict=True)
    current = current_data.resolve(strict=True)
    rollback = rollback_data.parent.resolve(strict=True) / rollback_data.name
    recovery_root = recovery_set.resolve(strict=True)
    if os.path.lexists(rollback):
        raise ValueError("Rollback destination must be new")
    if len({staged, current, rollback, recovery_root}) != 4 or any(
        a.is_relative_to(b) for a in (staged, current, rollback) for b in (staged, current, rollback)
        if a != b
    ) or any(
        a.is_relative_to(b) for a, b in (
            (current, recovery_root), (staged, recovery_root),
            (recovery_root, current), (recovery_root, staged),
        )
    ):
        raise ValueError("Cutover paths must be distinct, separate directories")
    if len({staged.stat().st_dev, current.stat().st_dev, rollback.parent.stat().st_dev}) != 1:
        raise ValueError("Cutover and rollback must be on the same filesystem")
    if _files(staged) != manifest["files"] or _archive_identity(staged / ARCHIVE) != manifest["archive"]:
        raise ValueError("Staged data differs from the verified recovery set")
    _verify_databases(staged, manifest["files"])
    _verify_core(staged)

    os.rename(current, rollback)
    try:
        os.rename(staged, current)
        if _files(current) != manifest["files"]:
            raise ValueError("Cutover data differs from the verified recovery set")
    except BaseException:
        if current.exists() and not staged.exists():
            os.rename(current, staged)
        os.rename(rollback, current)
        raise
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify", "stage", "cutover"))
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    for name in ("app", "server", "frontend"):
        parser.add_argument(f"--{name}-version")
    parser.add_argument("--staged-data", type=Path)
    parser.add_argument("--rollback-data", type=Path)
    parser.add_argument("--confirm-stopped", action="store_true")
    args = parser.parse_args()
    versions = {name: getattr(args, f"{name}_version") for name in ("app", "server", "frontend")}
    if any(versions.values()) and not all(versions.values()):
        parser.error("Supply all three installed versions together")
    if args.action == "create":
        if args.destination is None:
            parser.error("create requires a destination")
        result = create(args.source, args.destination, versions)
    elif args.action == "verify":
        result = verify(args.source, versions if all(versions.values()) else None)
    elif args.action == "stage":
        if args.destination is None:
            parser.error("stage requires a destination")
        result = stage_restore(args.source, args.destination, versions)
    else:
        if args.destination is None or args.staged_data is None or args.rollback_data is None or not args.confirm_stopped:
            parser.error("cutover requires current data, --staged-data, --rollback-data, and --confirm-stopped")
        result = cutover(
            args.source, args.staged_data, args.destination, args.rollback_data,
            versions, stopped_confirmed=True,
        )
    print(json.dumps({"status": "cutover" if args.action == "cutover" else "verified",
                      "files": len(result["files"]), "versions": result["versions"]}))


if __name__ == "__main__":
    main()
