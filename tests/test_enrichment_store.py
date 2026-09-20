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


def test_playback_policy_cas_and_projection_checkpoint_are_independent(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    subscription = subscribe(store)
    version = capture(store, subscription)
    policy = store.get_playback_policy(subscription)
    assert policy["mode"] == "prefer_spotify" and policy["revision"] == 0
    policy = store.set_playback_policy(subscription, "local_only", 0, "admin")
    assert policy["revision"] == 1
    with pytest.raises(ValueError, match="revision conflict"):
        store.set_playback_policy(subscription, "prefer_local", 0)
    projection = store.prepare_playback_projection(
        subscription, version, 1, "a" * 64, 1, 0,
        '[{"position":0,"reason":"missing","omitted":true,"fallback":null}]',
    )
    assert projection["state"] == "prepared"
    store.mark_playback_projection_writing(subscription)
    committed = store.commit_playback_projection(subscription, "playback", "builtin", "a" * 64)
    assert committed["state"] == "applied"
    # The playback checkpoint does not advance the older archive-copy checkpoint.
    assert store.get_subscription(subscription)["applied_version_id"] is None
    store.close()


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


def test_v1_upgrade_preserves_capture_and_rolls_back_failed_migration(tmp_path):
    path = tmp_path / "legacy.db"
    old = ArchiveStore.__new__(ArchiveStore)
    old._db = sqlite3.connect(path)
    old._initialize()
    sub, version = "sub", "version"
    old._db.execute(
        "INSERT INTO subscriptions(id,provider_domain,account_id,source_playlist_id,provider_instance_id,name) VALUES (?,?,?,?,?,?)",
        (sub, "spotify", "account", "playlist", "instance", "name"),
    )
    payload = '{"position": 0}'
    old._db.execute(
        "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)",
        (version, sub, "A", module._now(), 1, "instance", "name", module._digest([payload])),
    )
    old._db.execute("INSERT INTO occurrences VALUES (?,?,?)", (version, 0, payload))
    identity = old._db.execute("SELECT value FROM metadata WHERE key='store_uuid'").fetchone()[0]
    old._db.commit()
    old._db.close()

    class BrokenMigration(ArchiveStore):
        def _migrate_v2(self):
            super()._migrate_v2()
            raise RuntimeError("injected migration failure")

    with pytest.raises(RuntimeError):
        BrokenMigration(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='apply_jobs'").fetchone() is None
        assert "applied_version_id" not in [row[1] for row in db.execute("PRAGMA table_info(subscriptions)")]
    upgraded = ArchiveStore(path)
    assert upgraded.store_uuid == identity
    assert upgraded.get_version(version)["total"] == 1
    assert upgraded.get_subscription(sub)["applied_version_id"] is None
    assert upgraded.list_applies() == []
    upgraded.close()
    reopened = ArchiveStore(path)
    assert reopened.store_uuid == identity
    reopened.close()


def prepare(store, version, digest="a" * 64):
    return store.prepare_apply(version, digest, 1, 1, "[]", "marker-" + version, "Visible archive")


def test_apply_intent_idempotency_and_checkpoint_independence(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    sub = subscribe(store)
    first = capture(store, sub, "A")
    job = prepare(store, first)
    assert store.prepare_apply(first, "a" * 64, 1, 1, "[]", "new-marker", "New name")["id"] == job["id"]
    with pytest.raises(ValueError, match="Conflicting"):
        prepare(store, first, "b" * 64)
    with pytest.raises(ValueError):
        store.commit_apply(job["id"], "destination", "builtin", "a" * 64)
    store.mark_apply_creating(job["id"])
    store.commit_apply(job["id"], "destination", "builtin", "a" * 64)
    second = capture(store, sub, "B")
    store.commit_apply(job["id"], "destination", "builtin", "a" * 64)
    state = store.get_subscription(sub)
    assert state["committed_version_id"] == second
    assert state["applied_version_id"] == first
    assert state["applied_job_id"] == job["id"]
    with pytest.raises(ValueError):
        store.commit_apply(job["id"], "different", "builtin", "a" * 64)
    with pytest.raises(ValueError):
        store.fail_apply(job["id"], "late failure")
    store.close()


def test_uncertain_creation_cannot_blind_retry_but_can_reconcile(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    job = prepare(store, capture(store, subscribe(store)))
    store.mark_apply_creating(job["id"])
    store.close()
    store = ArchiveStore(path)
    assert store.get_apply(job["id"])["state"] == "creating"
    with pytest.raises(ValueError):
        store.mark_apply_creating(job["id"])
    store.fail_apply(job["id"], "lost response")
    assert store.get_apply(job["id"])["state"] == "uncertain"
    with pytest.raises(ValueError):
        store.commit_apply(job["id"], "destination", "builtin", "b" * 64)
    store.commit_apply(job["id"], "destination", "builtin", "a" * 64)
    assert store.get_apply_for_version(job["version_id"])["state"] == "applied"
    store.close()


def test_apply_validation_and_conflict_state(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    version = capture(store, subscribe(store))
    for total, projected, omissions in [(2, 2, "[]"), (1, 0, "[]"), (1, 2, "[]")]:
        with pytest.raises(ValueError):
            store.prepare_apply(version, "a" * 64, total, projected, omissions, "marker", "Name")
    job = prepare(store, version)
    store.fail_apply(job["id"], "before creation")
    assert store.get_apply(job["id"])["state"] == "failed"
    store.mark_apply_creating(job["id"])
    store.conflict_apply(job["id"], "multiple marker matches")
    with pytest.raises(ValueError):
        store.commit_apply(job["id"], "destination", "builtin", "a" * 64)
    assert len(store.list_applies()) == 1
    store.close()


def test_v2_sync_migration_rollback_preserves_applied_capture(tmp_path):
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    sub = subscribe(store)
    version = capture(store, sub)
    job = prepare(store, version)
    store.mark_apply_creating(job["id"])
    store.commit_apply(job["id"], "destination", "builtin", "a" * 64)
    with store._transaction():
        for table in ("playback_projections", "playback_policies"):
            store._db.execute(f"DROP TABLE {table}")
        store._db.execute("DELETE FROM metadata WHERE key='schema_v5_migrated_at'")
        for table in ("match_decisions", "match_candidates", "match_sources", "local_asset_locations", "local_assets"):
            store._db.execute(f"DROP TABLE {table}")
        store._db.execute("DELETE FROM metadata WHERE key='schema_v4_migrated_at'")
        for table in ("sync_jobs", "subscription_sync_state", "subscription_sync_policy"):
            store._db.execute(f"DROP TABLE {table}")
        store._db.execute("DELETE FROM metadata WHERE key='schema_v3_migrated_at'")
        store._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (store._schema_digest(),))
        store._db.execute("PRAGMA user_version=2")
    store.close()

    class BrokenMigration(ArchiveStore):
        def _migrate_v3(self):
            super()._migrate_v3()
            raise RuntimeError("migration interrupted")

    with pytest.raises(RuntimeError):
        BrokenMigration(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT name FROM sqlite_master WHERE name='sync_jobs'").fetchone() is None
    store = ArchiveStore(path)
    assert store.get_sync_policy(sub)["mode"] == "manual"
    assert store.get_subscription(sub)["applied_version_id"] == version
    assert store.get_apply(job["id"])["state"] == "applied"
    assert store.get_sync_status(sub)["state"]["next_check_at"] is None
    store.close()


def test_sync_policy_revision_and_due_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_now", lambda: "2026-09-20T00:00:00+00:00")
    store = ArchiveStore(tmp_path / "archive.db")
    sub = subscribe(store)
    assert store.get_sync_policy(sub)["revision"] == 0
    updated = store.set_sync_policy(sub, "scheduled", 3600, "admin", expected_revision=0)
    assert updated["revision"] == 1
    assert store.list_due("2026-09-20T00:59:59+00:00") == []
    assert store.list_due("2026-09-20T01:00:00+00:00")[0]["id"] == sub
    with pytest.raises(ValueError):
        store.set_sync_policy(sub, "manual", expected_revision=0)
    store.set_sync_policy(sub, "manual", expected_revision=1)
    assert store.list_due("2026-09-21T00:00:00+00:00") == []
    assert store.get_sync_status(sub)["state"]["next_check_at"] is None
    for interval in (3599, 604801, True):
        with pytest.raises(ValueError):
            store.set_sync_policy(sub, "scheduled", interval, "admin", expected_revision=2)
    store.close()


def test_sync_capture_exclusion_and_owned_capture(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    sub = subscribe(store)
    run = store.begin_sync(sub)
    with pytest.raises(sqlite3.IntegrityError):
        store.begin_sync(sub)
    with pytest.raises(ValueError):
        store.begin_capture(sub, "A")
    store.mark_sync_running(run["id"])
    store.observe_sync(run["id"], "A")
    capture_id = store.begin_capture(sub, "A", sync_job_id=run["id"])
    version = store.commit_capture(capture_id, snapshot_before="A", snapshot_after="A", total=0, occurrences=[])
    store.succeed_sync(run["id"], version)
    assert store.get_sync_job(run["id"])["version_id"] == version
    capture_id = store.begin_capture(sub, "B")
    with pytest.raises(ValueError):
        store.begin_sync(sub)
    store.fail_capture(capture_id, "cancelled")
    store.close()


def test_sync_failure_access_pause_recovery_and_checkpoints(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_now", lambda: "2026-09-20T00:00:00+00:00")
    path = tmp_path / "archive.db"
    store = ArchiveStore(path)
    sub = subscribe(store)
    version = capture(store, sub)
    store.set_sync_policy(sub, "scheduled", 3600, "admin", expected_revision=0)
    run = store.begin_sync(sub, "scheduled")
    store.mark_sync_running(run["id"])
    store.observe_sync(run["id"], "B")
    store.fail_sync(run["id"], "denied", "Access unavailable", "access_denied")
    state = store.get_sync_status(sub)["state"]
    assert state["next_check_at"] is None and state["consecutive_failures"] == 1
    assert store.get_subscription(sub)["committed_version_id"] == version
    assert store.get_subscription(sub)["observed_snapshot"] == "B"
    retry = store.begin_sync(sub)
    store.mark_sync_running(retry["id"])
    store.observe_sync(retry["id"], "A")
    store.succeed_sync(retry["id"], version)
    state = store.get_sync_status(sub)["state"]
    assert state["consecutive_failures"] == 0 and state["access_state"] == "accessible"
    assert state["next_check_at"] == "2026-09-20T01:00:00+00:00"
    interrupted = store.begin_sync(sub)
    store.close()
    store = ArchiveStore(path)
    assert store.recover_sync_pending() == 1
    assert store.recover_sync_pending() == 0
    assert store.get_sync_job(interrupted["id"])["state"] == "interrupted"
    assert store.get_subscription(sub)["committed_version_id"] == version
    assert store.get_subscription(sub)["applied_version_id"] is None
    store.close()


def test_match_assets_candidates_decisions_and_version_overlay(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    sub = subscribe(store)
    version = capture(
        store,
        sub,
        rows=[
            {"position": 0, "state": "track", "source_item_id": "spotify-track"},
            {"position": 1, "state": "unavailable", "source_item_id": None},
            {"position": 2, "state": "track", "source_item_id": "spotify-track"},
        ],
    )
    first = store.upsert_local_asset(
        "track", "filesystem", "music/song.flac", {"name": "Song"}, {"origin": "library_mapping"}
    )
    assert store.upsert_local_asset("track", "filesystem", "music/song.flac", {"name": "Renamed"})["id"] == first["id"]
    location = store.add_local_asset_location(first["id"], "builtin", "42", {"library": True})
    assert location["asset_id"] == first["id"]
    with pytest.raises(ValueError, match="another asset"):
        second = store.upsert_local_asset("track", "filesystem", "other.flac")
        store.add_local_asset_location(second["id"], "builtin", "42")

    overlay = store.replace_match_candidates(
        "spotify",
        "account-a",
        "track",
        "spotify-track",
        [{"asset_id": first["id"], "score": 0.97, "evidence": {"isrc": "same"}}],
        "exact-isrc-v1",
    )
    candidate_id = overlay["candidates"][0]["id"]
    assert overlay["revision"] == 0
    assert overlay["candidates"][0]["asset"]["locations"][1]["provider_instance_id"] == "filesystem"
    rejected = store.set_match_decision(
        "spotify", "account-a", "track", "spotify-track", "reject", first["id"], 0, "admin", {"reason": "wrong edition"},
        "exact-isrc-v1",
    )
    assert rejected["revision"] == 1 and rejected["candidates"][0]["rejected"] is True
    assert rejected["decision"]["evidence"] == {"reason": "wrong edition"}

    # Removing and rediscovering a pair never erases its rejection history.
    assert store.replace_match_candidates("spotify", "account-a", "track", "spotify-track", [], "metadata-v2")[
        "candidates"
    ] == []
    refreshed = store.replace_match_candidates(
        "spotify", "account-a", "track", "spotify-track",
        [{"asset_id": first["id"], "score": 0.91, "evidence": {"title": "same"}}], "metadata-v2",
    )
    assert refreshed["candidates"][0]["id"] == candidate_id
    assert refreshed["candidates"][0]["rejected"] is True
    approved = store.set_match_decision(
        "spotify", "account-a", "track", "spotify-track", "approve", first["id"], 1, "admin"
    )
    assert approved["approved_asset_id"] == first["id"]
    assert approved["candidates"][0]["approved"] is True and approved["candidates"][0]["rejected"] is False
    with pytest.raises(ValueError, match="revision conflict"):
        store.clear_match_decision("spotify", "account-a", "track", "spotify-track", 1)
    cleared = store.clear_match_decision("spotify", "account-a", "track", "spotify-track", 2, "admin")
    assert cleared["revision"] == 3 and cleared["approved_asset_id"] is None
    assert cleared["candidates"][0]["rejected"] is False
    assert cleared["decision"]["action"] == "clear"
    assert [entry["action"] for entry in cleared["decision_history"]] == ["reject", "approve", "clear"]

    version_overlay = store.get_version_match_overlay(version)
    assert version_overlay["content_digest"] == store.get_version(version)["content_digest"]
    assert version_overlay["occurrences"][0]["match"]["revision"] == 3
    assert version_overlay["occurrences"][1]["match"] is None
    assert version_overlay["occurrences"][2]["match"]["source"]["source_item_id"] == "spotify-track"
    assert len(store.list_match_overlays("spotify", "account-a", "track")) == 1
    store.close()


def test_match_candidate_validation_and_cas_are_atomic(tmp_path):
    store = ArchiveStore(tmp_path / "archive.db")
    asset = store.upsert_local_asset("track", "filesystem", "song.flac")
    key = ("spotify", "account", "track", "source")
    for candidates in (
        [{"asset_id": asset["id"], "score": True}],
        [{"asset_id": asset["id"], "score": 1.1}],
        [{"asset_id": asset["id"], "score": 0.5}, {"asset_id": asset["id"], "score": 0.4}],
    ):
        with pytest.raises(ValueError):
            store.replace_match_candidates(*key, candidates, "v1")
    overlay = store.replace_match_candidates(*key, [{"asset_id": asset["id"], "score": 1, "evidence": {}}], "v1")
    with pytest.raises(ValueError, match="current candidate"):
        other = store.upsert_local_asset("track", "filesystem", "other.flac")
        store.set_match_decision(*key, "approve", other["id"], 0)
    assert store.get_match_overlay(*key)["revision"] == overlay["revision"] == 0
    store.close()


def test_v3_match_migration_is_transactional_and_backup_preserves_overlay(tmp_path):
    path = tmp_path / "legacy-v3.db"
    store = ArchiveStore(path)
    sub = subscribe(store)
    version = capture(store, sub)
    with store._transaction():
        for table in ("playback_projections", "playback_policies"):
            store._db.execute(f"DROP TABLE {table}")
        store._db.execute("DELETE FROM metadata WHERE key='schema_v5_migrated_at'")
        for table in ("match_decisions", "match_candidates", "match_sources", "local_asset_locations", "local_assets"):
            store._db.execute(f"DROP TABLE {table}")
        store._db.execute("DELETE FROM metadata WHERE key='schema_v4_migrated_at'")
        store._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (store._schema_digest(),))
        store._db.execute("PRAGMA user_version=3")
    identity = store.store_uuid
    store.close()

    class BrokenMigration(ArchiveStore):
        def _migrate_v4(self):
            super()._migrate_v4()
            raise RuntimeError("migration interrupted")

    with pytest.raises(RuntimeError):
        BrokenMigration(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT name FROM sqlite_master WHERE name='match_sources'").fetchone() is None

    store = ArchiveStore(path)
    assert store.store_uuid == identity and store.get_version(version)["total"] == 1
    asset = store.upsert_local_asset("track", "filesystem", "song.flac")
    store.replace_match_candidates(
        "spotify", "account-a", "track", "song", [{"asset_id": asset["id"], "score": 1}], "v1"
    )
    store.set_match_decision("spotify", "account-a", "track", "song", "approve", asset["id"], 0)
    destination = tmp_path / "backup-v4.db"
    store.backup(destination)
    restored = ArchiveStore(destination)
    assert restored.get_match_overlay("spotify", "account-a", "track", "song")["approved_asset_id"] == asset["id"]
    restored.close()


def test_v4_playback_policy_migration_preserves_match_overlay(tmp_path):
    path = tmp_path / "legacy-v4.db"
    store = ArchiveStore(path)
    sub = subscribe(store)
    version = capture(store, sub, rows=[{"position": 0, "state": "track", "source_item_id": "song"}])
    asset = store.upsert_local_asset("track", "local", "song.flac")
    store.replace_match_candidates(
        "spotify", "account-a", "track", "song", [{"asset_id": asset["id"], "score": 1, "evidence": {}}], "v1"
    )
    store.set_match_decision("spotify", "account-a", "track", "song", "approve", asset["id"], 0)
    with store._transaction():
        for table in ("playback_projections", "playback_policies"):
            store._db.execute(f"DROP TABLE {table}")
        store._db.execute("DELETE FROM metadata WHERE key='schema_v5_migrated_at'")
        store._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (store._schema_digest(),))
        store._db.execute("PRAGMA user_version=4")
    store.close()

    restored = ArchiveStore(path)
    assert restored.get_playback_policy(sub)["mode"] == "prefer_spotify"
    assert restored.get_version_match_overlay(version)["occurrences"][0]["match"]["approved_asset_id"] == asset["id"]
    restored.close()
    store.close()
