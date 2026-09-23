"""Independent, durable source archive. Never writes Music Assistant core tables.

Methods are synchronous and serialized; async callers should use ``asyncio.to_thread``.
Capture payloads must already be filtered to exclude credentials by the adapter.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA_VERSION = 11
ACCESS_STATES = {"unknown", "accessible", "authentication_required", "access_denied", "temporarily_unavailable", "provider_offline"}
PROVENANCE_STATES = {"value", "stale", "missing", "empty", "not_loaded", "inaccessible"}
PROVENANCE_TYPES = {"string", "integer", "number", "boolean", "object", "array", "null"}


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
                elif version not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, SCHEMA_VERSION):
                    raise ValueError(f"Unsupported enrichment schema {version}; database untouched")
                required = {"metadata", "subscriptions", "jobs", "versions", "occurrences"}
                if version >= 2:
                    required.add("apply_jobs")
                if version >= 3:
                    required.update(("subscription_sync_policy", "subscription_sync_state", "sync_jobs"))
                if version >= 4:
                    required.update(("match_sources", "local_assets", "local_asset_locations", "match_candidates", "match_decisions"))
                if version >= 5:
                    required.update(("playback_policies", "playback_projections"))
                if version >= 6:
                    required.update(("provenance_subjects", "provenance_values", "provenance_overrides"))
                if version >= 7:
                    required.update(("itunes_import_documents", "itunes_import_batches"))
                if version >= 8:
                    required.add("itunes_apply_jobs")
                if version >= 9:
                    required.add("bulk_match_operations")
                if version >= 11:
                    required.add("maintained_mirrors")
                actual = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                later_tables = {
                    "itunes_import_documents", "itunes_import_batches",
                    "itunes_apply_jobs", "bulk_match_operations",
                    "maintained_mirrors",
                }
                if actual != required and not (
                    version < 9 and required <= actual <= required | later_tables
                ):
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
                if version <= 4:
                    self._migrate_v5()
                if version <= 5:
                    self._migrate_v6()
                if version <= 6:
                    self._migrate_v7()
                if version <= 7:
                    self._migrate_v8()
                if version <= 8:
                    self._migrate_v9()
                if version <= 9:
                    self._migrate_v10()
                if version <= 10:
                    self._migrate_v11()
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

    def _migrate_v5(self) -> None:
        """Add explicit playback policy and its independent projection checkpoint."""
        self._db.execute("""CREATE TABLE playback_policies (
            subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id),
            mode TEXT NOT NULL DEFAULT 'prefer_spotify'
              CHECK(mode IN ('prefer_local','local_only','prefer_spotify')),
            revision INTEGER NOT NULL DEFAULT 0, actor_id TEXT, updated_at TEXT NOT NULL)""")
        self._db.execute("""CREATE TABLE playback_projections (
            subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id),
            version_id TEXT NOT NULL REFERENCES versions(id), policy_revision INTEGER NOT NULL,
            projection_digest TEXT NOT NULL, source_count INTEGER NOT NULL,
            projected_count INTEGER NOT NULL, gaps_json TEXT NOT NULL,
            destination_item_id TEXT, destination_provider_instance TEXT,
            state TEXT NOT NULL CHECK(state IN ('prepared','writing','applied','failed','uncertain','conflict')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT)""")
        self._db.execute(
            "INSERT INTO playback_policies(subscription_id,updated_at) SELECT id,? FROM subscriptions", (_now(),)
        )
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT INTO metadata VALUES ('schema_v5_migrated_at',?)", (_now(),))
        self._db.execute("PRAGMA user_version=5")

    def _migrate_v6(self) -> None:
        """Add immutable provenance observations and override events."""
        self._db.execute("""CREATE TABLE provenance_subjects (
            id TEXT PRIMARY KEY, provider_domain TEXT NOT NULL, account_id TEXT NOT NULL,
            media_type TEXT NOT NULL, source_item_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(provider_domain,account_id,media_type,source_item_id))""")
        self._db.execute("""CREATE TABLE provenance_values (
            id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES provenance_subjects(id),
            field_name TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
              ('value','stale','missing','empty','not_loaded','inaccessible')),
            value_type TEXT CHECK(value_type IN ('string','integer','number','boolean','object','array','null')),
            value_json TEXT, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
            parser_version TEXT NOT NULL, date_precision TEXT, unit TEXT,
            raw_version_id TEXT REFERENCES versions(id), raw_position INTEGER,
            raw_json_pointer TEXT, created_at TEXT NOT NULL,
            CHECK((state='value' AND value_type IS NOT NULL AND value_json IS NOT NULL)
               OR (state='stale' AND ((value_type IS NULL AND value_json IS NULL)
                                   OR (value_type IS NOT NULL AND value_json IS NOT NULL)))
               OR (state NOT IN ('value','stale') AND value_type IS NULL AND value_json IS NULL)),
            CHECK((raw_version_id IS NULL AND raw_position IS NULL AND raw_json_pointer IS NULL)
               OR (raw_version_id IS NOT NULL AND raw_position IS NOT NULL AND raw_position>=0)))""")
        self._db.execute("""CREATE TABLE provenance_overrides (
            id TEXT PRIMARY KEY, subject_id TEXT NOT NULL REFERENCES provenance_subjects(id),
            field_name TEXT NOT NULL, action TEXT NOT NULL CHECK(action IN ('set','clear')),
            revision INTEGER NOT NULL CHECK(revision>0), actor_id TEXT NOT NULL,
            value_type TEXT CHECK(value_type IN ('string','integer','number','boolean','object','array','null')),
            value_json TEXT, created_at TEXT NOT NULL,
            UNIQUE(subject_id,field_name,revision),
            CHECK((action='set' AND value_type IS NOT NULL AND value_json IS NOT NULL)
               OR (action='clear' AND value_type IS NULL AND value_json IS NULL)))""")
        self._db.execute(
            "CREATE INDEX provenance_values_latest ON provenance_values(subject_id,field_name,created_at DESC,id DESC)"
        )
        self._db.execute(
            "CREATE INDEX provenance_overrides_latest ON provenance_overrides(subject_id,field_name,revision DESC)"
        )
        # Existing stable source identities become subjects. Migration never guesses values.
        keys = {
            tuple(row)
            for row in self._db.execute(
                "SELECT provider_domain,account_id,media_type,source_item_id FROM match_sources"
            )
        }
        for row in self._db.execute(
            """SELECT s.provider_domain,s.account_id,o.payload FROM occurrences o
            JOIN versions v ON v.id=o.version_id JOIN subscriptions s ON s.id=v.subscription_id"""
        ):
            try:
                occurrence = json.loads(row[2])
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(occurrence, dict):
                continue
            source_item_id = occurrence.get("source_item_id")
            if occurrence.get("state") == "track" and isinstance(source_item_id, str) and source_item_id.strip():
                keys.add((row[0], row[1], "track", source_item_id))
        now = _now()
        self._db.executemany(
            "INSERT INTO provenance_subjects VALUES (?,?,?,?,?,?)",
            [(str(uuid.uuid4()), *key, now) for key in sorted(keys)],
        )
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT INTO metadata VALUES ('schema_v6_migrated_at',?)", (now,))
        self._db.execute("PRAGMA user_version=6")

    def _migrate_v7(self) -> None:
        """Add restart-safe staged iTunes imports without retaining source XML."""
        self._db.execute("""CREATE TABLE IF NOT EXISTS itunes_import_documents (
            id TEXT PRIMARY KEY, source_digest TEXT NOT NULL UNIQUE,
            library_persistent_id TEXT NOT NULL, source_metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL)""")
        self._db.execute("""CREATE TABLE IF NOT EXISTS itunes_import_batches (
            id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES itunes_import_documents(id),
            plan_digest TEXT NOT NULL, path_mappings_json TEXT NOT NULL,
            playlist_ids_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('staged','previewed','committed','failed')),
            revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0),
            preview_revision INTEGER NOT NULL DEFAULT 0 CHECK(preview_revision>=0),
            preview_digest TEXT, preview_json TEXT,
            committed_result_json TEXT, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(document_id,plan_digest),
            CHECK((preview_digest IS NULL AND preview_json IS NULL AND preview_revision=0)
               OR (preview_digest IS NOT NULL AND preview_json IS NOT NULL AND preview_revision>0)),
            CHECK((status='committed' AND committed_result_json IS NOT NULL)
               OR (status!='committed' AND committed_result_json IS NULL)))""")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS itunes_import_batches_document ON itunes_import_batches(document_id,created_at,id)"
        )
        now = _now()
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_v7_migrated_at',?)", (now,))
        self._db.execute("PRAGMA user_version=7")

    def _migrate_v8(self) -> None:
        """Add durable intent for one-playlist legacy imports."""
        self._db.execute("""CREATE TABLE IF NOT EXISTS itunes_apply_jobs (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL UNIQUE REFERENCES itunes_import_batches(id),
            preview_revision INTEGER NOT NULL, preview_digest TEXT NOT NULL,
            projection_digest TEXT NOT NULL, playlist_id TEXT NOT NULL, requested_name TEXT NOT NULL,
            source_count INTEGER NOT NULL CHECK(source_count>=0), uris_json TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('prepared','creating','applied','failed','uncertain')),
            destination_item_id TEXT, destination_provider_instance TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT)""")
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_v8_migrated_at',?)", (_now(),))
        self._db.execute("PRAGMA user_version=8")

    def _migrate_v9(self) -> None:
        """Persist exact bulk-review responses for safe transport retries."""
        self._db.execute("""CREATE TABLE IF NOT EXISTS bulk_match_operations (
            operation_id TEXT PRIMARY KEY, request_digest TEXT NOT NULL,
            response_json TEXT NOT NULL, created_at TEXT NOT NULL)""")
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_v9_migrated_at',?)", (_now(),))
        self._db.execute("PRAGMA user_version=9")

    def _migrate_v10(self) -> None:
        """Remember the exact verified destination before any later rewrite."""
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(playback_projections)")}
        if "destination_content_digest" not in columns:
            self._db.execute("ALTER TABLE playback_projections ADD COLUMN destination_content_digest TEXT")
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_v10_migrated_at',?)", (_now(),))
        self._db.execute("PRAGMA user_version=10")

    def _migrate_v11(self) -> None:
        """Keep opt-in current mirrors separate from immutable captures and playback projections."""
        self._db.execute("""CREATE TABLE IF NOT EXISTS maintained_mirrors (
            subscription_id TEXT PRIMARY KEY REFERENCES subscriptions(id),
            revision INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
            allow_partial INTEGER NOT NULL DEFAULT 0 CHECK(allow_partial IN (0,1)),
            state TEXT NOT NULL CHECK(state IN
              ('pending','prepared','writing','applied','failed','uncertain','conflict','detached','disabled')),
            applied_version_id TEXT REFERENCES versions(id),
            applied_digest TEXT,
            target_version_id TEXT REFERENCES versions(id),
            target_digest TEXT,
            destination_item_id TEXT,
            destination_provider_instance TEXT,
            destination_content_digest TEXT,
            error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        self._db.execute("UPDATE metadata SET value=? WHERE key='schema_digest'", (self._schema_digest(),))
        self._db.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_v11_migrated_at',?)", (_now(),))
        self._db.execute("PRAGMA user_version=11")

    @staticmethod
    def _bounded_json(value, label: str, expected_type: type, limit: int = 65536) -> str:
        if not isinstance(value, expected_type):
            raise ValueError(f"{label} must be a {expected_type.__name__}")
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > limit:
            raise ValueError(f"{label} exceeds {limit} bytes")
        return encoded

    @staticmethod
    def _sha256(value: str, label: str) -> str:
        if not isinstance(value, str) or len(value) != 64 or any(char not in "0123456789abcdefABCDEF" for char in value):
            raise ValueError(f"{label} must be a SHA-256 digest")
        return value.lower()

    @staticmethod
    def _decode_itunes_import(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["inspection_id"] = result.pop("id")
        result["source_metadata"] = json.loads(result.pop("source_metadata_json"))
        result["path_mappings"] = json.loads(result.pop("path_mappings_json"))
        result["playlist_ids"] = json.loads(result.pop("playlist_ids_json"))
        result["preview"] = json.loads(result.pop("preview_json")) if result.get("preview_json") is not None else None
        result.pop("preview_json", None)
        result["committed_result"] = (
            json.loads(result.pop("committed_result_json"))
            if result.get("committed_result_json") is not None else None
        )
        result.pop("committed_result_json", None)
        return result

    def stage_itunes_import(
        self, source_digest: str, library_persistent_id: str, source_metadata: dict,
        path_mappings: list, playlist_ids: list[str],
    ) -> dict:
        """Persist an immutable source identity and idempotent import plan."""
        source_digest = self._sha256(source_digest, "source_digest")
        if (
            not isinstance(library_persistent_id, str)
            or not 1 <= len(library_persistent_id.strip()) <= 128
            or not isinstance(playlist_ids, list)
            or not all(isinstance(item, str) and 1 <= len(item) <= 256 for item in playlist_ids)
            or len(playlist_ids) != len(set(playlist_ids))
        ):
            raise ValueError("A bounded library ID and unique playlist IDs are required")
        metadata_json = self._bounded_json(source_metadata, "source_metadata", dict)
        mappings_json = self._bounded_json(path_mappings, "path_mappings", list)
        playlists_json = self._bounded_json(playlist_ids, "playlist_ids", list)
        plan_digest = hashlib.sha256(f"{mappings_json}\n{playlists_json}".encode()).hexdigest()
        now = _now()
        with self._transaction():
            document = self._db.execute(
                "SELECT * FROM itunes_import_documents WHERE source_digest=?", (source_digest,)
            ).fetchone()
            if document is None:
                document_id = str(uuid.uuid4())
                self._db.execute(
                    "INSERT INTO itunes_import_documents VALUES (?,?,?,?,?)",
                    (document_id, source_digest, library_persistent_id.strip(), metadata_json, now),
                )
            else:
                if (
                    document["library_persistent_id"] != library_persistent_id.strip()
                    or document["source_metadata_json"] != metadata_json
                ):
                    raise ValueError("Source digest metadata conflict")
                document_id = document["id"]
            row = self._db.execute(
                "SELECT b.*,d.source_digest,d.library_persistent_id,d.source_metadata_json "
                "FROM itunes_import_batches b JOIN itunes_import_documents d ON d.id=b.document_id "
                "WHERE b.document_id=? AND b.plan_digest=?",
                (document_id, plan_digest),
            ).fetchone()
            if row is None:
                batch_id = str(uuid.uuid4())
                self._db.execute(
                    """INSERT INTO itunes_import_batches
                    (id,document_id,plan_digest,path_mappings_json,playlist_ids_json,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,'staged',?,?)""",
                    (batch_id, document_id, plan_digest, mappings_json, playlists_json, now, now),
                )
                row = self._select_itunes_import(batch_id)
            return self._decode_itunes_import(row)

    def _select_itunes_import(self, batch_id: str) -> sqlite3.Row:
        row = self._db.execute(
            "SELECT b.*,d.source_digest,d.library_persistent_id,d.source_metadata_json "
            "FROM itunes_import_batches b JOIN itunes_import_documents d ON d.id=b.document_id WHERE b.id=?",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return row

    def get_itunes_import(self, batch_id: str) -> dict:
        with self._lock:
            return self._decode_itunes_import(self._select_itunes_import(batch_id))

    def record_itunes_import_preview(
        self, batch_id: str, expected_revision: int, preview_digest: str, preview: dict,
    ) -> dict:
        preview_digest = self._sha256(preview_digest, "preview_digest")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("A non-negative expected revision is required")
        preview_json = self._bounded_json(preview, "preview", dict, 2 * 1024 * 1024)
        with self._transaction():
            current = self._select_itunes_import(batch_id)
            if current["revision"] != expected_revision:
                raise ValueError("iTunes import revision conflict")
            if current["status"] not in ("staged", "previewed"):
                raise ValueError("iTunes import cannot be previewed in its current state")
            self._db.execute(
                """UPDATE itunes_import_batches SET status='previewed',revision=revision+1,
                preview_revision=preview_revision+1,preview_digest=?,preview_json=?,error=NULL,updated_at=? WHERE id=?""",
                (preview_digest, preview_json, _now(), batch_id),
            )
            return self._decode_itunes_import(self._select_itunes_import(batch_id))

    def commit_itunes_import(
        self, batch_id: str, expected_revision: int, preview_digest: str, committed_result: dict,
    ) -> dict:
        preview_digest = self._sha256(preview_digest, "preview_digest")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("A non-negative expected revision is required")
        result_json = self._bounded_json(committed_result, "committed_result", dict, 262144)
        with self._transaction():
            current = self._select_itunes_import(batch_id)
            if current["status"] == "committed":
                if current["preview_digest"] == preview_digest and current["committed_result_json"] == result_json:
                    return self._decode_itunes_import(current)
                raise ValueError("iTunes import is already committed")
            if current["revision"] != expected_revision:
                raise ValueError("iTunes import revision conflict")
            if current["status"] != "previewed" or current["preview_digest"] != preview_digest:
                raise ValueError("iTunes import preview does not match")
            self._db.execute(
                """UPDATE itunes_import_batches SET status='committed',revision=revision+1,
                committed_result_json=?,error=NULL,updated_at=? WHERE id=?""",
                (result_json, _now(), batch_id),
            )
            return self._decode_itunes_import(self._select_itunes_import(batch_id))

    def fail_itunes_import(self, batch_id: str, expected_revision: int, error: str) -> dict:
        if (
            type(expected_revision) is not int or expected_revision < 0
            or not isinstance(error, str) or not error.strip() or len(error) > 2048
        ):
            raise ValueError("A revision and bounded error are required")
        with self._transaction():
            current = self._select_itunes_import(batch_id)
            if current["revision"] != expected_revision:
                raise ValueError("iTunes import revision conflict")
            if current["status"] == "committed":
                raise ValueError("Committed iTunes import cannot fail")
            self._db.execute(
                "UPDATE itunes_import_batches SET status='failed',revision=revision+1,error=?,updated_at=? WHERE id=?",
                (error.strip(), _now(), batch_id),
            )
            return self._decode_itunes_import(self._select_itunes_import(batch_id))

    @staticmethod
    def _decode_itunes_apply(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["uris"] = json.loads(result.pop("uris_json"))
        return result

    def get_itunes_apply(self, batch_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM itunes_apply_jobs WHERE batch_id=?", (batch_id,)).fetchone()
            return self._decode_itunes_apply(row)

    def prepare_itunes_apply(
        self, batch_id: str, expected_revision: int, preview_digest: str, projection_digest: str,
        playlist_id: str, requested_name: str, source_count: int, uris: list[str],
    ) -> dict:
        preview_digest = self._sha256(preview_digest, "preview_digest")
        projection_digest = self._sha256(projection_digest, "projection_digest")
        uris_json = self._bounded_json(uris, "uris", list, 2 * 1024 * 1024)
        if (
            type(expected_revision) is not int or expected_revision < 0
            or not isinstance(playlist_id, str) or not playlist_id
            or not isinstance(requested_name, str) or not requested_name
            or len(requested_name) > 160 or len(uris) > 10000
            or type(source_count) is not int or not len(uris) <= source_count <= 10000
            or not all(isinstance(uri, str) and uri.startswith("library://track/") for uri in uris)
        ):
            raise ValueError("A bounded one-playlist apply plan is required")
        with self._transaction():
            batch = self._select_itunes_import(batch_id)
            existing = self._db.execute("SELECT * FROM itunes_apply_jobs WHERE batch_id=?", (batch_id,)).fetchone()
            if existing is not None:
                decoded = self._decode_itunes_apply(existing)
                if (
                    existing["preview_digest"] != preview_digest
                    or existing["projection_digest"] != projection_digest
                    or existing["playlist_id"] != playlist_id
                ):
                    raise ValueError("iTunes apply intent conflicts with the existing job")
                return decoded
            if (
                batch["status"] != "previewed" or batch["revision"] != expected_revision
                or batch["preview_digest"] != preview_digest
            ):
                raise ValueError("iTunes import preview changed; preview again")
            now, job_id = _now(), str(uuid.uuid4())
            self._db.execute(
                """INSERT INTO itunes_apply_jobs
                (id,batch_id,preview_revision,preview_digest,projection_digest,playlist_id,requested_name,
                 source_count,uris_json,state,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
                (job_id, batch_id, batch["preview_revision"], preview_digest, projection_digest,
                 playlist_id, requested_name, source_count, uris_json, now, now),
            )
            return self._decode_itunes_apply(
                self._db.execute("SELECT * FROM itunes_apply_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def mark_itunes_apply_creating(self, job_id: str) -> dict:
        with self._transaction():
            row = self._db.execute("SELECT * FROM itunes_apply_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "prepared":
                raise ValueError("iTunes apply job is not prepared")
            self._db.execute(
                "UPDATE itunes_apply_jobs SET state='creating',updated_at=? WHERE id=?", (_now(), job_id)
            )
            return self._decode_itunes_apply(
                self._db.execute("SELECT * FROM itunes_apply_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def commit_itunes_apply(
        self, job_id: str, destination_item_id: str, destination_provider_instance: str,
    ) -> dict:
        if not all(isinstance(value, str) and value for value in (destination_item_id, destination_provider_instance)):
            raise ValueError("iTunes apply destination is required")
        with self._transaction():
            row = self._db.execute("SELECT * FROM itunes_apply_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] not in ("creating", "uncertain"):
                raise ValueError("iTunes apply job is not creating")
            now = _now()
            result = {
                "created_playlists": 1, "imported_tracks": len(json.loads(row["uris_json"])),
                "source_tracks": row["source_count"],
                "destination_item_id": destination_item_id,
                "destination_provider_instance": destination_provider_instance,
            }
            result_json = self._bounded_json(result, "committed_result", dict, 262144)
            self._db.execute(
                """UPDATE itunes_apply_jobs SET state='applied',destination_item_id=?,
                destination_provider_instance=?,error=NULL,updated_at=? WHERE id=?""",
                (destination_item_id, destination_provider_instance, now, job_id),
            )
            self._db.execute(
                """UPDATE itunes_import_batches SET status='committed',revision=revision+1,
                committed_result_json=?,error=NULL,updated_at=? WHERE id=?""",
                (result_json, now, row["batch_id"]),
            )
            return self._decode_itunes_apply(
                self._db.execute("SELECT * FROM itunes_apply_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def fail_itunes_apply(self, job_id: str, error: str, *, uncertain: bool) -> dict:
        if not isinstance(error, str) or not error or len(error) > 2048:
            raise ValueError("A bounded iTunes apply error is required")
        with self._transaction():
            row = self._db.execute("SELECT * FROM itunes_apply_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] == "applied":
                raise ValueError("iTunes apply job cannot fail")
            state = "uncertain" if uncertain or row["state"] == "creating" else "failed"
            self._db.execute(
                "UPDATE itunes_apply_jobs SET state=?,error=?,updated_at=? WHERE id=?",
                (state, error, _now(), job_id),
            )
            return self._decode_itunes_apply(
                self._db.execute("SELECT * FROM itunes_apply_jobs WHERE id=?", (job_id,)).fetchone()
            )

    def recover_itunes_apply_pending(self) -> None:
        with self._transaction():
            now = _now()
            self._db.execute(
                """UPDATE itunes_apply_jobs SET state='uncertain',error='Interrupted during playlist creation',updated_at=?
                WHERE state='creating'""", (now,)
            )

    @staticmethod
    def _json_object(value: dict | None, label: str) -> str:
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be an object")
        return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _provenance_key(
        provider_domain: str, account_id: str, media_type: str, source_item_id: str
    ) -> tuple[str, str, str, str]:
        key = (provider_domain, account_id, media_type, source_item_id)
        if not all(isinstance(value, str) and value.strip() and len(value) <= 512 for value in key):
            raise ValueError("A bounded stable provenance subject key is required")
        return key

    @staticmethod
    def _typed_value(value) -> tuple[str, str]:
        if value is None:
            value_type = "null"
        elif isinstance(value, bool):
            value_type = "boolean"
        elif type(value) is int:
            value_type = "integer"
        elif type(value) is float:
            value_type = "number"
        elif isinstance(value, str):
            value_type = "string"
        elif isinstance(value, dict):
            value_type = "object"
        elif isinstance(value, list):
            value_type = "array"
        else:
            raise ValueError("Unsupported provenance value type")
        return value_type, json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)

    def _ensure_provenance_subject(
        self, provider_domain: str, account_id: str, media_type: str, source_item_id: str
    ) -> dict:
        key = self._provenance_key(provider_domain, account_id, media_type, source_item_id)
        self._db.execute(
            """INSERT OR IGNORE INTO provenance_subjects
            (id,provider_domain,account_id,media_type,source_item_id,created_at) VALUES (?,?,?,?,?,?)""",
            (str(uuid.uuid4()), *key, _now()),
        )
        return dict(
            self._db.execute(
                """SELECT * FROM provenance_subjects WHERE provider_domain=? AND account_id=?
                AND media_type=? AND source_item_id=?""",
                key,
            ).fetchone()
        )

    def lookup_provenance_subject(
        self, provider_domain: str, account_id: str, media_type: str, source_item_id: str
    ) -> dict | None:
        key = self._provenance_key(provider_domain, account_id, media_type, source_item_id)
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM provenance_subjects WHERE provider_domain=? AND account_id=?
                AND media_type=? AND source_item_id=?""",
                key,
            ).fetchone()
            return dict(row) if row else None

    def _validate_raw_reference(self, subject: dict, reference: dict | None) -> tuple[str | None, int | None, str | None]:
        if reference is None:
            return None, None, None
        if not isinstance(reference, dict) or set(reference) - {"version_id", "position", "json_pointer"}:
            raise ValueError("Raw reference must be a bounded occurrence reference")
        version_id, position = reference.get("version_id"), reference.get("position")
        pointer = reference.get("json_pointer")
        if not isinstance(version_id, str) or type(position) is not int or position < 0:
            raise ValueError("Raw reference requires version and position")
        if pointer is not None and (not isinstance(pointer, str) or len(pointer) > 512 or (pointer and not pointer.startswith("/"))):
            raise ValueError("Invalid raw JSON pointer")
        row = self._db.execute(
            """SELECT s.provider_domain,s.account_id,o.payload FROM occurrences o
            JOIN versions v ON v.id=o.version_id JOIN subscriptions s ON s.id=v.subscription_id
            WHERE o.version_id=? AND o.position=?""",
            (version_id, position),
        ).fetchone()
        if row is None or (row[0], row[1]) != (subject["provider_domain"], subject["account_id"]):
            raise ValueError("Raw reference is outside the provenance subject account")
        occurrence = json.loads(row[2])
        if occurrence.get("source_item_id") != subject["source_item_id"]:
            raise ValueError("Raw reference does not identify the provenance subject")
        return version_id, position, pointer

    def upsert_provenance_values(
        self,
        provider_domain: str,
        account_id: str,
        media_type: str,
        source_item_id: str,
        values: list[dict],
    ) -> dict:
        """Append a validated observation batch; existing observations and overrides remain immutable."""
        if not isinstance(values, list) or not values:
            raise ValueError("At least one provenance value is required")
        with self._transaction():
            subject = self._ensure_provenance_subject(provider_domain, account_id, media_type, source_item_id)
            prepared = []
            seen = set()
            for item in values:
                if not isinstance(item, dict):
                    raise ValueError("Provenance values must be objects")
                field = item.get("field_name")
                state = item.get("state")
                source = item.get("source")
                parser_version = item.get("parser_version")
                if (
                    not isinstance(field, str) or not field.strip() or len(field) > 128 or field in seen
                    or state not in PROVENANCE_STATES
                    or not isinstance(source, str) or not source.strip() or len(source) > 256
                    or not isinstance(parser_version, str) or not parser_version.strip() or len(parser_version) > 128
                ):
                    raise ValueError("Invalid provenance observation")
                seen.add(field)
                fetched_at = _timestamp(item.get("fetched_at")) if isinstance(item.get("fetched_at"), str) else None
                if fetched_at is None:
                    raise ValueError("Provenance observation requires fetched_at")
                if state in ("value", "stale") and "value" in item:
                    value_type, value_json = self._typed_value(item["value"])
                elif state == "value":
                    if "value" not in item:
                        raise ValueError("Value state requires a typed value")
                elif state == "stale":
                    value_type = value_json = None
                else:
                    if "value" in item:
                        raise ValueError("Non-value provenance states cannot carry a value")
                    value_type = value_json = None
                precision, unit = item.get("date_precision"), item.get("unit")
                if precision is not None and (not isinstance(precision, str) or not precision.strip() or len(precision) > 32):
                    raise ValueError("Invalid date precision")
                if unit is not None and (not isinstance(unit, str) or not unit.strip() or len(unit) > 64):
                    raise ValueError("Invalid provenance unit")
                raw_version, raw_position, raw_pointer = self._validate_raw_reference(subject, item.get("raw_reference"))
                identity = (
                    subject["id"], field, state, value_type, value_json, source, fetched_at,
                    parser_version, precision, unit, raw_version, raw_position, raw_pointer,
                )
                if not self._db.execute(
                    """SELECT 1 FROM provenance_values WHERE subject_id=? AND field_name=? AND state=?
                    AND value_type IS ? AND value_json IS ? AND source=? AND fetched_at=? AND parser_version=?
                    AND date_precision IS ? AND unit IS ? AND raw_version_id IS ? AND raw_position IS ?
                    AND raw_json_pointer IS ? LIMIT 1""",
                    identity,
                ).fetchone():
                    prepared.append((str(uuid.uuid4()), *identity, _now()))
            self._db.executemany(
                """INSERT INTO provenance_values
                (id,subject_id,field_name,state,value_type,value_json,source,fetched_at,parser_version,
                 date_precision,unit,raw_version_id,raw_position,raw_json_pointer,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                prepared,
            )
            return self.get_provenance_overlay(provider_domain, account_id, media_type, source_item_id)

    @staticmethod
    def _decoded_provenance_row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        encoded = result.pop("value_json", None)
        result["value"] = json.loads(encoded) if encoded is not None else None
        raw_version = result.pop("raw_version_id", None)
        raw_position = result.pop("raw_position", None)
        raw_pointer = result.pop("raw_json_pointer", None)
        result["raw_reference"] = (
            {"version_id": raw_version, "position": raw_position, "json_pointer": raw_pointer}
            if raw_version is not None else None
        )
        return result

    def get_provenance_overlay(
        self, provider_domain: str, account_id: str, media_type: str, source_item_id: str,
        observation_version_id: str | None = None,
    ) -> dict:
        with self._lock:
            subject = self.lookup_provenance_subject(provider_domain, account_id, media_type, source_item_id)
            if subject is None:
                return {"subject": None, "fields": {}}
            fields = {}
            observation_filter = " AND raw_version_id=?" if observation_version_id is not None else ""
            observation_args = (
                (subject["id"], observation_version_id)
                if observation_version_id is not None else (subject["id"],)
            )
            names = {
                row[0] for row in self._db.execute(
                    f"SELECT field_name FROM provenance_values WHERE subject_id=?{observation_filter} "  # noqa: S608
                    "UNION SELECT field_name FROM provenance_overrides WHERE subject_id=?",
                    (*observation_args, subject["id"]),
                )
            }
            for field in sorted(names):
                observation = self._db.execute(
                    f"SELECT * FROM provenance_values WHERE subject_id=? AND field_name=?{observation_filter} "  # noqa: S608
                    "ORDER BY fetched_at DESC,created_at DESC,id DESC LIMIT 1",
                    (subject["id"], field, *((observation_version_id,) if observation_version_id is not None else ())),
                ).fetchone()
                override = self._db.execute(
                    """SELECT * FROM provenance_overrides WHERE subject_id=? AND field_name=?
                    ORDER BY revision DESC LIMIT 1""",
                    (subject["id"], field),
                ).fetchone()
                observed = self._decoded_provenance_row(observation)
                override_value = self._decoded_provenance_row(override)
                active = override_value is not None and override_value["action"] == "set"
                effective = override_value if active else observed
                fields[field] = {
                    "revision": override_value["revision"] if override_value else 0,
                    "observation": observed,
                    "override": override_value if active else None,
                    "effective": effective,
                }
            return {"subject": subject, "fields": fields}

    def _change_provenance_override(
        self, provider_domain: str, account_id: str, media_type: str, source_item_id: str,
        field_name: str, expected_revision: int, actor_id: str, *, clear: bool, value=None
    ) -> dict:
        if (not isinstance(field_name, str) or not field_name.strip() or len(field_name) > 128
                or type(expected_revision) is not int or expected_revision < 0
                or not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 256):
            raise ValueError("Valid field, actor, and expected revision are required")
        with self._transaction():
            subject = self._ensure_provenance_subject(provider_domain, account_id, media_type, source_item_id)
            row = self._db.execute(
                "SELECT max(revision) FROM provenance_overrides WHERE subject_id=? AND field_name=?",
                (subject["id"], field_name),
            ).fetchone()
            revision = row[0] or 0
            if revision != expected_revision:
                raise ValueError("Provenance override revision conflict")
            value_type, value_json = (None, None) if clear else self._typed_value(value)
            self._db.execute(
                """INSERT INTO provenance_overrides
                (id,subject_id,field_name,action,revision,actor_id,value_type,value_json,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), subject["id"], field_name, "clear" if clear else "set",
                 revision + 1, actor_id, value_type, value_json, _now()),
            )
            return self.get_provenance_overlay(provider_domain, account_id, media_type, source_item_id)

    def set_provenance_override(
        self, provider_domain: str, account_id: str, media_type: str, source_item_id: str,
        field_name: str, value, expected_revision: int, actor_id: str
    ) -> dict:
        return self._change_provenance_override(
            provider_domain, account_id, media_type, source_item_id, field_name,
            expected_revision, actor_id, clear=False, value=value
        )

    def clear_provenance_override(
        self, provider_domain: str, account_id: str, media_type: str, source_item_id: str,
        field_name: str, expected_revision: int, actor_id: str
    ) -> dict:
        return self._change_provenance_override(
            provider_domain, account_id, media_type, source_item_id, field_name,
            expected_revision, actor_id, clear=True
        )

    def get_version_provenance_overlay(self, version_id: str) -> dict:
        version = self.get_version(version_id)
        subscription = self.get_subscription(version["subscription_id"])
        occurrences = []
        for occurrence in version["occurrences"]:
            source_item_id = occurrence.get("source_item_id") if occurrence.get("state") == "track" else None
            occurrences.append({
                "position": occurrence.get("position"),
                "source_item_id": source_item_id,
                "provenance": self.get_provenance_overlay(
                    subscription["provider_domain"], subscription["account_id"], "track", source_item_id
                ) if source_item_id else None,
            })
        return {"version_id": version_id, "content_digest": version["content_digest"], "occurrences": occurrences}

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

    def approve_match_candidates(
        self,
        provider_domain: str,
        account_id: str,
        approvals: list[dict],
        actor_id: str | None,
        operation_id: str,
        version_id: str,
    ) -> dict:
        """Atomically apply an idempotent, bounded group of explicit approvals."""
        if not isinstance(approvals, list) or not 1 <= len(approvals) <= 200:
            raise ValueError("Bulk approval requires 1..200 selected candidates")
        canonical = []
        seen: set[str] = set()
        for item in approvals:
            if not isinstance(item, dict):
                raise ValueError("Bulk approval entries are invalid or duplicated")
            source_item_id = item.get("source_item_id")
            asset_id = item.get("asset_id")
            expected_revision = item.get("expected_revision")
            if (
                not isinstance(source_item_id, str)
                or source_item_id in seen
                or not isinstance(asset_id, str)
                or type(expected_revision) is not int
                or expected_revision < 0
            ):
                raise ValueError("Bulk approval entries are invalid or duplicated")
            seen.add(source_item_id)
            canonical.append((source_item_id, asset_id, expected_revision))
        canonical.sort()
        request_digest = _digest(
            [provider_domain, account_id, version_id]
            + [json.dumps(item, separators=(",", ":"), ensure_ascii=False) for item in canonical]
        )
        with self._transaction():
            previous = self._db.execute(
                "SELECT request_digest,response_json FROM bulk_match_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if previous is not None:
                if previous["request_digest"] != request_digest:
                    raise ValueError("Bulk approval operation ID was already used for another request")
                result = json.loads(previous["response_json"])
                result["idempotent_replay"] = True
                return result
            sources = []
            for source_item_id, asset_id, expected_revision in canonical:
                source = self._db.execute(
                    """SELECT * FROM match_sources WHERE provider_domain=? AND account_id=?
                    AND media_type='track' AND source_item_id=?""",
                    (provider_domain, account_id, source_item_id),
                ).fetchone()
                if source is None or source["revision"] != expected_revision:
                    raise ValueError("Match decision revision conflict")
                if not self._db.execute(
                    "SELECT 1 FROM match_candidates WHERE source_id=? AND asset_id=? AND active=1",
                    (source["id"], asset_id),
                ).fetchone():
                    raise ValueError("Decision asset is not a current candidate")
                sources.append((source, asset_id))
            now = _now()
            evidence_json = self._json_object(
                {
                    "kind": "explicit_bulk_user_review",
                    "version_id": version_id,
                    "operation_id": operation_id,
                    "request_digest": request_digest,
                },
                "Decision evidence",
            )
            for source, asset_id in sources:
                revision = source["revision"] + 1
                self._db.execute(
                    """INSERT INTO match_decisions
                    (id,source_id,asset_id,action,revision,actor_id,evidence_json,algorithm_version,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()), source["id"], asset_id, "approve", revision,
                        actor_id, evidence_json, "ma-merged-mapping-v1", now,
                    ),
                )
                self._db.execute(
                    "UPDATE match_sources SET revision=?,approved_asset_id=?,updated_at=? WHERE id=?",
                    (revision, asset_id, now, source["id"]),
                )
            result = {
                "operation_id": operation_id,
                "approved_count": len(sources),
                "idempotent_replay": False,
                "matches": [
                    self.get_match_overlay(provider_domain, account_id, "track", source["source_item_id"])
                    for source, _ in sources
                ],
            }
            response_json = self._bounded_json(
                result, "Bulk approval response", dict, limit=2 * 1024 * 1024
            )
            self._db.execute(
                "INSERT INTO bulk_match_operations VALUES (?,?,?,?)",
                (operation_id, request_digest, response_json, now),
            )
            return result

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

    def get_playback_policy(self, subscription_id: str) -> dict:
        with self._lock:
            self.get_subscription(subscription_id)
            row = self._db.execute(
                "SELECT * FROM playback_policies WHERE subscription_id=?", (subscription_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Playback policy is missing")
            return dict(row)

    def set_playback_policy(self, subscription_id: str, mode: str, expected_revision: int, actor_id: str | None = None) -> dict:
        if mode not in ("prefer_local", "local_only", "prefer_spotify"):
            raise ValueError("Playback mode must be prefer_local, local_only or prefer_spotify")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected playback policy revision must be a nonnegative integer")
        with self._transaction():
            policy = self.get_playback_policy(subscription_id)
            if policy["revision"] != expected_revision:
                raise ValueError("Playback policy revision conflict")
            self._db.execute(
                "UPDATE playback_policies SET mode=?,revision=revision+1,actor_id=?,updated_at=? WHERE subscription_id=?",
                (mode, actor_id, _now(), subscription_id),
            )
            return self.get_playback_policy(subscription_id)

    def get_playback_projection(self, subscription_id: str) -> dict | None:
        with self._lock:
            self.get_subscription(subscription_id)
            row = self._db.execute(
                "SELECT * FROM playback_projections WHERE subscription_id=?", (subscription_id,)
            ).fetchone()
            return dict(row) if row else None

    def prepare_playback_projection(self, subscription_id: str, version_id: str, policy_revision: int,
                                    projection_digest: str, source_count: int, projected_count: int,
                                    gaps_json: str) -> dict:
        if not isinstance(projection_digest, str) or len(projection_digest) != 64:
            raise ValueError("Expected SHA-256 projection digest")
        gaps = json.loads(gaps_json)
        if not isinstance(gaps, list) or type(source_count) is not int or type(projected_count) is not int \
                or not 0 <= projected_count <= source_count:
            raise ValueError("Invalid playback projection gaps or counts")
        normalized = json.dumps(gaps, sort_keys=True, ensure_ascii=False, allow_nan=False)
        with self._transaction():
            version = self.get_version(version_id)
            policy = self.get_playback_policy(subscription_id)
            if version["subscription_id"] != subscription_id or version["total"] != source_count:
                raise ValueError("Playback projection does not match subscription archive")
            if policy["revision"] != policy_revision:
                raise ValueError("Playback policy revision conflict")
            existing = self.get_playback_projection(subscription_id)
            now = _now()
            destination = (existing["destination_item_id"], existing["destination_provider_instance"]) if existing else (None, None)
            if existing and existing["state"] in ("writing", "uncertain"):
                raise ValueError("Playback projection requires reconciliation")
            self._db.execute("""INSERT INTO playback_projections
                (subscription_id,version_id,policy_revision,projection_digest,source_count,projected_count,gaps_json,
                 destination_item_id,destination_provider_instance,state,created_at,updated_at,error)
                VALUES (?,?,?,?,?,?,?,?,?,'prepared',?,?,NULL)
                ON CONFLICT(subscription_id) DO UPDATE SET version_id=excluded.version_id,
                 policy_revision=excluded.policy_revision,projection_digest=excluded.projection_digest,
                 source_count=excluded.source_count,projected_count=excluded.projected_count,gaps_json=excluded.gaps_json,
                 state='prepared',updated_at=excluded.updated_at,error=NULL""",
                (subscription_id, version_id, policy_revision, projection_digest, source_count, projected_count,
                 normalized, *destination, now, now),
            )
            return self.get_playback_projection(subscription_id)

    def mark_playback_projection_writing(self, subscription_id: str) -> None:
        with self._transaction():
            row = self.get_playback_projection(subscription_id)
            if row is None or row["state"] != "prepared":
                raise ValueError("Playback projection is not prepared")
            self._db.execute("UPDATE playback_projections SET state='writing',updated_at=? WHERE subscription_id=?",
                             (_now(), subscription_id))

    def commit_playback_projection(self, subscription_id: str, destination_item_id: str,
                                   destination_provider_instance: str, verified_digest: str,
                                   destination_content_digest: str) -> dict:
        destination_content_digest = self._sha256(destination_content_digest, "destination_content_digest")
        with self._transaction():
            row = self.get_playback_projection(subscription_id)
            if row is None or row["state"] not in ("writing", "uncertain"):
                raise ValueError("Playback projection is not being written")
            if verified_digest != row["projection_digest"]:
                raise ValueError("Playback destination digest does not match preview")
            self._db.execute("""UPDATE playback_projections SET state='applied',destination_item_id=?,
                destination_provider_instance=?,destination_content_digest=?,updated_at=?,error=NULL
                WHERE subscription_id=?""",
                (destination_item_id, destination_provider_instance, destination_content_digest, _now(), subscription_id))
            return self.get_playback_projection(subscription_id)

    def fail_playback_projection(self, subscription_id: str, error: str, uncertain: bool = False) -> None:
        with self._transaction():
            row = self.get_playback_projection(subscription_id)
            if row is None:
                raise ValueError("Playback projection is missing")
            state = "uncertain" if uncertain or row["state"] in ("writing", "uncertain") else "failed"
            self._db.execute("UPDATE playback_projections SET state=?,updated_at=?,error=? WHERE subscription_id=?",
                             (state, _now(), error, subscription_id))

    def detach_playback_projection(self, subscription_id: str, expected_destination_item_id: str,
                                   expected_content_digest: str | None) -> dict:
        """Forget ownership of a destination without deleting the user's playlist."""
        if not isinstance(expected_destination_item_id, str) or not expected_destination_item_id:
            raise ValueError("Expected destination item ID is required")
        if expected_content_digest is not None:
            expected_content_digest = self._sha256(expected_content_digest, "expected_content_digest")
        with self._transaction():
            row = self.get_playback_projection(subscription_id)
            if row is None or row["state"] in ("prepared", "writing", "uncertain"):
                raise ValueError("Playback destination cannot be detached while its outcome is unresolved")
            if (row["destination_item_id"] != expected_destination_item_id
                    or row["destination_content_digest"] != expected_content_digest):
                raise ValueError("Playback destination changed; refresh before detaching")
            self._db.execute("DELETE FROM playback_projections WHERE subscription_id=?", (subscription_id,))
            return row

    def get_mirror(self, subscription_id: str) -> dict | None:
        with self._lock:
            self.get_subscription(subscription_id)
            row = self._db.execute(
                "SELECT * FROM maintained_mirrors WHERE subscription_id=?", (subscription_id,)
            ).fetchone()
            return dict(row) if row else None

    def recover_mirror_pending(self) -> tuple[int, int]:
        """After restart, never retry a mirror write whose external outcome is unknown."""
        with self._transaction():
            now = _now()
            prepared = self._db.execute("""UPDATE maintained_mirrors SET state='failed',
                error='Mirror update interrupted before writing',updated_at=?
                WHERE state='prepared'""", (now,)).rowcount
            writing = self._db.execute("""UPDATE maintained_mirrors SET state='uncertain',
                error='Mirror write interrupted; inspect destination before retrying',updated_at=?
                WHERE state='writing'""", (now,)).rowcount
            return prepared, writing

    def configure_mirror(self, subscription_id: str, enabled: bool, allow_partial: bool,
                         expected_revision: int) -> dict:
        if type(enabled) is not bool or type(allow_partial) is not bool:
            raise ValueError("Mirror settings must be boolean")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Expected mirror revision must be a nonnegative integer")
        with self._transaction():
            current = self.get_mirror(subscription_id)
            if (current["revision"] if current else 0) != expected_revision:
                raise ValueError("Mirror configuration revision conflict")
            if current and current["state"] in ("writing", "uncertain"):
                raise ValueError("Mirror write outcome requires reconciliation")
            if current and current["state"] == "conflict" and enabled:
                raise ValueError("Detach the edited mirror destination before enabling a new one")
            now = _now()
            if current is None:
                self._db.execute("""INSERT INTO maintained_mirrors
                    (subscription_id,revision,enabled,allow_partial,state,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?)""",
                    (subscription_id, 1, int(enabled), int(allow_partial),
                     "pending" if enabled else "disabled", now, now),
                )
            else:
                reset = current["state"] == "detached" and enabled
                self._db.execute("""UPDATE maintained_mirrors SET revision=revision+1,enabled=?,allow_partial=?,
                    state=?,applied_version_id=CASE WHEN ? THEN NULL ELSE applied_version_id END,
                    applied_digest=CASE WHEN ? THEN NULL ELSE applied_digest END,
                    destination_item_id=CASE WHEN ? THEN NULL ELSE destination_item_id END,
                    destination_provider_instance=CASE WHEN ? THEN NULL ELSE destination_provider_instance END,
                    destination_content_digest=CASE WHEN ? THEN NULL ELSE destination_content_digest END,
                    target_version_id=NULL,target_digest=NULL,error=NULL,updated_at=? WHERE subscription_id=?""",
                    (int(enabled), int(allow_partial), "pending" if enabled else "disabled",
                     *([int(reset)] * 5), now, subscription_id),
                )
            return self.get_mirror(subscription_id)

    def prepare_mirror(self, subscription_id: str, version_id: str, target_digest: str) -> dict:
        target_digest = self._sha256(target_digest, "target_digest")
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            version = self.get_version(version_id)
            if version["subscription_id"] != subscription_id:
                raise ValueError("Mirror version belongs to another source")
            if mirror is None or not mirror["enabled"] or mirror["state"] in ("writing", "uncertain", "conflict"):
                raise ValueError("Mirror is not enabled or needs reconciliation")
            self._db.execute("""UPDATE maintained_mirrors SET state='prepared',target_version_id=?,
                target_digest=?,error=NULL,updated_at=? WHERE subscription_id=?""",
                (version_id, target_digest, _now(), subscription_id),
            )
            return self.get_mirror(subscription_id)

    def mark_mirror_writing(self, subscription_id: str) -> None:
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            if mirror is None or mirror["state"] != "prepared" or not mirror["enabled"]:
                raise ValueError("Mirror is not prepared")
            self._db.execute("UPDATE maintained_mirrors SET state='writing',updated_at=? WHERE subscription_id=?",
                             (_now(), subscription_id))

    def commit_mirror(self, subscription_id: str, destination_item_id: str,
                      destination_provider_instance: str, target_digest: str,
                      destination_content_digest: str) -> dict:
        target_digest = self._sha256(target_digest, "target_digest")
        destination_content_digest = self._sha256(destination_content_digest, "destination_content_digest")
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            if mirror is None or mirror["state"] != "writing" or mirror["target_digest"] != target_digest:
                raise ValueError("Mirror write does not match its prepared target")
            self._db.execute("""UPDATE maintained_mirrors SET state='applied',
                applied_version_id=target_version_id,applied_digest=target_digest,
                target_version_id=NULL,target_digest=NULL,destination_item_id=?,
                destination_provider_instance=?,destination_content_digest=?,error=NULL,updated_at=?
                WHERE subscription_id=?""",
                (destination_item_id, destination_provider_instance, destination_content_digest,
                 _now(), subscription_id),
            )
            return self.get_mirror(subscription_id)

    def advance_mirror_unchanged(self, subscription_id: str, version_id: str, target_digest: str) -> dict:
        target_digest = self._sha256(target_digest, "target_digest")
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            version = self.get_version(version_id)
            if (mirror is None or not mirror["enabled"] or mirror["state"] != "applied"
                    or mirror["applied_digest"] != target_digest or version["subscription_id"] != subscription_id):
                raise ValueError("Mirror content or source changed")
            self._db.execute("UPDATE maintained_mirrors SET applied_version_id=?,updated_at=? WHERE subscription_id=?",
                             (version_id, _now(), subscription_id))
            return self.get_mirror(subscription_id)

    def fail_mirror(self, subscription_id: str, error: str, uncertain: bool = False,
                    conflict: bool = False) -> dict:
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            if mirror is None:
                raise ValueError("Mirror is missing")
            state = "conflict" if conflict else "uncertain" if uncertain or mirror["state"] == "writing" else "failed"
            self._db.execute("""UPDATE maintained_mirrors SET state=?,enabled=CASE WHEN ? THEN 0 ELSE enabled END,
                error=?,updated_at=? WHERE subscription_id=?""",
                (state, int(conflict), error[:500], _now(), subscription_id),
            )
            return self.get_mirror(subscription_id)

    def detach_mirror(self, subscription_id: str, expected_destination_item_id: str,
                      expected_content_digest: str | None) -> dict:
        if expected_content_digest is not None:
            expected_content_digest = self._sha256(expected_content_digest, "expected_content_digest")
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            if mirror is None or mirror["state"] in ("writing", "uncertain", "prepared"):
                raise ValueError("Mirror destination has an unresolved write")
            if (not expected_destination_item_id or mirror["destination_item_id"] != expected_destination_item_id
                    or mirror["destination_content_digest"] != expected_content_digest):
                raise ValueError("Mirror destination changed; refresh before detaching")
            self._db.execute("""UPDATE maintained_mirrors SET enabled=0,state='detached',revision=revision+1,
                target_version_id=NULL,target_digest=NULL,error=NULL,updated_at=? WHERE subscription_id=?""",
                (_now(), subscription_id),
            )
            return self.get_mirror(subscription_id)

    def mirror_destination_claimed(self, subscription_id: str, destination_item_id: str) -> bool:
        """Prevent a reconciliation from taking ownership of another managed playlist."""
        with self._lock:
            queries = (
                ("SELECT 1 FROM maintained_mirrors WHERE destination_item_id=? AND subscription_id<>?", True),
                ("SELECT 1 FROM playback_projections WHERE destination_item_id=?", False),
                ("SELECT 1 FROM apply_jobs WHERE destination_item_id=?", False),
                ("SELECT 1 FROM itunes_apply_jobs WHERE destination_item_id=?", False),
            )
            for query, scoped in queries:
                args = (destination_item_id, subscription_id) if scoped else (destination_item_id,)
                if self._db.execute(query, args).fetchone():
                    return True
            return False

    def reconcile_mirror_applied(self, subscription_id: str, expected_revision: int,
                                 expected_target_digest: str, destination_item_id: str,
                                 destination_provider_instance: str, observed_content_digest: str) -> dict:
        expected_target_digest = self._sha256(expected_target_digest, "expected_target_digest")
        observed_content_digest = self._sha256(observed_content_digest, "observed_content_digest")
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            if (mirror is None or mirror["state"] != "uncertain" or mirror["revision"] != expected_revision
                    or mirror["target_digest"] != expected_target_digest or not mirror["target_version_id"]):
                raise ValueError("Mirror reconciliation checkpoint changed")
            if mirror["destination_item_id"] and mirror["destination_item_id"] != destination_item_id:
                raise ValueError("Mirror has a different recorded destination")
            if self.mirror_destination_claimed(subscription_id, destination_item_id):
                raise ValueError("Playlist is already managed by another operation")
            self._db.execute("""UPDATE maintained_mirrors SET state='applied',
                applied_version_id=target_version_id,applied_digest=target_digest,
                target_version_id=NULL,target_digest=NULL,destination_item_id=?,
                destination_provider_instance=?,destination_content_digest=?,error=NULL,updated_at=?
                WHERE subscription_id=?""",
                (destination_item_id, destination_provider_instance, observed_content_digest,
                 _now(), subscription_id),
            )
            return self.get_mirror(subscription_id)

    def reconcile_mirror_unwritten(self, subscription_id: str, expected_revision: int,
                                   expected_target_digest: str, observed_content_digest: str) -> dict:
        expected_target_digest = self._sha256(expected_target_digest, "expected_target_digest")
        observed_content_digest = self._sha256(observed_content_digest, "observed_content_digest")
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            if (mirror is None or mirror["state"] != "uncertain" or mirror["revision"] != expected_revision
                    or mirror["target_digest"] != expected_target_digest or not mirror["destination_item_id"]
                    or mirror["destination_content_digest"] != observed_content_digest):
                raise ValueError("Mirror's previous verified destination changed")
            self._db.execute("""UPDATE maintained_mirrors SET state='failed',target_version_id=NULL,
                target_digest=NULL,error='Previous destination content verified; update may be retried',updated_at=?
                WHERE subscription_id=?""", (_now(), subscription_id))
            return self.get_mirror(subscription_id)

    def abandon_uncertain_mirror(self, subscription_id: str, expected_revision: int,
                                 expected_target_digest: str) -> dict:
        expected_target_digest = self._sha256(expected_target_digest, "expected_target_digest")
        with self._transaction():
            mirror = self.get_mirror(subscription_id)
            if (mirror is None or mirror["state"] != "uncertain" or mirror["revision"] != expected_revision
                    or mirror["target_digest"] != expected_target_digest):
                raise ValueError("Mirror uncertain checkpoint changed")
            self._db.execute("""UPDATE maintained_mirrors SET state='detached',enabled=0,
                revision=revision+1,target_version_id=NULL,target_digest=NULL,
                error='Uncertain destination was explicitly abandoned; inspect for an orphan playlist',
                updated_at=? WHERE subscription_id=?""", (_now(), subscription_id))
            return self.get_mirror(subscription_id)

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
            self._db.execute(
                "INSERT OR IGNORE INTO playback_policies(subscription_id,updated_at) VALUES (?,?)", (result["id"], _now())
            )
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

    def diagnostics(self, recent_limit: int = 20) -> dict:
        """Return bounded aggregate health data without archive identities or payloads."""
        if type(recent_limit) is not int or not 1 <= recent_limit <= 100:
            raise ValueError("Recent diagnostics limit must be 1..100")
        count_tables = (
            "subscriptions", "jobs", "versions", "occurrences", "apply_jobs",
            "sync_jobs", "local_assets", "local_asset_locations", "match_sources",
            "match_candidates", "match_decisions", "playback_policies",
            "playback_projections", "provenance_subjects", "provenance_values",
            "provenance_overrides", "itunes_import_documents", "itunes_import_batches",
            "bulk_match_operations",
        )
        queue_tables = {
            "capture": ("jobs", "state"),
            "sync": ("sync_jobs", "state"),
            "apply": ("apply_jobs", "state"),
            "playback": ("playback_projections", "state"),
            "itunes_import": ("itunes_import_batches", "status"),
        }
        timestamp_columns = {
            "jobs": ("created_at", "finished_at"),
            "sync_jobs": ("created_at", "finished_at"),
            "apply_jobs": ("created_at", "updated_at"),
            "playback_projections": ("created_at", "updated_at"),
            "itunes_import_batches": ("created_at", "updated_at"),
        }
        with self._lock:
            counts = {
                table: self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - fixed table allowlist
                for table in count_tables
            }
            queues: dict[str, dict[str, int]] = {}
            recent = []
            for kind, (table, state_column) in queue_tables.items():
                queues[kind] = {
                    row[0]: row[1]
                    for row in self._db.execute(
                        f"SELECT {state_column},COUNT(*) FROM {table} "  # noqa: S608 - fixed allowlist
                        f"GROUP BY {state_column} ORDER BY {state_column}"
                    )
                }
                created_column, finished_column = timestamp_columns[table]
                recent.extend(
                    {
                        "kind": kind,
                        "state": row[0],
                        "created_at": row[1],
                        "updated_at": row[2],
                    }
                    for row in self._db.execute(
                        f"SELECT {state_column},{created_column},{finished_column} FROM {table} "  # noqa: S608 - fixed allowlist
                        f"ORDER BY {created_column} DESC LIMIT ?",
                        (recent_limit,),
                    )
                )
            recent.sort(key=lambda item: item["created_at"], reverse=True)
            page_size = self._db.execute("PRAGMA page_size").fetchone()[0]
            page_count = self._db.execute("PRAGMA page_count").fetchone()[0]
            return {
                "schema_version": self._db.execute("PRAGMA user_version").fetchone()[0],
                "counts": counts,
                "queues": queues,
                "database_bytes": page_size * page_count,
                "recent_jobs": recent[:recent_limit],
                "match_review": {
                    "approved_sources": self._db.execute(
                        "SELECT COUNT(*) FROM match_sources WHERE approved_asset_id IS NOT NULL"
                    ).fetchone()[0],
                    "recent_decisions": [
                        {"action": row[0], "created_at": row[1]}
                        for row in self._db.execute(
                            "SELECT action,created_at FROM match_decisions ORDER BY created_at DESC,id DESC LIMIT ?",
                            (recent_limit,),
                        )
                    ],
                },
            }

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

    @staticmethod
    def verify_backup(source: str | Path) -> dict:
        """Verify a completed archive backup without opening the live store."""
        source = Path(source)
        manifest_path = source.with_suffix(source.suffix + ".manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or (
            not isinstance(manifest.get("store_uuid"), str)
            or not manifest["store_uuid"]
            or type(manifest.get("schema_version")) is not int
            or type(manifest.get("size")) is not int
            or not isinstance(manifest.get("sha256"), str)
            or len(manifest["sha256"]) != 64
        ):
            raise ValueError("Invalid archive backup manifest")
        if manifest["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported archive backup schema version")
        ArchiveStore._verify_backup_file(source, manifest)
        return manifest

    @staticmethod
    def stage_restore(source: str | Path, destination: str | Path) -> dict:
        """Copy a verified backup into a new staging path without replacing live data."""
        source = Path(source)
        destination = Path(destination)
        manifest = ArchiveStore.verify_backup(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with source.open("rb") as original, destination.open("xb") as staged:
                created = True
                shutil.copyfileobj(original, staged, length=1024 * 1024)
                staged.flush()
                os.fsync(staged.fileno())
            ArchiveStore._verify_backup_file(destination, manifest)
        except Exception:
            if created:
                destination.unlink(missing_ok=True)
            raise
        return manifest

    @staticmethod
    def _verify_backup_file(path: Path, manifest: dict) -> None:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        if size != manifest["size"] or digest.hexdigest() != manifest["sha256"]:
            raise ValueError("Archive backup hash or size mismatch")
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Archive backup integrity check failed")
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("Archive backup foreign key check failed")
                if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                    raise ValueError("Archive backup schema version mismatch")
                metadata = dict(db.execute("SELECT key,value FROM metadata"))
                schema_digest = _digest(
                    [row[0] for row in db.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")]
                )
                if metadata.get("store_uuid") != manifest["store_uuid"] or metadata.get("schema_digest") != schema_digest:
                    raise ValueError("Archive backup identity or schema mismatch")
                for version_id, total, expected in db.execute("SELECT id,total,content_digest FROM versions"):
                    payloads = [
                        row[0] for row in db.execute(
                            "SELECT payload FROM occurrences WHERE version_id=? ORDER BY position", (version_id,)
                        )
                    ]
                    if len(payloads) != total or _digest(payloads) != expected:
                        raise ValueError("Archive backup version content digest mismatch")
        except sqlite3.DatabaseError as error:
            raise ValueError("Archive backup database is invalid") from error
