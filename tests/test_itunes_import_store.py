"""Persistence boundaries for staged iTunes XML imports."""

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "music_assistant_lm/providers/library_enrichment/store.py"
spec = importlib.util.spec_from_file_location("itunes_enrichment_store", PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ArchiveStore = module.ArchiveStore


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def stage(store: ArchiveStore, source: str = "library-a") -> dict:
    return store.stage_itunes_import(
        digest(source),
        "67A9C3B9B57F556D",
        {"application_version": "11.0.3", "track_count": 16905, "playlist_count": 102},
        [{"source": "G:\\Music", "destination": "/media/music"}],
        ["PLAYLIST-A", "PLAYLIST-B"],
    )


def test_staged_import_is_idempotent_restart_safe_and_cas_guarded(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    first = stage(store)
    repeated = stage(store)
    assert repeated["inspection_id"] == first["inspection_id"]
    assert repeated["status"] == "staged" and repeated["revision"] == 0
    assert repeated["source_digest"] == digest("library-a")
    assert repeated["path_mappings"][0]["source"] == "G:\\Music"
    assert repeated["playlist_ids"] == ["PLAYLIST-A", "PLAYLIST-B"]
    store.close()

    store = ArchiveStore(path)
    preview_digest = digest("preview-a")
    previewed = store.record_itunes_import_preview(
        first["inspection_id"], 0, preview_digest,
        {"matched_tracks": 16000, "unresolved_tracks": 905, "selected_playlists": 2},
    )
    assert previewed["status"] == "previewed"
    assert previewed["revision"] == 1 and previewed["preview_revision"] == 1
    assert previewed["preview"]["unresolved_tracks"] == 905
    with pytest.raises(ValueError, match="revision conflict"):
        store.record_itunes_import_preview(first["inspection_id"], 0, digest("stale"), {})
    with pytest.raises(ValueError, match="preview does not match"):
        store.commit_itunes_import(first["inspection_id"], 1, digest("other"), {})

    committed = store.commit_itunes_import(
        first["inspection_id"], 1, preview_digest,
        {"created_playlists": 2, "imported_tracks": 16000, "unresolved_tracks": 905},
    )
    assert committed["status"] == "committed" and committed["revision"] == 2
    # A transport retry is idempotent even though it carries the pre-commit revision.
    assert store.commit_itunes_import(
        first["inspection_id"], 1, preview_digest,
        {"created_playlists": 2, "imported_tracks": 16000, "unresolved_tracks": 905},
    )["revision"] == 2
    with pytest.raises(ValueError, match="already committed"):
        store.commit_itunes_import(first["inspection_id"], 2, preview_digest, {"created_playlists": 3})
    store.close()


def test_source_identity_and_bounded_payload_validation_are_atomic(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    staged = stage(store)
    with pytest.raises(ValueError, match="metadata conflict"):
        store.stage_itunes_import(
            digest("library-a"), "DIFFERENT", {"track_count": 1}, [], []
        )
    assert store.get_itunes_import(staged["inspection_id"])["library_persistent_id"] == "67A9C3B9B57F556D"
    assert store._db.execute("SELECT count(*) FROM itunes_import_documents").fetchone()[0] == 1
    assert store._db.execute("SELECT count(*) FROM itunes_import_batches").fetchone()[0] == 1

    with pytest.raises(ValueError, match="SHA-256"):
        store.stage_itunes_import("not-a-digest", "LIBRARY", {}, [], [])
    with pytest.raises(ValueError, match="exceeds"):
        store.stage_itunes_import(digest("large"), "LIBRARY", {"value": "x" * 70000}, [], [])
    assert store._db.execute("SELECT count(*) FROM itunes_import_documents").fetchone()[0] == 1
    store.close()


def test_v6_migration_is_transactional_and_backup_preserves_import(tmp_path):
    path = tmp_path / "legacy-v6.db"
    store = ArchiveStore(path)
    with store._transaction():
        store._db.execute("DROP TABLE itunes_import_batches")
        store._db.execute("DROP TABLE itunes_import_documents")
        store._db.execute("DELETE FROM metadata WHERE key='schema_v7_migrated_at'")
        store._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (store._schema_digest(),))
        store._db.execute("PRAGMA user_version=6")
    identity = store.store_uuid
    store.close()

    class BrokenMigration(ArchiveStore):
        def _migrate_v7(self):
            super()._migrate_v7()
            raise RuntimeError("migration interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        BrokenMigration(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT name FROM sqlite_master WHERE name='itunes_import_documents'").fetchone() is None

    restored = ArchiveStore(path)
    assert restored.store_uuid == identity
    imported = stage(restored)
    backup = tmp_path / "backup-v7.db"
    manifest = restored.backup(backup)
    assert manifest["schema_version"] == 7
    reopened = ArchiveStore(backup)
    assert reopened.get_itunes_import(imported["inspection_id"])["playlist_ids"] == ["PLAYLIST-A", "PLAYLIST-B"]
    reopened.close()
    restored.close()
