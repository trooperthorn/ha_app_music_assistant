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


def test_version_history_is_bounded_stable_and_account_scoped(tmp_path, monkeypatch):
    store = ArchiveStore(tmp_path / "archive.db")
    sub = subscribe(store)
    other = subscribe(store, account="other")
    monkeypatch.setattr(module, "_now", lambda: "2026-09-20T00:00:00+00:00")
    ids = [capture(store, sub, snapshot=str(index)) for index in range(3)]
    capture(store, other)
    rows = store.list_versions(sub)
    assert [row["id"] for row in rows] == sorted(ids, reverse=True)
    assert store.list_versions(sub, limit=1, offset=1) == rows[1:2]
    assert store.list_versions(sub, offset=100) == []
    assert all("occurrences" not in row and row["content_digest"] and row["total"] == 1 for row in rows)
    with pytest.raises(KeyError):
        store.list_versions("unknown")
    for limit, offset in [(0, 0), (201, 0), (True, 0), (1, -1), (1, False), (1, 2**31)]:
        with pytest.raises(ValueError):
            store.list_versions(sub, limit, offset)
    store.close()


@pytest.mark.parametrize("failure", ["serialization", "publication"])
def test_backup_manifest_failure_leaves_no_published_recovery_set(tmp_path, monkeypatch, failure):
    store = ArchiveStore(tmp_path / "archive.db")
    capture(store, subscribe(store))
    destination = tmp_path / "backup.db"

    def broken_dump(value, handle, **kwargs):
        handle.write("{partial")
        raise OSError("injected manifest serialization failure")

    def broken_link(*args):
        raise OSError("injected manifest publication failure")

    if failure == "serialization":
        monkeypatch.setattr(module.json, "dump", broken_dump)
    else:
        monkeypatch.setattr(module.os, "link", broken_link)
    with pytest.raises(OSError):
        store.backup(destination)
    assert not destination.exists()
    assert not destination.with_suffix(".db.manifest.json").exists()
    assert list(tmp_path.glob(".*.manifest.tmp")) == []
    store.close()


def test_backup_refuses_orphan_manifest_without_changing_it(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    destination = tmp_path / "backup.db"
    manifest = destination.with_suffix(".db.manifest.json")
    manifest.write_text("existing evidence", encoding="utf-8")
    with pytest.raises(FileExistsError):
        store.backup(destination)
    assert manifest.read_text(encoding="utf-8") == "existing evidence"
    assert not destination.exists()
    store.close()


def test_backup_rejects_schema_changed_after_open(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE jobs ADD COLUMN unexpected TEXT")
    destination = tmp_path / "backup.db"
    with pytest.raises(ValueError, match="schema"):
        store.backup(destination)
    assert not destination.exists()
    store.close()
