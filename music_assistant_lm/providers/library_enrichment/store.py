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
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 4
ACCESS_STATES = {"unknown", "accessible", "authentication_required", "access_denied", "temporarily_unavailable", "provider_offline"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(payloads: list[str]) -> str:
    return hashlib.sha256(json.dumps(payloads, ensure_ascii=False).encode()).hexdigest()


def _timestamp(value: str) -> str:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must include timezone")
    return parsed.astimezone(UTC).isoformat()


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
                    version = 1
                elif version not in (1, 2, 3, SCHEMA_VERSION):
                    raise ValueError(f"Unsupported enrichment schema {version}; database untouched")
                required = {"metadata", "subscriptions", "jobs", "versions", "occurrences"}
                if version >= 2:
                    required.add("apply_jobs")
                if version >= 3:
                    required.update(("subscription_sync_policy", "subscription_sync_state", "sync_jobs"))
                if version >= 4:
                    required.update(("match_sources", "local_assets", "local_asset_locations", "match_candidates", "match_decisions"))
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
                if version == 1:
                    self._migrate_v2()
                if version <= 2:
                    self._migrate_v3()
                if version <= 3:
                    self._migrate_v4()
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
        self._db.execute("PRAGMA user_version=1")

    def _migrate_v2(self) -> None:
        """Run only after validating v1; outer transaction rolls back all DDL on failure."""
        self._db.execute("""CREATE TABLE apply_jobs (
            id TEXT PRIMARY KEY, version_id TEXT NOT NULL UNIQUE REFERENCES versions(id),
            subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
            projection_digest TEXT NOT NULL, source_count INTEGER NOT NULL,
            projected_count INTEGER NOT NULL, omissions_json TEXT NOT NULL,
            marker TEXT NOT NULL UNIQUE, requested_name TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','creating','applied','failed','uncertain','conflict')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT,
            destination_item_id TEXT, destination_provider_instance TEXT, verified_digest TEXT)""")
        for column in (
            "applied_version_id TEXT REFERENCES versions(id)",
            "applied_at TEXT",
            "applied_job_id TEXT REFERENCES apply_jobs(id)",
        ):
            self._db.execute(f"ALTER TABLE subscriptions ADD COLUMN {column}")
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT INTO metadata VALUES ('schema_v2_migrated_at',?)", (_now(),))
        self._db.execute("PRAGMA user_version=2")

    def _schema_digest(self) -> str:
        return _digest([row[0] for row in self._db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")])

    def _migrate_v3(self) -> None:
        self._db.execute("""CREATE TABLE subscription_sync_policy (
            subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id),
            mode TEXT NOT NULL DEFAULT 'manual' CHECK(mode IN ('manual','scheduled')),
            interval_seconds INTEGER NOT NULL DEFAULT 86400 CHECK(interval_seconds BETWEEN 3600 AND 604800),
            initiating_user_id TEXT, revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)""")
        self._db.execute("""CREATE TABLE subscription_sync_state (
            subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id), next_check_at TEXT,
            last_check_at TEXT, last_success_at TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0,
            access_state TEXT NOT NULL DEFAULT 'unknown' CHECK(access_state IN
              ('unknown','accessible','authentication_required','access_denied','temporarily_unavailable','provider_offline')),
            last_error_code TEXT, last_error TEXT)""")
        self._db.execute("""CREATE TABLE sync_jobs (
            id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL REFERENCES subscriptions(id),
            trigger TEXT NOT NULL CHECK(trigger IN ('manual','scheduled')),
            state TEXT NOT NULL CHECK(state IN ('queued','running','succeeded','failed','cancelled','interrupted')),
            created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            observed_snapshot TEXT, version_id TEXT REFERENCES versions(id), error TEXT)""")
        self._db.execute("CREATE UNIQUE INDEX one_active_sync ON sync_jobs(subscription_id) WHERE state IN ('queued','running')")
        self._db.execute("CREATE INDEX sync_due ON subscription_sync_state(next_check_at)")
        self._db.execute("INSERT INTO subscription_sync_policy(subscription_id,updated_at) SELECT id,? FROM subscriptions", (_now(),))
        self._db.execute("INSERT INTO subscription_sync_state(subscription_id) SELECT id FROM subscriptions")
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT INTO metadata VALUES ('schema_v3_migrated_at',?)", (_now(),))
        self._db.execute("PRAGMA user_version=3")

    def _migrate_v4(self) -> None:
        """Add a mutable match overlay without changing immutable archive rows."""
        self._db.execute("""CREATE TABLE local_assets (
            id TEXT PRIMARY KEY, media_type TEXT NOT NULL CHECK(media_type='track'),
            metadata_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self._db.execute("""CREATE TABLE local_asset_locations (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES local_assets(id),
            provider_instance_id TEXT NOT NULL, item_id TEXT NOT NULL,
            evidence_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(provider_instance_id,item_id))""")
        self._db.execute("""CREATE TABLE match_sources (
            id TEXT PRIMARY KEY, provider_domain TEXT NOT NULL, account_id TEXT NOT NULL,
            media_type TEXT NOT NULL CHECK(media_type='track'), source_item_id TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 0, approved_asset_id TEXT REFERENCES local_assets(id),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(provider_domain,account_id,media_type,source_item_id))""")
        self._db.execute("""CREATE TABLE match_candidates (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES match_sources(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL REFERENCES local_assets(id), score REAL NOT NULL CHECK(score BETWEEN 0 AND 1),
            evidence_json TEXT NOT NULL, algorithm_version TEXT NOT NULL, observed_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
            UNIQUE(source_id,asset_id))""")
        self._db.execute("""CREATE TABLE match_decisions (
            id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES match_sources(id),
            asset_id TEXT REFERENCES local_assets(id),
            action TEXT NOT NULL CHECK(action IN ('approve','reject','clear')),
            revision INTEGER NOT NULL, actor_id TEXT, evidence_json TEXT NOT NULL,
            algorithm_version TEXT, created_at TEXT NOT NULL, UNIQUE(source_id,revision),
            CHECK((action='clear' AND asset_id IS NULL) OR (action!='clear' AND asset_id IS NOT NULL)))""")
        self._db.execute("CREATE INDEX match_candidates_source ON match_candidates(source_id,score DESC,id)")
        self._db.execute("CREATE INDEX match_decisions_source ON match_decisions(source_id,revision)")
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT INTO metadata VALUES ('schema_v4_migrated_at',?)", (_now(),))
        self._db.execute("PRAGMA user_version=4")

    @staticmethod
    def _json_object(value: dict | None, label: str) -> str:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _source_key(provider_domain: str, account_id: str, media_type: str, source_item_id: str) -> tuple[str, str, str, str]:
        if media_type != "track" or not all(
            isinstance(value, str) and value.strip() for value in (provider_domain, account_id, source_item_id)
        ):
            raise ValueError("A stable track source key is required")
        return provider_domain, account_id, media_type, source_item_id

    def _ensure_match_source(self, provider_domain: str, account_id: str, media_type: str, source_item_id: str) -> dict:
        key = self._source_key(provider_domain, account_id, media_type, source_item_id)
        now = _now()
        self._db.execute(
            """INSERT OR IGNORE INTO match_sources
            (id,provider_domain,account_id,media_type,source_item_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)""",
            (str(uuid.uuid4()), *key, now, now),
        )
        return dict(
            self._db.execute(
                """SELECT * FROM match_sources WHERE provider_domain=? AND account_id=?
                AND media_type=? AND source_item_id=?""",
                key,
            ).fetchone()
        )

    def upsert_local_asset(
        self,
        media_type: str,
        provider_instance_id: str,
        item_id: str,
        metadata: dict | None = None,
        evidence: dict | None = None,
    ) -> dict:
        if media_type != "track" or not all(
            isinstance(value, str) and value.strip() for value in (provider_instance_id, item_id)
        ):
            raise ValueError("A local track location is required")
        metadata_json = self._json_object(metadata, "Asset metadata")
        evidence_json = self._json_object(evidence, "Location evidence")
        with self._transaction():
            location = self._db.execute(
                "SELECT asset_id FROM local_asset_locations WHERE provider_instance_id=? AND item_id=?",
                (provider_instance_id, item_id),
            ).fetchone()
            now = _now()
            if location is None:
                asset_id = str(uuid.uuid4())
                self._db.execute(
                    "INSERT INTO local_assets VALUES (?,?,?,?,?,?)",
                    (asset_id, media_type, metadata_json, evidence_json, now, now),
                )
                self._db.execute(
                    "INSERT INTO local_asset_locations VALUES (?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), asset_id, provider_instance_id, item_id, evidence_json, now, now),
                )
            else:
                asset_id = location["asset_id"]
                asset = self._db.execute("SELECT media_type FROM local_assets WHERE id=?", (asset_id,)).fetchone()
                if asset["media_type"] != media_type:
                    raise ValueError("Local location media type conflict")
                self._db.execute(
                    "UPDATE local_assets SET metadata_json=?,evidence_json=?,updated_at=? WHERE id=?",
                    (metadata_json, evidence_json, now, asset_id),
                )
                self._db.execute(
                    """UPDATE local_asset_locations SET evidence_json=?,updated_at=?
                    WHERE provider_instance_id=? AND item_id=?""",
                    (evidence_json, now, provider_instance_id, item_id),
                )
            return self.get_local_asset(asset_id)

    def add_local_asset_location(
        self, asset_id: str, provider_instance_id: str, item_id: str, evidence: dict | None = None
    ) -> dict:
        if not all(isinstance(value, str) and value.strip() for value in (asset_id, provider_instance_id, item_id)):
            raise ValueError("Stable asset and location identities are required")
        evidence_json = self._json_object(evidence, "Location evidence")
        with self._transaction():
            self.get_local_asset(asset_id)
            existing = self._db.execute(
                "SELECT * FROM local_asset_locations WHERE provider_instance_id=? AND item_id=?",
                (provider_instance_id, item_id),
            ).fetchone()
            if existing is not None and existing["asset_id"] != asset_id:
                raise ValueError("Local location is already bound to another asset")
            now = _now()
            if existing is None:
                location_id = str(uuid.uuid4())
                self._db.execute(
                    "INSERT INTO local_asset_locations VALUES (?,?,?,?,?,?,?)",
                    (location_id, asset_id, provider_instance_id, item_id, evidence_json, now, now),
                )
            else:
                location_id = existing["id"]
                self._db.execute(
                    "UPDATE local_asset_locations SET evidence_json=?,updated_at=? WHERE id=?",
                    (evidence_json, now, location_id),
                )
            result = dict(self._db.execute("SELECT * FROM local_asset_locations WHERE id=?", (location_id,)).fetchone())
            result["evidence"] = json.loads(result.pop("evidence_json"))
            return result

    def get_local_asset(self, asset_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM local_assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise KeyError(asset_id)
            result = dict(row)
            result["metadata"] = json.loads(result.pop("metadata_json"))
            result["evidence"] = json.loads(result.pop("evidence_json"))
            result["locations"] = [
                {**dict(location), "evidence": json.loads(location["evidence_json"])}
                for location in self._db.execute(
                    "SELECT * FROM local_asset_locations WHERE asset_id=? ORDER BY provider_instance_id,item_id", (asset_id,)
                )
            ]
            for location in result["locations"]:
                location.pop("evidence_json")
            return result

    def replace_match_candidates(
        self,
        provider_domain: str,
        account_id: str,
        media_type: str,
        source_item_id: str,
        candidates: list[dict],
        algorithm_version: str,
    ) -> dict:
        if not isinstance(candidates, list) or not isinstance(algorithm_version, str) or not algorithm_version.strip():
            raise ValueError("Candidates and algorithm version are required")
        normalized: list[tuple[str, float, str]] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict) or not isinstance(candidate.get("asset_id"), str):
                raise ValueError("Candidate asset is required")
            asset_id, score = candidate["asset_id"], candidate.get("score")
            if asset_id in seen or isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 1:
                raise ValueError("Candidate assets must be unique with score 0..1")
            seen.add(asset_id)
            normalized.append((asset_id, float(score), self._json_object(candidate.get("evidence"), "Candidate evidence")))
        with self._transaction():
            source = self._ensure_match_source(provider_domain, account_id, media_type, source_item_id)
            for asset_id, _, _ in normalized:
                asset = self.get_local_asset(asset_id)
                if asset["media_type"] != media_type:
                    raise ValueError("Candidate media type differs from source")
            existing = {
                row["asset_id"]: row["id"]
                for row in self._db.execute("SELECT id,asset_id FROM match_candidates WHERE source_id=?", (source["id"],))
            }
            now = _now()
            self._db.execute("UPDATE match_candidates SET active=0 WHERE source_id=?", (source["id"],))
            for asset_id, score, evidence_json in normalized:
                self._db.execute(
                    """INSERT INTO match_candidates
                    (id,source_id,asset_id,score,evidence_json,algorithm_version,observed_at,active)
                    VALUES (?,?,?,?,?,?,?,1) ON CONFLICT(source_id,asset_id) DO UPDATE SET
                    score=excluded.score,evidence_json=excluded.evidence_json,
                    algorithm_version=excluded.algorithm_version,observed_at=excluded.observed_at,active=1""",
                    (existing.get(asset_id, str(uuid.uuid4())), source["id"], asset_id, score, evidence_json, algorithm_version, now),
                )
            return self.get_match_overlay(provider_domain, account_id, media_type, source_item_id)

    def get_match_overlay(
        self, provider_domain: str, account_id: str, media_type: str, source_item_id: str
    ) -> dict:
        key = self._source_key(provider_domain, account_id, media_type, source_item_id)
        with self._lock:
            source_row = self._db.execute(
                """SELECT * FROM match_sources WHERE provider_domain=? AND account_id=?
                AND media_type=? AND source_item_id=?""",
                key,
            ).fetchone()
            if source_row is None:
                return {
                    "source": dict(zip(("provider_domain", "account_id", "media_type", "source_item_id"), key, strict=True)),
                    "revision": 0,
                    "decision": None,
                    "decision_history": [],
                    "approved_asset_id": None,
                    "candidates": [],
                }
            source = dict(source_row)
            decisions = [dict(row) for row in self._db.execute(
                "SELECT * FROM match_decisions WHERE source_id=? ORDER BY revision", (source["id"],)
            )]
            latest_by_asset: dict[str, str] = {}
            for decision in decisions:
                if decision["action"] == "clear":
                    latest_by_asset.clear()
                elif decision["asset_id"] is not None:
                    latest_by_asset[decision["asset_id"]] = decision["action"]
                decision["evidence"] = json.loads(decision.pop("evidence_json"))
            candidates = []
            for row in self._db.execute(
                "SELECT * FROM match_candidates WHERE source_id=? AND active=1 ORDER BY score DESC,id", (source["id"],)
            ):
                candidate = dict(row)
                candidate["evidence"] = json.loads(candidate.pop("evidence_json"))
                candidate["rejected"] = latest_by_asset.get(candidate["asset_id"]) == "reject"
                candidate["approved"] = candidate["asset_id"] == source["approved_asset_id"]
                candidate["asset"] = self.get_local_asset(candidate["asset_id"])
                candidates.append(candidate)
            public_source = {key: source[key] for key in ("id", "provider_domain", "account_id", "media_type", "source_item_id")}
            return {
                "source": public_source,
                "revision": source["revision"],
                "decision": decisions[-1] if decisions else None,
                "decision_history": decisions,
                "approved_asset_id": source["approved_asset_id"],
                "candidates": candidates,
            }

    def list_match_overlays(
        self, provider_domain: str | None = None, account_id: str | None = None, media_type: str | None = None
    ) -> list[dict]:
        if media_type not in (None, "track"):
            raise ValueError("Only track matching is supported")
        with self._lock:
            rows = self._db.execute(
                """SELECT provider_domain,account_id,media_type,source_item_id FROM match_sources
                WHERE (? IS NULL OR provider_domain=?) AND (? IS NULL OR account_id=?)
                AND (? IS NULL OR media_type=?) ORDER BY provider_domain,account_id,media_type,source_item_id""",
                (provider_domain, provider_domain, account_id, account_id, media_type, media_type),
            ).fetchall()
            return [self.get_match_overlay(*tuple(row)) for row in rows]

    def set_match_decision(
        self,
        provider_domain: str,
        account_id: str,
        media_type: str,
        source_item_id: str,
        action: str,
        asset_id: str,
        expected_revision: int,
        actor_id: str | None = None,
        evidence: dict | None = None,
        algorithm_version: str | None = None,
    ) -> dict:
        if action not in ("approve", "reject") or not isinstance(asset_id, str) or not asset_id.strip():
            raise ValueError("Approve or reject requires an asset")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected match revision required")
        evidence_json = self._json_object(evidence, "Decision evidence")
        with self._transaction():
            source = self._ensure_match_source(provider_domain, account_id, media_type, source_item_id)
            self.get_local_asset(asset_id)
            if source["revision"] != expected_revision:
                raise ValueError("Match decision revision conflict")
            if not self._db.execute(
                "SELECT 1 FROM match_candidates WHERE source_id=? AND asset_id=? AND active=1", (source["id"], asset_id)
            ).fetchone():
                raise ValueError("Decision asset is not a current candidate")
            revision, now = expected_revision + 1, _now()
            self._db.execute(
                """INSERT INTO match_decisions
                (id,source_id,asset_id,action,revision,actor_id,evidence_json,algorithm_version,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), source["id"], asset_id, action, revision, actor_id, evidence_json, algorithm_version, now),
            )
            approved = (
                asset_id
                if action == "approve"
                else None
                if source["approved_asset_id"] == asset_id
                else source["approved_asset_id"]
            )
            self._db.execute(
                "UPDATE match_sources SET revision=?,approved_asset_id=?,updated_at=? WHERE id=?",
                (revision, approved, now, source["id"]),
            )
            return self.get_match_overlay(provider_domain, account_id, media_type, source_item_id)

    def clear_match_decision(
        self,
        provider_domain: str,
        account_id: str,
        media_type: str,
        source_item_id: str,
        expected_revision: int,
        actor_id: str | None = None,
        evidence: dict | None = None,
    ) -> dict:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected match revision required")
        evidence_json = self._json_object(evidence, "Decision evidence")
        with self._transaction():
            source = self._ensure_match_source(provider_domain, account_id, media_type, source_item_id)
            if source["revision"] != expected_revision:
                raise ValueError("Match decision revision conflict")
            revision, now = expected_revision + 1, _now()
            self._db.execute(
                """INSERT INTO match_decisions
                (id,source_id,asset_id,action,revision,actor_id,evidence_json,created_at)
                VALUES (?,?,NULL,'clear',?,?,?,?)""",
                (str(uuid.uuid4()), source["id"], revision, actor_id, evidence_json, now),
            )
            self._db.execute(
                "UPDATE match_sources SET revision=?,approved_asset_id=NULL,updated_at=? WHERE id=?",
                (revision, now, source["id"]),
            )
            return self.get_match_overlay(provider_domain, account_id, media_type, source_item_id)

    def get_version_match_overlay(self, version_id: str) -> dict:
        version = self.get_version(version_id)
        subscription = self.get_subscription(version["subscription_id"])
        occurrences = []
        for occurrence in version["occurrences"]:
            source_item_id = occurrence.get("source_item_id") if occurrence.get("state") == "track" else None
            occurrences.append(
                {
                    "position": occurrence.get("position"),
                    "source_item_id": source_item_id,
                    "match": self.get_match_overlay(
                        subscription["provider_domain"], subscription["account_id"], "track", source_item_id
                    ) if source_item_id else None,
                }
            )
        return {"version_id": version_id, "content_digest": version["content_digest"], "occurrences": occurrences}

    def get_sync_policy(self, subscription_id: str) -> dict:
        with self._lock:
            self.get_subscription(subscription_id)
            return dict(self._db.execute("SELECT * FROM subscription_sync_policy WHERE subscription_id=?", (subscription_id,)).fetchone())

    def set_sync_policy(
        self,
        subscription_id: str,
        mode: str,
        interval_seconds: int = 86400,
        initiating_user_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        if mode not in ("manual", "scheduled") or type(interval_seconds) is not int or not 3600 <= interval_seconds <= 604800:
            raise ValueError("Invalid sync mode or interval")
        if mode == "scheduled" and (not isinstance(initiating_user_id, str) or not initiating_user_id.strip()):
            raise ValueError("Scheduled sync requires initiating user")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected policy revision required")
        with self._transaction():
            policy = self.get_sync_policy(subscription_id)
            if policy["revision"] != expected_revision:
                raise ValueError("Sync policy revision conflict")
            now = _now()
            due = (datetime.fromisoformat(now) + timedelta(seconds=interval_seconds)).isoformat() if mode == "scheduled" else None
            self._db.execute(
                """UPDATE subscription_sync_policy SET mode=?,interval_seconds=?,initiating_user_id=?,
                revision=revision+1,updated_at=? WHERE subscription_id=?""",
                (mode, interval_seconds, initiating_user_id, now, subscription_id),
            )
            self._db.execute("UPDATE subscription_sync_state SET next_check_at=? WHERE subscription_id=?", (due, subscription_id))
            return self.get_sync_policy(subscription_id)

    def list_due(self, now: str, limit: int = 50) -> list[dict]:
        now = _timestamp(now)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Due limit must be 1..200")
        with self._lock:
            return [
                dict(row)
                for row in self._db.execute(
                    """SELECT s.*,p.mode,p.interval_seconds,p.initiating_user_id,
                p.revision,t.next_check_at,t.consecutive_failures,t.access_state FROM subscriptions s
                JOIN subscription_sync_policy p ON p.subscription_id=s.id JOIN subscription_sync_state t ON t.subscription_id=s.id
                WHERE p.mode='scheduled' AND t.next_check_at<=? AND NOT EXISTS
                (SELECT 1 FROM sync_jobs j WHERE j.subscription_id=s.id AND j.state IN ('queued','running'))
                AND NOT EXISTS (SELECT 1 FROM jobs c WHERE c.subscription_id=s.id AND c.state='pending')
                ORDER BY t.next_check_at,s.id LIMIT ?""",
                    (now, limit),
                )
            ]

    def begin_sync(self, subscription_id: str, trigger: str = "manual") -> dict:
        if trigger not in ("manual", "scheduled"):
            raise ValueError("Invalid sync trigger")
        with self._transaction():
            policy = self.get_sync_policy(subscription_id)
            if trigger == "scheduled" and policy["mode"] != "scheduled":
                raise ValueError("Scheduled sync is disabled")
            if self._db.execute("SELECT 1 FROM jobs WHERE subscription_id=? AND state='pending'", (subscription_id,)).fetchone():
                raise ValueError("Capture already pending for subscription")
            job_id = str(uuid.uuid4())
            self._db.execute(
                "INSERT INTO sync_jobs(id,subscription_id,trigger,state,created_at) VALUES (?,?,?,'queued',?)",
                (job_id, subscription_id, trigger, _now()),
            )
            return self.get_sync_job(job_id)

    def get_sync_job(self, job_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM sync_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return dict(row)

    def list_sync_jobs(self, subscription_id: str | None = None) -> list[dict]:
        with self._lock:
            return [
                dict(row)
                for row in self._db.execute(
                    """SELECT * FROM sync_jobs
                WHERE (? IS NULL OR subscription_id=?) ORDER BY created_at,id""",
                    (subscription_id, subscription_id),
                )
            ]

    def get_sync_status(self, subscription_id: str) -> dict:
        with self._lock:
            return {
                "policy": self.get_sync_policy(subscription_id),
                "state": dict(
                    self._db.execute("SELECT * FROM subscription_sync_state WHERE subscription_id=?", (subscription_id,)).fetchone()
                ),
                "jobs": self.list_sync_jobs(subscription_id),
            }

    def mark_sync_running(self, job_id: str) -> None:
        with self._transaction():
            if self.get_sync_job(job_id)["state"] != "queued":
                raise ValueError("Sync is not queued")
            self._db.execute("UPDATE sync_jobs SET state='running',started_at=? WHERE id=?", (_now(), job_id))

    def observe_sync(self, job_id: str, snapshot: str) -> None:
        if not isinstance(snapshot, str) or not snapshot:
            raise ValueError("Snapshot required")
        with self._transaction():
            job = self.get_sync_job(job_id)
            if job["state"] != "running":
                raise ValueError("Sync is not running")
            now = _now()
            self._db.execute("UPDATE sync_jobs SET observed_snapshot=? WHERE id=?", (snapshot, job_id))
            self._db.execute(
                "UPDATE subscriptions SET observed_snapshot=?,observed_at=? WHERE id=?", (snapshot, now, job["subscription_id"])
            )
            self._db.execute(
                "UPDATE subscription_sync_state SET last_check_at=?,access_state='accessible' WHERE subscription_id=?",
                (now, job["subscription_id"]),
            )

    def _sync_next_check(self, subscription_id: str, now: str) -> str | None:
        policy = self.get_sync_policy(subscription_id)
        return (
            (datetime.fromisoformat(now) + timedelta(seconds=policy["interval_seconds"])).isoformat()
            if policy["mode"] == "scheduled"
            else None
        )

    def succeed_sync(self, job_id: str, version_id: str | None = None) -> None:
        with self._transaction():
            job = self.get_sync_job(job_id)
            if job["state"] != "running":
                raise ValueError("Sync is not running")
            if version_id is not None:
                version = self.get_version(version_id)
                if version["subscription_id"] != job["subscription_id"] or version["snapshot_id"] != job["observed_snapshot"]:
                    raise ValueError("Sync version does not match observed source")
            now = _now()
            self._db.execute(
                "UPDATE sync_jobs SET state='succeeded',finished_at=?,version_id=?,error=NULL WHERE id=?", (now, version_id, job_id)
            )
            self._db.execute(
                """UPDATE subscription_sync_state SET next_check_at=?,last_check_at=?,last_success_at=?,
                consecutive_failures=0,access_state='accessible',last_error_code=NULL,last_error=NULL WHERE subscription_id=?""",
                (self._sync_next_check(job["subscription_id"], now), now, now, job["subscription_id"]),
            )

    def fail_sync(
        self, job_id: str, error_code: str, error: str, access_state: str = "temporarily_unavailable", next_check_at: str | None = None
    ) -> None:
        if access_state not in ACCESS_STATES:
            raise ValueError("Invalid access state")
        if next_check_at is not None:
            next_check_at = _timestamp(next_check_at)
        with self._transaction():
            job = self.get_sync_job(job_id)
            if job["state"] not in ("queued", "running"):
                raise ValueError("Sync is not active")
            now = _now()
            policy = self.get_sync_policy(job["subscription_id"])
            due = next_check_at or self._sync_next_check(job["subscription_id"], now)
            if policy["mode"] == "manual" or access_state in ("authentication_required", "access_denied"):
                due = None
            self._db.execute("UPDATE sync_jobs SET state='failed',finished_at=?,error=? WHERE id=?", (now, error, job_id))
            self._db.execute(
                """UPDATE subscription_sync_state SET next_check_at=?,last_check_at=?,consecutive_failures=consecutive_failures+1,
                access_state=?,last_error_code=?,last_error=? WHERE subscription_id=?""",
                (due, now, access_state, error_code, error, job["subscription_id"]),
            )

    def recover_sync_pending(self) -> int:
        """At exclusive provider startup, interrupt abandoned work without changing checkpoints."""
        with self._transaction():
            return self._db.execute(
                """UPDATE sync_jobs SET state='interrupted',finished_at=?,
                error='Provider restarted; fresh check required' WHERE state IN ('queued','running')""",
                (_now(),),
            ).rowcount

    def cancel_sync(self, job_id: str) -> None:
        with self._transaction():
            job = self.get_sync_job(job_id)
            if job["state"] not in ("queued", "running"):
                raise ValueError("Sync is not active")
            now = _now()
            self._db.execute("UPDATE sync_jobs SET state='cancelled',finished_at=? WHERE id=?", (now, job_id))
            self._db.execute(
                "UPDATE subscription_sync_state SET next_check_at=? WHERE subscription_id=?",
                (self._sync_next_check(job["subscription_id"], now), job["subscription_id"]),
            )

    def prepare_apply(
        self,
        version_id: str,
        projection_digest: str,
        source_count: int,
        projected_count: int,
        omissions_json: str,
        marker: str,
        requested_name: str,
    ) -> dict:
        if (
            not isinstance(projection_digest, str)
            or len(projection_digest) != 64
            or any(c not in "0123456789abcdef" for c in projection_digest)
        ):
            raise ValueError("Expected SHA-256 projection digest")
        if type(source_count) is not int or type(projected_count) is not int or not 0 <= projected_count <= source_count:
            raise ValueError("Invalid projection counts")
        omissions = json.loads(omissions_json)
        if not isinstance(omissions, list) or len(omissions) != source_count - projected_count:
            raise ValueError("Omission count does not match projection")
        normalized = json.dumps(omissions, sort_keys=True, ensure_ascii=False, allow_nan=False)
        if not all(isinstance(value, str) and value.strip() for value in (marker, requested_name)):
            raise ValueError("Apply marker and destination name are required")
        with self._transaction():
            version = self.get_version(version_id)
            if version["total"] != source_count:
                raise ValueError("Projection source count differs from archive")
            existing = self.get_apply_for_version(version_id)
            if existing:
                if (existing["projection_digest"], existing["projected_count"], existing["omissions_json"]) != (
                    projection_digest,
                    projected_count,
                    normalized,
                ):
                    raise ValueError("Conflicting projection for existing apply intent")
                return existing
            job_id, now = str(uuid.uuid4()), _now()
            self._db.execute(
                """INSERT INTO apply_jobs
                (id,version_id,subscription_id,projection_digest,source_count,projected_count,
                 omissions_json,marker,requested_name,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
                (
                    job_id,
                    version_id,
                    version["subscription_id"],
                    projection_digest,
                    source_count,
                    projected_count,
                    normalized,
                    marker,
                    requested_name,
                    now,
                    now,
                ),
            )
            return self.get_apply(job_id)

    def get_apply(self, job_id: str) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM apply_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return dict(row)

    def get_apply_for_version(self, version_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM apply_jobs WHERE version_id=?", (version_id,)).fetchone()
            return dict(row) if row else None

    def list_applies(self) -> list[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute("SELECT * FROM apply_jobs ORDER BY created_at,id")]

    def mark_apply_creating(self, job_id: str) -> None:
        with self._transaction():
            job = self.get_apply(job_id)
            if job["state"] not in ("prepared", "failed"):
                raise ValueError("Apply requires reconciliation before creating another destination")
            self._db.execute("UPDATE apply_jobs SET state='creating',error=NULL,updated_at=? WHERE id=?", (_now(), job_id))

    def commit_apply(self, job_id: str, destination_item_id: str, destination_provider_instance: str, verified_digest: str) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (destination_item_id, destination_provider_instance)):
            raise ValueError("Verified destination identity required")
        with self._transaction():
            job = self.get_apply(job_id)
            if verified_digest != job["projection_digest"]:
                raise ValueError("Destination digest does not match projection")
            if job["state"] == "applied":
                if (job["destination_item_id"], job["destination_provider_instance"]) != (
                    destination_item_id,
                    destination_provider_instance,
                ):
                    raise ValueError("Apply already committed to another destination")
                return
            if job["state"] not in ("creating", "uncertain"):
                raise ValueError("Apply must be created or reconciled before commit")
            now = _now()
            self._db.execute(
                """UPDATE apply_jobs SET state='applied',updated_at=?,error=NULL,
                destination_item_id=?,destination_provider_instance=?,verified_digest=? WHERE id=?""",
                (now, destination_item_id, destination_provider_instance, verified_digest, job_id),
            )
            self._db.execute(
                "UPDATE subscriptions SET applied_version_id=?,applied_at=?,applied_job_id=? WHERE id=?",
                (job["version_id"], now, job_id, job["subscription_id"]),
            )

    def fail_apply(self, job_id: str, error: str, uncertain: bool = False) -> None:
        with self._transaction():
            job = self.get_apply(job_id)
            if job["state"] in ("applied", "conflict"):
                raise ValueError("Cannot fail a completed or conflicted apply")
            # Once creation was entered, a generic error cannot prove no destination exists.
            state = "uncertain" if uncertain or job["state"] in ("creating", "uncertain") else "failed"
            self._db.execute("UPDATE apply_jobs SET state=?,error=?,updated_at=? WHERE id=?", (state, error, _now(), job_id))

    def conflict_apply(self, job_id: str, error: str) -> None:
        with self._transaction():
            job = self.get_apply(job_id)
            if job["state"] == "applied":
                raise ValueError("Cannot rewrite an applied checkpoint")
            self._db.execute("UPDATE apply_jobs SET state='conflict',error=?,updated_at=? WHERE id=?", (error, _now(), job_id))

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
            result = dict(
                self._db.execute(
                    """SELECT * FROM subscriptions WHERE
                provider_domain=? AND account_id=? AND source_playlist_id=?""",
                    (provider_domain, account_id, source_playlist_id),
                ).fetchone()
            )
            self._db.execute(
                "INSERT OR IGNORE INTO subscription_sync_policy(subscription_id,updated_at) VALUES (?,?)", (result["id"], _now())
            )
            self._db.execute("INSERT OR IGNORE INTO subscription_sync_state(subscription_id) VALUES (?)", (result["id"],))
            return result

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

    def begin_capture(self, subscription_id: str, snapshot_id: str, sync_job_id: str | None = None) -> str:
        if not snapshot_id:
            raise ValueError("Snapshot identity required")
        with self._transaction():
            self.get_subscription(subscription_id)
            active_sync = self._db.execute(
                "SELECT id,state FROM sync_jobs WHERE subscription_id=? AND state IN ('queued','running')", (subscription_id,)
            ).fetchone()
            if active_sync is not None and (active_sync["id"] != sync_job_id or active_sync["state"] != "running"):
                raise ValueError("Subscription has an active sync")
            if sync_job_id is not None and (active_sync is None or active_sync["id"] != sync_job_id):
                raise ValueError("Sync capture ownership is invalid")
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
