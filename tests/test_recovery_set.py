"""Offline, coordinated recovery packaging never replaces a healthy installation."""

import base64
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = _module(ROOT / "scripts/recovery_set.py", "recovery_set")
store_module = _module(
    ROOT / "music_assistant_lm/providers/library_enrichment/store.py", "enrichment_store_for_recovery"
)
VERSIONS = {"app": "2026.09.22.2", "server": "2.10.4", "frontend": "2026.9.22.1"}


def _data(root: Path) -> Path:
    root.mkdir()
    settings = {
        "server_id": "test-server",
        "encryption_key": base64.urlsafe_b64encode(bytes(range(32))).decode(),
    }
    (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    for name in ("library.db", "auth.db"):
        with closing(sqlite3.connect(root / name)) as db:
            tables = (
                ("settings", "artists", "albums", "tracks", "playlists", "provider_mappings")
                if name == "library.db"
                else ("settings", "users", "user_auth_providers", "auth_tokens")
            )
            for table in tables:
                db.execute(f"CREATE TABLE {table} (key TEXT, value TEXT)")
            if name == "library.db":
                db.execute("INSERT INTO settings VALUES ('version', ?)", (str(recovery.PINNED_LIBRARY_SCHEMA),))
            db.commit()
    archive = root / "library_enrichment" / "enrichment.db"
    store = store_module.ArchiveStore(archive)
    store.close()
    return root


def test_recovery_set_round_trip_and_version_gate(tmp_path: Path) -> None:
    assert recovery.MAX_ARCHIVE_SCHEMA == store_module.SCHEMA_VERSION
    source = _data(tmp_path / "quiesced-data")
    recovery_set = tmp_path / "recovery-set"
    manifest = recovery.create(source, recovery_set, VERSIONS)
    assert manifest["archive"]["schema_version"] == store_module.SCHEMA_VERSION
    assert set(manifest["files"]) == recovery.REQUIRED
    assert recovery.verify(recovery_set, VERSIONS) == manifest

    staged = tmp_path / "staged-data"
    assert recovery.stage_restore(recovery_set, staged, VERSIONS) == manifest
    assert recovery._files(staged) == recovery._files(source)
    assert not (source / recovery.COMPLETION).exists()
    with pytest.raises(ValueError, match="must be new"):
        recovery.stage_restore(recovery_set, staged, VERSIONS)
    with pytest.raises(ValueError, match="versions differ"):
        recovery.stage_restore(recovery_set, tmp_path / "wrong-version", {**VERSIONS, "server": "2.10.5"})
    assert not (tmp_path / "wrong-version").exists()


def test_offline_cutover_retains_previous_data_for_rollback(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    recovery_set = tmp_path / "recovery-set"
    recovery.create(source, recovery_set, VERSIONS)
    staged = tmp_path / "staged-data"
    recovery.stage_restore(recovery_set, staged, VERSIONS)
    current = _data(tmp_path / "current-data")
    (current / "prior.txt").write_text("keep this installation", encoding="utf-8")
    rollback = tmp_path / "previous-data"

    with pytest.raises(ValueError, match="confirmed stopped"):
        recovery.cutover(recovery_set, staged, current, rollback, VERSIONS, stopped_confirmed=False)
    assert (current / "prior.txt").exists() and staged.exists() and not rollback.exists()

    recovery.cutover(recovery_set, staged, current, rollback, VERSIONS, stopped_confirmed=True)
    assert recovery._files(current) == recovery._files(source)
    assert (rollback / "prior.txt").read_text(encoding="utf-8") == "keep this installation"
    assert not staged.exists()


def test_cutover_failure_restores_previous_data(tmp_path: Path, monkeypatch) -> None:
    source = _data(tmp_path / "quiesced-data")
    recovery_set = tmp_path / "recovery-set"
    recovery.create(source, recovery_set, VERSIONS)
    staged = tmp_path / "staged-data"
    recovery.stage_restore(recovery_set, staged, VERSIONS)
    current = _data(tmp_path / "current-data")
    (current / "prior.txt").write_text("original", encoding="utf-8")
    rollback = tmp_path / "previous-data"
    original_rename = recovery.os.rename
    calls = 0

    def fail_install(src, dst):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated install failure")
        return original_rename(src, dst)

    monkeypatch.setattr(recovery.os, "rename", fail_install)
    with pytest.raises(OSError, match="simulated install failure"):
        recovery.cutover(recovery_set, staged, current, rollback, VERSIONS, stopped_confirmed=True)
    assert (current / "prior.txt").read_text(encoding="utf-8") == "original"
    assert staged.exists() and not rollback.exists()


def test_cutover_post_swap_validation_failure_rolls_back(tmp_path: Path, monkeypatch) -> None:
    source = _data(tmp_path / "quiesced-data")
    recovery_set = tmp_path / "recovery-set"
    recovery.create(source, recovery_set, VERSIONS)
    staged = tmp_path / "staged-data"
    recovery.stage_restore(recovery_set, staged, VERSIONS)
    current = _data(tmp_path / "current-data")
    (current / "prior.txt").write_text("original", encoding="utf-8")
    rollback = tmp_path / "previous-data"
    original_files = recovery._files

    def invalid_after_swap(path):
        if path == current and rollback.exists() and not staged.exists():
            return {}
        return original_files(path)

    monkeypatch.setattr(recovery, "_files", invalid_after_swap)
    with pytest.raises(ValueError, match="Cutover data differs"):
        recovery.cutover(recovery_set, staged, current, rollback, VERSIONS, stopped_confirmed=True)
    assert (current / "prior.txt").read_text(encoding="utf-8") == "original"
    assert staged.exists() and not rollback.exists()


def test_corrupt_staging_cannot_replace_current_data(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    recovery_set = tmp_path / "recovery-set"
    recovery.create(source, recovery_set, VERSIONS)
    staged = tmp_path / "staged-data"
    recovery.stage_restore(recovery_set, staged, VERSIONS)
    (staged / "library.db").write_bytes(b"corrupt")
    current = _data(tmp_path / "current-data")
    rollback = tmp_path / "previous-data"
    with pytest.raises(ValueError, match="Staged data differs"):
        recovery.cutover(recovery_set, staged, current, rollback, VERSIONS, stopped_confirmed=True)
    assert current.exists() and not rollback.exists()


def test_corrupt_partial_or_incomplete_set_cannot_stage(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    recovery_set = tmp_path / "recovery-set"
    recovery.create(source, recovery_set, VERSIONS)
    (recovery_set / "data" / "library.db").write_bytes(b"broken")
    with pytest.raises(ValueError, match="hashes or membership"):
        recovery.stage_restore(recovery_set, tmp_path / "staged-data", VERSIONS)
    assert not (tmp_path / "staged-data").exists()

    (recovery_set / recovery.COMPLETION).unlink()
    with pytest.raises(ValueError, match="incomplete"):
        recovery.verify(recovery_set)


def test_missing_required_data_and_bad_database_never_publish(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    (source / "auth.db").unlink()
    with pytest.raises(ValueError, match="missing"):
        recovery.create(source, tmp_path / "missing-set", VERSIONS)
    assert not (tmp_path / "missing-set").exists()
    (source / "auth.db").write_bytes(b"not sqlite")
    with pytest.raises(ValueError, match="cannot be opened"):
        recovery.create(source, tmp_path / "corrupt-set", VERSIONS)
    assert not (tmp_path / "corrupt-set").exists()


def test_older_archive_is_verifiable_but_not_staged_without_review(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    with closing(sqlite3.connect(source / recovery.ARCHIVE)) as db:
        db.execute("PRAGMA user_version=10")
        db.commit()
    recovery_set = tmp_path / "older-set"
    recovery.create(source, recovery_set, VERSIONS)
    assert recovery.verify(recovery_set, VERSIONS)["archive"]["schema_version"] == 10
    with pytest.raises(ValueError, match="migration review"):
        recovery.stage_restore(recovery_set, tmp_path / "staged-data", VERSIONS)
    assert not (tmp_path / "staged-data").exists()


def test_changed_source_during_copy_never_publishes(tmp_path: Path, monkeypatch) -> None:
    source = _data(tmp_path / "quiesced-data")
    original = recovery._files
    calls = 0

    def changing_files(root):
        nonlocal calls
        calls += 1
        result = original(root)
        if calls == 3:
            result["settings.json"] = {**result["settings.json"], "size": 999}
        return result

    monkeypatch.setattr(recovery, "_files", changing_files)
    destination = tmp_path / "changed-set"
    with pytest.raises(ValueError, match="source changed"):
        recovery.create(source, destination, VERSIONS)
    assert not destination.exists()
    assert not list(tmp_path.glob(".changed-set.*.tmp"))


def test_malformed_settings_never_publish(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    (source / "settings.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError, match="settings cannot be parsed"):
        recovery.create(source, tmp_path / "bad-settings-set", VERSIONS)
    assert not (tmp_path / "bad-settings-set").exists()


@pytest.mark.parametrize("missing", ["server_id", "encryption_key"])
def test_missing_credential_identity_never_publishes(tmp_path: Path, missing: str) -> None:
    source = _data(tmp_path / "quiesced-data")
    settings_path = source / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    del settings[missing]
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        recovery.create(source, tmp_path / "invalid-set", VERSIONS)
    assert not (tmp_path / "invalid-set").exists()


def test_invalid_encryption_key_never_publishes(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    settings_path = source / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["encryption_key"] = "not-a-fernet-key"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    with pytest.raises(ValueError, match="key is invalid"):
        recovery.create(source, tmp_path / "invalid-set", VERSIONS)
    assert not (tmp_path / "invalid-set").exists()


@pytest.mark.parametrize("name", ["library.db", "auth.db"])
def test_missing_core_tables_never_publish(tmp_path: Path, name: str) -> None:
    source = _data(tmp_path / "quiesced-data")
    with closing(sqlite3.connect(source / name)) as db:
        db.execute("DROP TABLE settings")
        db.commit()
    with pytest.raises(ValueError, match="missing required tables"):
        recovery.create(source, tmp_path / "invalid-set", VERSIONS)
    assert not (tmp_path / "invalid-set").exists()


def test_empty_replacement_library_never_publishes(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    with closing(sqlite3.connect(source / "library.db")) as db:
        db.execute("DELETE FROM settings WHERE key='version'")
        db.commit()
    with pytest.raises(ValueError, match="schema differs"):
        recovery.create(source, tmp_path / "invalid-set", VERSIONS)
    assert not (tmp_path / "invalid-set").exists()


def test_archive_semantic_corruption_never_publishes(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    with closing(sqlite3.connect(source / recovery.ARCHIVE)) as db:
        db.execute("UPDATE metadata SET value='wrong' WHERE key='schema_digest'")
        db.commit()
    with pytest.raises(ValueError, match="schema digest mismatch"):
        recovery.create(source, tmp_path / "bad-archive-set", VERSIONS)
    assert not (tmp_path / "bad-archive-set").exists()


def test_manifest_and_extra_file_tampering_are_rejected(tmp_path: Path) -> None:
    source = _data(tmp_path / "quiesced-data")
    recovery_set = tmp_path / "recovery-set"
    recovery.create(source, recovery_set, VERSIONS)
    (recovery_set / "data" / "unexpected.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="membership"):
        recovery.verify(recovery_set)
    (recovery_set / "data" / "unexpected.txt").unlink()
    manifest_path = recovery_set / recovery.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["versions"]["app"] = "forged"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="completion marker"):
        recovery.verify(recovery_set)
