"""Independent, durable source archive. Never writes Music Assistant core tables.

Methods are synchronous and serialized; async callers should use ``asyncio.to_thread``.
Capture payloads must already be filtered to exclude credentials by the adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(payloads: list[str]) -> str:
    return hashlib.sha256(json.dumps(payloads, ensure_ascii=False).encode()).hexdigest()


class ArchiveStore:
    """A single explicitly located archive with fail-closed schema handling."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=10000")
        try:
            with self._transaction():
                version = self._db.execute("PRAGMA user_version").fetchone()[0]
                tables = self._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                if version == 0 and not tables:
                    self._initialize()
                elif version != SCHEMA_VERSION:
                    raise ValueError(f"Unsupported enrichment schema {version}; database untouched")
                required = {"metadata", "subscriptions", "jobs", "versions", "occurrences"}
                actual = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if actual != required:
                    raise ValueError("Unexpected enrichment database tables; database untouched")
                identity = self._db.execute("SELECT value FROM metadata WHERE key='store_uuid'").fetchone()
                if not identity:
                    raise ValueError("Enrichment store identity is missing")
                self.store_uuid = identity[0]
                uuid.UUID(self.store_uuid)
                schema_digest = self._db.execute("SELECT value FROM metadata WHERE key='schema_digest'").fetchone()
                if not schema_digest or schema_digest[0] != self._schema_digest():
                    raise ValueError("Enrichment schema digest mismatch")
                if self._db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Enrichment database contains broken foreign keys")
        except Exception:
            self._db.close()
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def _initialize(self) -> None:
        statements = [
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            """CREATE TABLE subscriptions (
                id TEXT PRIMARY KEY, provider_domain TEXT NOT NULL, account_id TEXT NOT NULL,
                source_playlist_id TEXT NOT NULL, provider_instance_id TEXT NOT NULL,
                name TEXT NOT NULL, observed_snapshot TEXT, observed_at TEXT,
                attempted_snapshot TEXT, attempted_at TEXT, committed_snapshot TEXT,
                committed_at TEXT, committed_version_id TEXT REFERENCES versions(id),
                UNIQUE(provider_domain, account_id, source_playlist_id))""",
            """CREATE TABLE jobs (id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
                snapshot_id TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending','committed','failed')),
                created_at TEXT NOT NULL, finished_at TEXT, error TEXT,
                received INTEGER NOT NULL DEFAULT 0, total INTEGER,
                version_id TEXT REFERENCES versions(id))""",
            """CREATE TABLE versions (id TEXT PRIMARY KEY,
                subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
                snapshot_id TEXT NOT NULL, created_at TEXT NOT NULL, total INTEGER NOT NULL,
                provider_instance_id TEXT NOT NULL, name TEXT NOT NULL, content_digest TEXT NOT NULL)""",
            """CREATE TABLE occurrences (version_id TEXT NOT NULL REFERENCES versions(id),
                position INTEGER NOT NULL CHECK(position>=0), payload TEXT NOT NULL,
                PRIMARY KEY(version_id, position))""",
            "CREATE UNIQUE INDEX one_pending_capture ON jobs(subscription_id) WHERE state='pending'",
        ]
        for statement in statements:
            self._db.execute(statement)
        self._db.execute("INSERT INTO metadata VALUES ('store_uuid',?)", (str(uuid.uuid4()),))
        self._db.execute("INSERT INTO metadata VALUES ('schema_created_at',?)", (_now(),))
        self._db.execute("INSERT INTO metadata VALUES ('schema_digest',?)", (self._schema_digest(),))
        self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def _schema_digest(self) -> str:
        return _digest([row[0] for row in self._db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")])

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def upsert_subscription(
        self, provider_domain: str, account_id: str, source_playlist_id: str, provider_instance_id: str, name: str
    ) -> dict:
        if not all(isinstance(v, str) and v.strip() for v in (provider_domain, account_id, source_playlist_id, provider_instance_id)):
            raise ValueError("Stable provider, account, playlist and instance identities are required")
        with self._transaction():
            self._db.execute(
                """INSERT INTO subscriptions
                (id,provider_domain,account_id,source_playlist_id,provider_instance_id,name)
                VALUES (?,?,?,?,?,?) ON CONFLICT(provider_domain,account_id,source_playlist_id)
                DO UPDATE SET provider_instance_id=excluded.provider_instance_id,name=excluded.name""",
                (str(uuid.uuid4()), provider_domain, account_id, source_playlist_id, provider_instance_id, name),
            )
            return dict(
                self._db.execute(
                    """SELECT * FROM subscriptions WHERE
                provider_domain=? AND account_id=? AND source_playlist_id=?""",
                    (provider_domain, account_id, source_playlist_id),
                ).fetchone()
            )

    def get_subscription(self, subscription_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM subscriptions WHERE id=?", (subscription_id,)).fetchone()
            if row is None:
                raise KeyError(subscription_id)
            return dict(row)

    def list_subscriptions(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM subscriptions ORDER BY id")]

    def observe(self, subscription_id: str, snapshot_id: str) -> None:
        if not snapshot_id:
            raise ValueError("Snapshot identity required")
        with self._transaction():
            self.get_subscription(subscription_id)
            self._db.execute(
                "UPDATE subscriptions SET observed_snapshot=?,observed_at=? WHERE id=?", (snapshot_id, _now(), subscription_id)
            )

    def begin_capture(self, subscription_id: str, snapshot_id: str) -> str:
        if not snapshot_id:
            raise ValueError("Snapshot identity required")
        with self._transaction():
            self.get_subscription(subscription_id)
            job_id, now = str(uuid.uuid4()), _now()
            self._db.execute(
                "INSERT INTO jobs(id,subscription_id,snapshot_id,state,created_at) VALUES (?,?,?,'pending',?)",
                (job_id, subscription_id, snapshot_id, now),
            )
            self._db.execute("UPDATE subscriptions SET attempted_snapshot=?,attempted_at=? WHERE id=?", (snapshot_id, now, subscription_id))
            return job_id

    def commit_capture(self, job_id: str, *, snapshot_before: str, snapshot_after: str, total: int, occurrences: list[dict]) -> str:
        """Commit a complete capture atomically; rejected input leaves the job pending."""
        if type(total) is not int or total < 0 or len(occurrences) != total:
            raise ValueError("Incomplete capture total")
        if any(type(row.get("position")) is not int or row["position"] != index for index, row in enumerate(occurrences)):
            raise ValueError("Capture positions must be contiguous and zero-based")
        payloads = [json.dumps(row, ensure_ascii=False, allow_nan=False) for row in occurrences]
        with self._transaction():
            job = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if not snapshot_before or snapshot_before != snapshot_after or snapshot_before != job["snapshot_id"]:
                raise ValueError("Source changed during capture")
            if job["state"] == "committed":
                return job["version_id"]
            if job["state"] != "pending":
                raise ValueError("Capture job is no longer pending")
            subscription = self.get_subscription(job["subscription_id"])
            version_id, now = str(uuid.uuid4()), _now()
            self._db.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)",
                (
                    version_id,
                    job["subscription_id"],
                    snapshot_before,
                    now,
                    total,
                    subscription["provider_instance_id"],
                    subscription["name"],
                    _digest(payloads),
                ),
            )
            self._db.executemany(
                "INSERT INTO occurrences VALUES (?,?,?)", [(version_id, index, payload) for index, payload in enumerate(payloads)]
            )
            self._db.execute(
                """UPDATE subscriptions SET committed_snapshot=?,committed_at=?,
                committed_version_id=? WHERE id=?""",
                (snapshot_before, now, version_id, job["subscription_id"]),
            )
            self._db.execute(
                "UPDATE jobs SET state='committed',finished_at=?,version_id=?,received=?,total=? WHERE id=?",
                (now, version_id, total, total, job_id),
            )
            return version_id

    def update_progress(self, job_id: str, received: int, total: int) -> None:
        if type(received) is not int or type(total) is not int or not 0 <= received <= total:
            raise ValueError("Invalid capture progress")
        with self._transaction():
            result = self._db.execute(
                "UPDATE jobs SET received=?,total=? WHERE id=? AND state='pending' AND received<=?", (received, total, job_id, received)
            )
            if result.rowcount != 1:
                raise ValueError("Capture progress cannot regress or update a finished job")

    def fail_capture(self, job_id: str, error: str) -> None:
        with self._transaction():
            cursor = self._db.execute(
                "UPDATE jobs SET state='failed',finished_at=?,error=? WHERE id=? AND state='pending'", (_now(), error, job_id)
            )
            if cursor.rowcount != 1:
                raise ValueError("Capture job is not pending")

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM jobs ORDER BY created_at,id")]

    def recover_pending(self) -> int:
        """At exclusive provider startup, interrupt unfinished work for explicit fresh retry."""
        with self._transaction():
            return self._db.execute(
                "UPDATE jobs SET state='failed',finished_at=?,error='interrupted; retry requires fresh capture' WHERE state='pending'",
                (_now(),),
            ).rowcount

    def get_version(self, version_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
            if row is None:
                raise KeyError(version_id)
            result = dict(row)
            payloads = [
                item[0] for item in self._db.execute("SELECT payload FROM occurrences WHERE version_id=? ORDER BY position", (version_id,))
            ]
            if len(payloads) != result["total"] or _digest(payloads) != result["content_digest"]:
                raise ValueError("Archive version content digest mismatch")
            result["occurrences"] = [json.loads(payload) for payload in payloads]
            return result

    def list_versions(self, subscription_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
        """Return bounded metadata newest-first, using ID to break timestamp ties."""
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 2**31 - 1:
            raise ValueError("Expected limit 1..200 and offset 0..2147483647")
        with self._lock:
            self.get_subscription(subscription_id)
            return [
                dict(row)
                for row in self._db.execute(
                    "SELECT * FROM versions WHERE subscription_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                    (subscription_id, limit, offset),
                )
            ]

    def backup(self, destination: str | Path) -> dict:
        """Create a new SQLite recovery image and verify its actual destination bytes.

        This covers only the extension database, not coordinated MA/application recovery.
        Existing destinations are refused. A failed backup is not published as a manifest.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        manifest_path = destination.with_suffix(destination.suffix + ".manifest.json")
        if manifest_path.exists():
            raise FileExistsError(manifest_path)
        temporary_manifest = destination.with_name(f".{destination.name}.{uuid.uuid4()}.manifest.tmp")
        with destination.open("xb"):
            pass
        try:
            with self._lock, closing(sqlite3.connect(destination)) as target:
                self._db.backup(target)
            # Reopen the completed destination independently; do not trust source-side checks.
            with closing(sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)) as target:
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Backup integrity verification failed")
                if target.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Backup foreign key verification failed")
                for version_id, total, digest in target.execute("SELECT id,total,content_digest FROM versions"):
                    payloads = [
                        row[0]
                        for row in target.execute("SELECT payload FROM occurrences WHERE version_id=? ORDER BY position", (version_id,))
                    ]
                    if len(payloads) != total or _digest(payloads) != digest:
                        raise ValueError("Backup version content digest mismatch")
                if target.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise ValueError("Backup schema version mismatch")
                metadata = dict(target.execute("SELECT key,value FROM metadata"))
                schema_digest = _digest(
                    [row[0] for row in target.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
                )
                if metadata.get("schema_digest") != schema_digest or metadata.get("store_uuid") != self.store_uuid:
                    raise ValueError("Backup schema or identity mismatch")
            manifest = {
                "store_uuid": self.store_uuid,
                "schema_version": SCHEMA_VERSION,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "size": destination.stat().st_size,
                "created_at": _now(),
            }
            with temporary_manifest.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            # Atomic, no-overwrite publication. Failure leaves no apparently valid manifest.
            os.link(temporary_manifest, manifest_path)
            return manifest
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            temporary_manifest.unlink(missing_ok=True)
