"""Durable archive behavior, independent of Music Assistant and remote accounts."""

import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[1] / "music_assistant_lm/providers/library_enrichment/store.py"
spec = importlib.util.spec_from_file_location("enrichment_store", PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ArchiveStore = module.ArchiveStore


def subscribe(store, account="account-a", playlist="playlist-a", name="Same name"):
    return store.upsert_subscription("spotify", account, playlist, "spotify-instance", name)["id"]


def capture(store, subscription, snapshot="A", rows=None):
    rows = rows if rows is not None else [{"position": 0, "item": {"id": "song"}}]
    store.observe(subscription, snapshot)
    job = store.begin_capture(subscription, snapshot)
    return store.commit_capture(job, snapshot_before=snapshot, snapshot_after=snapshot, total=len(rows), occurrences=rows)


def test_identity_rename_accounts_and_restart(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    first = subscribe(store)
    second = subscribe(store, account="account-b")
    third = subscribe(store, playlist="playlist-b")
    version = capture(store, first)
    assert subscribe(store, name="Renamed") == first
    assert len({first, second, third}) == 3
    store_uuid = store.store_uuid
    store.close()
    reopened = ArchiveStore(path)
    assert reopened.store_uuid == store_uuid
    assert reopened.get_subscription(first)["name"] == "Renamed"
    assert reopened.get_version(version)["name"] == "Same name"
    assert reopened.get_subscription(first)["committed_version_id"] == version
    reopened.close()


def test_faithful_occurrences_and_empty_playlist(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    sub = subscribe(store)
    rows = [
        {"position": 0, "item": {"id": "repeat"}},
        {"position": 1, "item": None, "reason": "unavailable"},
        {"position": 2, "item": {"id": "repeat"}},
        {"position": 3, "item": {"is_local": True}, "reason": "local"},
        {"position": 4, "item": {"type": "episode"}, "reason": "unsupported"},
    ]
    old = capture(store, sub, rows=rows)
    assert store.get_version(old)["occurrences"] == rows
    empty = capture(store, sub, snapshot="B", rows=[])
    assert store.get_version(empty)["occurrences"] == []
    assert store.get_version(old)["occurrences"] == rows
    store.close()


@pytest.mark.parametrize(
    "after,total,rows",
    [
        ("C", 1, [{"position": 0}]),
        ("B", 2, [{"position": 0}]),
        ("B", 2, [{"position": 0}, {"position": 0}]),
        ("B", 1, [{"position": 1}]),
    ],
)
def test_failed_capture_never_advances_commit(tmp_path, after, total, rows):
    store = ArchiveStore(tmp_path / "archive.db")
    sub = subscribe(store)
    old = capture(store, sub)
    store.observe(sub, "B")
    job = store.begin_capture(sub, "B")
    with pytest.raises(ValueError):
        store.commit_capture(job, snapshot_before="B", snapshot_after=after, total=total, occurrences=rows)
    state = store.get_subscription(sub)
    assert state["observed_snapshot"] == state["attempted_snapshot"] == "B"
    assert state["committed_snapshot"] == "A"
    assert state["committed_version_id"] == old
    store.fail_capture(job, "validation failed")
    assert store.list_jobs()[-1]["state"] == "failed"
    store.close()


def test_database_write_failure_rolls_back_version_and_job(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    sub = subscribe(store)
    old = capture(store, sub)
    job = store.begin_capture(sub, "B")
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TRIGGER reject_occurrence BEFORE INSERT ON occurrences BEGIN SELECT RAISE(ABORT,'injected storage failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.commit_capture(job, snapshot_before="B", snapshot_after="B", total=1, occurrences=[{"position": 0}])
    assert store.get_subscription(sub)["committed_version_id"] == old
    assert store.list_jobs()[-1]["state"] == "pending"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM versions").fetchone()[0] == 1
    store.close()


def test_restart_explicit_recovery_and_single_worker(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    sub = subscribe(store)
    store.begin_capture(sub, "A")
    with pytest.raises(sqlite3.IntegrityError):
        store.begin_capture(sub, "B")
    store.close()
    store = ArchiveStore(path)
    assert store.list_jobs()[0]["state"] == "pending"
    assert store.recover_pending() == 1
    assert store.recover_pending() == 0
    capture(store, sub, "B")
    store.close()


def test_unknown_schema_fails_without_reset(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    subscribe(store)
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Unsupported"):
        ArchiveStore(path)
    assert path.read_bytes() == before


def test_backup_destination_verified_and_reopenable(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    sub = subscribe(store)
    version = capture(store, sub)
    dest = tmp_path / "backup.db"
    manifest = store.backup(dest)
    assert manifest["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert dest.with_suffix(".db.manifest.json").is_file()
    restored = ArchiveStore(dest)
    assert restored.store_uuid == store.store_uuid
    assert restored.get_version(version) == store.get_version(version)
    restored.close()
    with pytest.raises(FileExistsError):
        store.backup(dest)
    store.close()


def test_progress_survives_restart(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    job = store.begin_capture(subscribe(store), "A")
    store.update_progress(job, 25, 100)
    with pytest.raises(ValueError):
        store.update_progress(job, 10, 100)
    store.close()
    store = ArchiveStore(path)
    assert store.list_jobs()[0]["received"] == 25
    assert store.list_jobs()[0]["total"] == 100
    store.close()


def test_content_corruption_rejected_on_read_and_backup(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    version = capture(store, subscribe(store))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE occurrences SET payload='{}'")
    with pytest.raises(ValueError, match="digest"):
        store.get_version(version)
    dest = tmp_path / "bad.db"
    with pytest.raises(ValueError, match="digest"):
        store.backup(dest)
    assert not dest.exists()
    assert not dest.with_suffix(".db.manifest.json").exists()
    store.close()


def test_structural_schema_change_fails_closed(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE jobs ADD COLUMN unexpected TEXT")
    with pytest.raises(ValueError, match="schema digest"):
        ArchiveStore(path)
