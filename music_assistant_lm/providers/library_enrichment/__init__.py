"""Selected Spotify metadata archives, isolated from the MA library database.

Compatibility is deliberately limited to the inspected server release. Archives
preserve references and occurrences; they are not downloaded audio or MA mirrors.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    get_authenticated_user,
    get_current_user,
    has_scope,
    impersonated_user,
)
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.auth import Scope
from music_assistant_models.background_task import TaskSchedule
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError

from .itunes_xml import ITunesXMLImportError, inspect_itunes_xml
from .itunes_zip import ITunesZipImportError, inspect_itunes_zip
from .spotify import SpotifyCaptureError, capture_playlist, preview_playlist
from .store import ArchiveStore

SUPPORTED_SERVER = "2.10.4"
MAX_ITUNES_ZIP_UPLOAD_BYTES = 15 * 1024 * 1024


async def setup(mass, manifest, config):
    """Construct the provider using the standard MA lifecycle."""
    return LibraryEnrichmentProvider(mass, manifest, config)


class LibraryEnrichmentProvider(PluginProvider):
    """Admin-operated selected captures and review overlays; no remote mutation."""

    async def handle_async_init(self) -> None:
        if version("music-assistant") != SUPPORTED_SERVER:
            raise InvalidDataError("Library Enrichment requires compatibility review for this server version")
        self._handles = []
        self._jobs: set[str] = set()
        self._stopping_jobs: set[str] = set()
        self._closing = False
        self._sync_jobs: dict[str, str] = {}
        self._sync_stopping: set[str] = set()
        self._sync_dispatcher_id = "library_enrichment_sync_dispatcher"
        self._write_lock = asyncio.Lock()
        self._store = await asyncio.to_thread(ArchiveStore, Path(self.mass.storage_path) / "library_enrichment" / "enrichment.db")
        self._itunes_import_root = Path(self.mass.storage_path) / "library_enrichment" / "imports"
        await asyncio.to_thread(self._itunes_import_root.mkdir, parents=True, exist_ok=True)
        self._itunes_zip_root = Path("/media/music-assistant-imports")
        await asyncio.to_thread(self._store.recover_pending)
        await asyncio.to_thread(self._store.recover_sync_pending)

    async def loaded_in_mass(self) -> None:
        for command, handler in (
            ("capabilities", self.capabilities),
            ("inspect", self.inspect),
            ("preview", self.preview),
            ("sources", self.sources),
            ("capture", self.capture),
            ("status", self.status),
            ("version", self.archive_version),
            ("versions", self.archive_versions),
            ("provenance", self.provenance),
            ("item_provenance", self.item_provenance),
            ("itunes_inspect", self.itunes_inspect),
            ("itunes_preview", self.itunes_preview),
            ("cancel", self.cancel),
            ("apply_preview", self.apply_preview),
            ("apply", self.apply),
            ("apply_status", self.apply_status),
            ("sync_policy", self.sync_policy),
            ("set_sync_policy", self.set_sync_policy),
            ("sync_now", self.sync_now),
            ("sync_status", self.sync_status),
            ("match_review", self.match_review),
            ("set_match_decision", self.set_match_decision),
            ("playback_policy", self.playback_policy),
            ("set_playback_policy", self.set_playback_policy),
            ("playback_preview", self.playback_preview),
            ("playback_apply", self.playback_apply),
            ("playback_status", self.playback_status),
        ):
            self._handles.append(
                self.mass.register_api_command(f"library_enrichment/{command}", handler, required_scope=Scope.CONFIG_PROVIDERS_WRITE)
            )
        self._handles.append(
            self.mass.webserver.register_dynamic_route(
                "/library-enrichment/itunes-upload", self._itunes_zip_upload, method="POST"
            )
        )
        self.mass.tasks.register_scheduled_task(
            task_id=self._sync_dispatcher_id, name="Check selected archived Spotify playlists",
            handler=self._dispatch_sync, schedule=TaskSchedule.hourly(every=1), initial_delay=60,
            metadata={"task_domain": "library_enrichment_sync"}, allow_retry=False, allow_cancel=True,
        )

    async def unload(self, is_removed: bool = False) -> None:
        self._closing = True
        for handle in self._handles:
            handle()
        async with self._write_lock:
            for task_id in (self._sync_dispatcher_id, *tuple(self._sync_jobs.values())):
                if task_id in self._sync_stopping:
                    raise InvalidDataError("Playlist sync is still stopping; archive store left open")
                stopped = await self.mass.tasks.unregister_scheduled_task_and_wait(task_id, clear_persisted_state=False)
                if not stopped:
                    self._sync_stopping.add(task_id)
                    raise InvalidDataError("Playlist sync is still stopping; archive store left open")
            await self._store_operation(self._store.recover_sync_pending)
            for job_id in tuple(self._jobs):
                if job_id in self._stopping_jobs:
                    raise InvalidDataError("Capture is still stopping; archive store left open")
                stopped = await self.mass.tasks.unregister_scheduled_task_and_wait(f"library_enrichment_{job_id}")
                if not stopped:
                    self._stopping_jobs.add(job_id)
                    raise InvalidDataError("Capture is still stopping; archive store left open")
            await asyncio.to_thread(self._store.recover_pending)
            await asyncio.to_thread(self._store.close)
        # Retain the independent archive on provider removal. Never delete history implicitly.

    @staticmethod
    def _authorize():
        user = get_current_user()
        if user is None or not has_scope(user, Scope.CONFIG_PROVIDERS_WRITE):
            raise InsufficientPermissions("Archive administration requires provider configuration permission")
        return user

    def _provider(self, instance_id: str):
        self._authorize()
        for provider in self.mass.music.providers:
            if provider.instance_id == instance_id and provider.domain == "spotify" and provider.available:
                return provider
        raise InvalidDataError("Select an available Spotify provider instance accessible to this user")

    async def capabilities(self) -> dict[str, Any]:
        self._authorize()
        return {
            "api_version": 1,
            "server_version": version("music-assistant"),
            "frontend_version": version("music-assistant-frontend"),
            "supported_server": SUPPORTED_SERVER,
            "selected_capture": True,
            "source_listing": True,
            "preview_preconditions": True,
            "version_listing": True,
            "provenance_read": True,
            "provenance_api_version": 1,
            "max_provenance_page": 200,
            "raw_payload_inline": False,
            "item_provenance": True,
            "item_provenance_api_version": 1,
            "itunes_import": True,
            "itunes_import_api_version": 1,
            "itunes_apply": False,
            "itunes_zip_packages": True,
            "itunes_zip_api_version": 1,
            "itunes_zip_upload": True,
            "itunes_zip_upload_api_version": 1,
            "max_itunes_zip_upload_bytes": MAX_ITUNES_ZIP_UPLOAD_BYTES,
            "itunes_source_modes": ["staged_server_path", "staged_zip"],
            "itunes_import_directory": str(self._itunes_import_root),
            "itunes_zip_directory": str(self._itunes_zip_root),
            "max_itunes_preview_page": 200,
            "inspection": "library_only_no_refresh",
            "mirror_apply": False,
            "archive_apply": True,
            "apply_api_version": 1,
            "subscription_sync": True,
            "sync_policy_api_version": 1,
            "interval_bounds": {"min": 3600, "max": 604800},
            "local_matching": True,
            "match_review_api_version": 1,
            "max_match_review_page": 200,
            "playback_policy": True,
            "playback_policy_api_version": 1,
            "playback_policy_modes": ["prefer_local", "local_only", "prefer_spotify"],
            "playback_strict_signal": "#EXTPROV:local_only||<provider-instance>",
            "liked_songs": False,
            "audio_backup": False,
            "max_items": 10000,
            "access": "provider_configuration_administrators",
        }

    async def _itunes_zip_upload(self, request: Any) -> Any:
        """Accept one small XML-only transport ZIP through the authenticated web API."""
        from aiohttp import web

        user = await get_authenticated_user(request)
        if user is None:
            return web.json_response({"error": "authentication_required"}, status=401)
        if not has_scope(user, Scope.CONFIG_PROVIDERS_WRITE):
            return web.json_response({"error": "insufficient_permissions"}, status=403)
        raw_name = request.headers.get("X-Filename", "iTunes Library.zip").strip()
        filename = Path(raw_name).name
        if (
            not filename or filename != raw_name or len(filename) > 160
            or Path(filename).suffix.casefold() != ".zip"
        ):
            return web.json_response({"error": "invalid_zip_filename"}, status=400)
        declared = request.content_length
        if declared is not None and (declared < 1 or declared > MAX_ITUNES_ZIP_UPLOAD_BYTES):
            return web.json_response({"error": "zip_upload_size_limit"}, status=413)
        try:
            await asyncio.to_thread(self._itunes_zip_root.mkdir, parents=True, exist_ok=True)
        except OSError:
            return web.json_response({"error": "itunes_media_staging_unavailable"}, status=503)
        data = bytearray()
        async for chunk in request.content.iter_chunked(1024 * 1024):
            data.extend(chunk)
            if len(data) > MAX_ITUNES_ZIP_UPLOAD_BYTES:
                return web.json_response({"error": "zip_upload_size_limit"}, status=413)
        if not data:
            return web.json_response({"error": "empty_zip_upload"}, status=400)
        digest = hashlib.sha256(data).hexdigest()
        destination = self._itunes_zip_root / f"itunes-library-{digest[:12]}.zip"
        temporary = self._itunes_zip_root / f".{destination.name}.{uuid.uuid4().hex}.part"
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            package = await asyncio.to_thread(inspect_itunes_zip, temporary)
            if package["package"]["media_files_total"]:
                raise ITunesZipImportError("Upload ZIP must contain the iTunes XML only; media stays in Music Assistant")
            if destination.exists():
                if await asyncio.to_thread(self._sha256_file, destination) != digest:
                    raise ITunesZipImportError("Staged ZIP name conflicts with different content")
                temporary.unlink(missing_ok=True)
            else:
                temporary.replace(destination)
        except (OSError, ITunesZipImportError) as err:
            temporary.unlink(missing_ok=True)
            return web.json_response({"error": "invalid_itunes_zip", "detail": str(err)}, status=400)
        return web.json_response(
            {"api_version": 1, "library_path": str(destination), "source_digest": digest,
             "package": package["package"]}
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while block := handle.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    def _itunes_source_path(self, library_path: str) -> Path:
        if not isinstance(library_path, str) or not library_path.strip():
            raise InvalidDataError("Select an XML file in the configured iTunes import directory")
        requested = Path(library_path.strip())
        if not requested.is_absolute():
            requested = self._itunes_import_root / requested
        try:
            resolved = requested.resolve(strict=True)
            root = self._itunes_import_root.resolve(strict=True)
        except OSError as err:
            raise InvalidDataError("The staged iTunes XML file is not readable") from err
        allowed_roots = [root]
        if resolved.suffix.casefold() == ".zip":
            try:
                allowed_roots.append(self._itunes_zip_root.resolve(strict=True))
            except OSError:
                pass
        if (
            not any(resolved.is_relative_to(candidate) for candidate in allowed_roots)
            or resolved.suffix.casefold() not in (".xml", ".zip")
            or not resolved.is_file()
        ):
            raise InvalidDataError("iTunes imports require a staged XML or ZIP file inside an advertised directory")
        return resolved

    def _inspect_itunes_source(
        self, source: Path, path_mappings: list[dict] | None = None,
        xml_member_path: str | None = None, localization_root: str = "iTunes Imported",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        if source.suffix.casefold() == ".xml":
            return inspect_itunes_xml(source, path_mappings=path_mappings), None
        package = inspect_itunes_zip(
            source, xml_member_path=xml_member_path, localization_root=localization_root,
        )
        try:
            with zipfile.ZipFile(source) as archive:
                info = archive.getinfo(package["package"]["selected_xml_path"])
                if info.file_size > 128 * 1024 * 1024:
                    raise ITunesZipImportError("Selected iTunes XML exceeds the supported size limit")
                xml_data = archive.read(info)
        except (OSError, KeyError, RuntimeError, zipfile.BadZipFile) as err:
            raise ITunesZipImportError("Selected iTunes XML cannot be read") from err
        if hashlib.sha256(xml_data).hexdigest() != package["itunes_xml"]["sha256"]:
            raise ITunesZipImportError("Selected iTunes XML changed during package inspection")
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", suffix=".xml", prefix="itunes-zip-", dir=self._itunes_import_root, delete=False,
            ) as temporary:
                temporary.write(xml_data)
                temporary.flush()
                temporary_path = Path(temporary.name)
            parsed = inspect_itunes_xml(temporary_path, path_mappings=path_mappings)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        parsed["source"]["path"] = str(source)
        parsed["source_digest"] = package["archive"]["sha256"]
        parsed["source"] = package["archive"]
        return parsed, package

    @staticmethod
    def _itunes_public_inspection(parsed: dict[str, Any], inspection_id: str, revision: int) -> dict[str, Any]:
        return {
            "api_version": 1,
            "inspection_id": inspection_id,
            "revision": revision,
            "source_digest": parsed["source_digest"],
            "library_path": parsed["source"]["path"],
            "library_persistent_id": parsed["library_persistent_id"],
            "library_date": parsed["library_date"],
            "library_version": parsed["library_version"],
            "tracks_total": parsed["tracks_total"],
            "playlists_total": parsed["playlists_total"],
            "occurrences_total": parsed["occurrence_count"],
            "roots": parsed["roots"],
            "warnings": parsed["warnings"],
            "warnings_truncated": parsed["warnings_truncated"],
            "playlists": [
                {key: playlist.get(key) for key in (
                    "id", "name", "parent_id", "kind", "selectable", "reason", "track_count",
                    "duplicate_occurrence_count",
                )}
                for playlist in parsed["playlists"]
            ],
        }

    async def itunes_inspect(
        self, library_path: str, xml_member_path: str = "", localization_root: str = "iTunes Imported",
    ) -> dict[str, Any]:
        """Inspect one staged legacy XML library without changing the MA library."""
        self._authorize()
        source = self._itunes_source_path(library_path)
        try:
            parsed, package = await asyncio.to_thread(
                self._inspect_itunes_source, source, None, xml_member_path or None, localization_root,
            )
            metadata = {
                "library_path": str(source),
                "source_kind": "zip" if package else "xml",
                "xml_member_path": package["package"]["selected_xml_path"] if package else None,
                "localization_root": localization_root if package else None,
                "library_date": parsed["library_date"],
                "library_version": parsed["library_version"],
                "tracks_total": parsed["tracks_total"],
                "playlists_total": parsed["playlists_total"],
                "occurrences_total": parsed["occurrence_count"],
                "parser_version": parsed["parser_version"],
            }
            staged = await self._store_operation(
                self._store.stage_itunes_import,
                parsed["source_digest"], parsed["library_persistent_id"], metadata, [], [],
            )
        except (ITunesXMLImportError, ITunesZipImportError, ValueError) as err:
            raise InvalidDataError(str(err)) from None
        result = self._itunes_public_inspection(parsed, staged["inspection_id"], staged["revision"])
        result["source_kind"] = "zip" if package else "xml"
        result["package"] = package["package"] if package else None
        result["localization"] = package["localization"] if package else None
        return result

    async def itunes_preview(
        self, inspection_id: str, source_digest: str, path_mappings: list, playlist_ids: list[str],
    ) -> dict[str, Any]:
        """Persist a digest-bound preview plan; never create or update MA items."""
        self._authorize()
        try:
            inspection = await self._read_store(self._store.get_itunes_import, inspection_id)
        except KeyError:
            raise InvalidDataError("The iTunes inspection was not found") from None
        if inspection["source_digest"] != source_digest:
            raise InvalidDataError("The iTunes source digest changed; inspect the library again")
        if not isinstance(path_mappings, list) or not isinstance(playlist_ids, list):
            raise InvalidDataError("Path mappings and playlist IDs must be lists")
        normalized_mappings = []
        for mapping in path_mappings:
            if not isinstance(mapping, dict):
                raise InvalidDataError("Path mappings must be objects")
            source_prefix = mapping.get("source_prefix", mapping.get("source_root"))
            target_prefix = mapping.get("target_prefix", mapping.get("target_root"))
            provider_instance_id = mapping.get("provider_instance_id", "unbound")
            if not all(isinstance(value, str) and value.strip() for value in (source_prefix, target_prefix)):
                raise InvalidDataError("Each path mapping requires nonempty source and target roots")
            if not isinstance(provider_instance_id, str) or not provider_instance_id.strip():
                raise InvalidDataError("A path mapping provider instance must be a nonempty string")
            normalized_mappings.append({
                "source_prefix": source_prefix,
                "target_prefix": target_prefix,
                "provider_instance_id": provider_instance_id,
            })
        source = self._itunes_source_path(inspection["source_metadata"]["library_path"])
        try:
            parsed, package = await asyncio.to_thread(
                self._inspect_itunes_source, source, normalized_mappings,
                inspection["source_metadata"].get("xml_member_path"),
                inspection["source_metadata"].get("localization_root") or "iTunes Imported",
            )
        except (ITunesXMLImportError, ITunesZipImportError) as err:
            raise InvalidDataError(str(err)) from None
        if parsed["source_digest"] != source_digest:
            raise InvalidDataError("The iTunes XML changed after inspection")
        by_id = {playlist["id"]: playlist for playlist in parsed["playlists"]}
        if (
            not all(isinstance(item, str) and item in by_id and by_id[item]["selectable"] for item in playlist_ids)
            or len(playlist_ids) != len(set(playlist_ids))
            or not playlist_ids
        ):
            raise InvalidDataError("Select one or more unique importable playlists")
        selected = [by_id[item] for item in playlist_ids]
        counts = {"matched": 0, "unresolved": 0, "ambiguous": 0, "unsupported": 0}
        for playlist in selected:
            for occurrence in playlist["occurrences"]:
                if occurrence["state"] == "unsupported":
                    counts["unsupported"] += 1
                elif occurrence["state"] != "track" or occurrence["path"]["state"] != "mapped":
                    counts["unresolved"] += 1
                else:
                    counts["matched"] += 1
        preview_seed = json.dumps(
            {
                "source_digest": source_digest,
                "path_mappings": path_mappings,
                "playlist_ids": playlist_ids,
                "snapshots": [playlist["snapshot_id"] for playlist in selected],
                "counts": counts,
            }, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        )
        preview_digest = hashlib.sha256(preview_seed.encode()).hexdigest()
        preview = {
            **counts,
            "selected_playlists": len(selected),
            "source_tracks": parsed["tracks_total"],
            "selected_occurrences": sum(playlist["occurrence_count"] for playlist in selected),
        }
        try:
            staged = await self._store_operation(
                self._store.stage_itunes_import,
                source_digest, parsed["library_persistent_id"], inspection["source_metadata"],
                path_mappings, playlist_ids,
            )
            recorded = await self._store_operation(
                self._store.record_itunes_import_preview,
                staged["inspection_id"], staged["revision"], preview_digest, preview,
            )
        except ValueError as err:
            raise InvalidDataError(str(err)) from None
        return {
            "api_version": 1,
            "inspection_id": recorded["inspection_id"],
            "revision": recorded["revision"],
            "source_digest": source_digest,
            "preview_digest": preview_digest,
            **preview,
        }

    async def inspect(self, media_type: str, library_item_id: str) -> dict[str, Any]:
        """Hydrate a library record directly, never calling refresh-on-access get()."""
        self._authorize()
        if media_type not in ("artist", "album", "track", "playlist"):
            raise InvalidDataError("Inspection supports library artists, albums, tracks and playlists")
        item = await self.mass.music.get_controller(MediaType(media_type)).get_library_item(library_item_id)
        return {"record_kind": "full", "external_ids_loaded": True, "item": item.to_dict()}

    async def preview(self, provider_instance_id: str, source_playlist_id: str, max_items: int = 10000) -> dict[str, Any]:
        """Check one explicit source without writing archive state or importing tracks."""
        try:
            return await preview_playlist(self._provider(provider_instance_id), source_playlist_id, max_items=max_items)
        except SpotifyCaptureError as err:
            raise InvalidDataError(str(err)) from None

    async def sources(self, provider_instance_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Page imported library playlist candidates without contacting Spotify or hydrating."""
        self._provider(provider_instance_id)
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 2147483647:
            raise InvalidDataError("Source page requires limit 1..200 and nonnegative offset")
        rows = await self.mass.music.playlists.library_items(
            provider=provider_instance_id, summary=True, limit=limit + 1, offset=offset, order_by="name", collapse_collections=False
        )
        items, excluded = [], []
        seen = set()
        for row in rows[:limit]:
            base = {"name": row.name, "library_item_id": str(row.item_id)}
            mappings = sorted(
                (
                    mapping
                    for mapping in row.provider_mappings
                    if mapping.provider_instance == provider_instance_id and mapping.provider_domain == "spotify"
                ),
                key=lambda mapping: mapping.item_id,
            )
            if getattr(row, "is_dynamic", False):
                excluded.append({**base, "reason": "dynamic_playlist"})
                continue
            valid = [mapping for mapping in mappings if re.fullmatch(r"[A-Za-z0-9]{22}", mapping.item_id)]
            if not valid:
                excluded.append({**base, "reason": "unsupported_source_identity"})
            for mapping in valid:
                if mapping.item_id not in seen:
                    items.append({**base, "source_playlist_id": mapping.item_id})
                    seen.add(mapping.item_id)
        return {
            "items": items,
            "excluded": excluded,
            "limit": limit,
            "offset": offset,
            "has_more": len(rows) > limit,
            "scope": "imported_library_only",
        }

    async def capture(
        self,
        provider_instance_id: str,
        source_playlist_id: str,
        max_items: int = 10000,
        expected_account_id: str | None = None,
        expected_snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        user = self._authorize()
        preview = await self.preview(provider_instance_id, source_playlist_id, max_items)
        if expected_account_id is not None and preview["account_id"] != expected_account_id:
            raise InvalidDataError("Spotify account changed since preview; preview again")
        if expected_snapshot_id is not None and preview["snapshot_id"] != expected_snapshot_id:
            raise InvalidDataError("Spotify playlist changed since preview; preview again")
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            preparation = asyncio.create_task(asyncio.to_thread(self._prepare_capture, provider_instance_id, source_playlist_id, preview))
            try:
                subscription_id, job_id = await asyncio.shield(preparation)
            except asyncio.CancelledError:
                # to_thread keeps running after cancellation. Keep the lifecycle lock
                # until it returns, then finish the durable job before releasing it.
                try:
                    _, job_id = await preparation
                except Exception:
                    raise asyncio.CancelledError from None
                await asyncio.to_thread(self._store.fail_capture, job_id, "Capture request cancelled before scheduling")
                raise
            except (sqlite3.IntegrityError, ValueError):
                raise InvalidDataError("A capture for this source is already pending") from None
            self._jobs.add(job_id)
            try:
                task = self.mass.tasks.run_background_task(
                    name="Capture selected Spotify playlist",
                    task_id=f"library_enrichment_{job_id}",
                    handler=lambda: self._run_capture(user.user_id, job_id, provider_instance_id, source_playlist_id, preview, max_items),
                    user_id=user.user_id,
                    allow_retry=False,
                    allow_cancel=True,
                    priority=True,
                )
            except Exception:
                await asyncio.to_thread(self._store.fail_capture, job_id, "Unable to schedule capture")
                self._jobs.discard(job_id)
                raise
        return {"job_id": job_id, "subscription_id": subscription_id, "task_id": task.id}

    def _prepare_capture(self, instance_id, playlist_id, preview):
        subscription = self._store.upsert_subscription("spotify", preview["account_id"], playlist_id, instance_id, preview["name"])
        self._store.observe(subscription["id"], preview["snapshot_id"])
        return subscription["id"], self._store.begin_capture(subscription["id"], preview["snapshot_id"])

    async def _run_capture(self, user_id, job_id, instance_id, playlist_id, preview, max_items):
        # MA 2.10.4's queue records user_id but does not restore its context when
        # a worker slot becomes available. Refresh permissions and bind explicitly.
        try:
            user = await self.mass.webserver.auth.get_user(user_id)
        except BaseException:
            await asyncio.to_thread(self._store.fail_capture, job_id, "Unable to verify initiating user")
            self._jobs.discard(job_id)
            self._stopping_jobs.discard(job_id)
            raise
        user_token = current_user.set(user)
        impersonation_token = impersonated_user.set(None)
        try:
            await self._capture(job_id, instance_id, playlist_id, preview, max_items)
        finally:
            impersonated_user.reset(impersonation_token)
            current_user.reset(user_token)

    async def _capture(self, job_id, instance_id, playlist_id, preview, max_items):
        try:
            received = 0

            async def progress(count):
                nonlocal received
                received = max(received, count)
                await asyncio.to_thread(self._store.update_progress, job_id, received, preview["total"])

            capture = await capture_playlist(self._provider(instance_id), playlist_id, max_items=max_items, on_page=progress)
            if capture["account_id"] != preview["account_id"]:
                raise InvalidDataError("Spotify account changed; preview again")
            if capture["snapshot_before"] != preview["snapshot_id"]:
                raise InvalidDataError("Source changed since preview; capture again")
            await asyncio.to_thread(
                self._store.commit_capture,
                job_id,
                snapshot_before=capture["snapshot_before"],
                snapshot_after=capture["snapshot_after"],
                total=capture["total"],
                occurrences=capture["occurrences"],
            )
        except asyncio.CancelledError:
            try:
                await asyncio.to_thread(self._store.fail_capture, job_id, "Capture cancelled")
            except ValueError:
                pass  # A commit already executing in the worker thread wins atomically.
            raise
        except Exception as err:
            # Avoid persisting provider exceptions that could contain URLs or account secrets.
            error = str(err) if isinstance(err, SpotifyCaptureError) else f"Capture failed: {type(err).__name__}"
            await asyncio.to_thread(self._store.fail_capture, job_id, error)
            raise InvalidDataError("Capture failed; previous committed archive retained") from None
        finally:
            self._jobs.discard(job_id)
            self._stopping_jobs.discard(job_id)

    async def status(self) -> dict[str, Any]:
        self._authorize()
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            jobs = await self._store_operation(self._store.list_jobs)
            for job in jobs:
                task = self._capture_task(job["id"])
                job["task_status"] = getattr(getattr(task, "status", None), "value", getattr(task, "status", None))
                started_at = getattr(task, "started_at", None)
                job["task_started_at"] = started_at.isoformat() if started_at is not None else None
                if job["state"] == "pending" and job["id"] in self._jobs and job["task_status"] not in (
                    "idle",
                    "pending",
                    "running",
                ):
                    await self._store_operation(self._store.fail_capture, job["id"], "Capture task stopped before completion")
                    self._jobs.discard(job["id"])
                    self._stopping_jobs.discard(job["id"])
            if any(job["state"] == "pending" and job["task_status"] not in ("idle", "pending", "running") for job in jobs):
                jobs = await self._store_operation(self._store.list_jobs)
                for job in jobs:
                    task = self._capture_task(job["id"])
                    job["task_status"] = getattr(getattr(task, "status", None), "value", getattr(task, "status", None))
                    started_at = getattr(task, "started_at", None)
                    job["task_started_at"] = started_at.isoformat() if started_at is not None else None
            subscriptions = await self._store_operation(self._store.list_subscriptions)
            return {"subscriptions": subscriptions, "jobs": jobs}

    def _capture_task(self, job_id: str):
        try:
            return self.mass.tasks.get_task(f"library_enrichment_{job_id}")
        except InvalidDataError:
            return None

    @staticmethod
    async def _store_operation(method, *args):
        operation = asyncio.create_task(asyncio.to_thread(method, *args))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            try:
                await operation
            finally:
                raise asyncio.CancelledError from None

    async def archive_version(self, version_id: str) -> dict[str, Any]:
        self._authorize()
        return await self._read_store(self._store.get_version, version_id)

    async def archive_versions(self, subscription_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        self._authorize()
        return await self._read_store(self._store.list_versions, subscription_id, limit, offset)

    @staticmethod
    def _spotify_provenance_values(
        version_id: str, occurrence: dict[str, Any], fetched_at: str
    ) -> list[dict[str, Any]]:
        """Parse a fixed, typed subset of an immutable Spotify occurrence."""
        position = occurrence["position"]
        raw = occurrence.get("source_payload")
        item_key = "item" if isinstance(raw, dict) and "item" in raw else "track"
        item = raw.get(item_key) if isinstance(raw, dict) else None
        album = item.get("album") if isinstance(item, dict) else None
        external_ids = item.get("external_ids") if isinstance(item, dict) else None
        added_by = raw.get("added_by") if isinstance(raw, dict) else None
        parser_version = "spotify-archive-v1"

        def observation(field_name, container, key, pointer, expected, *, unit=None, date_precision=None):
            if raw is None:
                state, value = "not_loaded", None
            elif not isinstance(container, dict):
                state, value = "inaccessible", None
            elif key not in container:
                state, value = "missing", None
            else:
                value = container[key]
                if value is None:
                    state = "not_loaded"
                elif value == "" or value == []:
                    state = "empty"
                elif expected(value):
                    state = "value"
                else:
                    state, value = "inaccessible", None
            result = {
                "field_name": field_name,
                "state": state,
                "source": "spotify.playlist",
                "fetched_at": fetched_at,
                "parser_version": parser_version,
                "raw_reference": {"version_id": version_id, "position": position, "json_pointer": pointer},
            }
            if state == "value":
                result["value"] = value
            if unit is not None:
                result["unit"] = unit
            if date_precision is not None and state == "value":
                result["date_precision"] = date_precision(value)
            return result

        def string(value):
            return isinstance(value, str) and bool(value)

        def integer(value):
            return type(value) is int

        def boolean(value):
            return type(value) is bool

        def strings(value):
            return isinstance(value, list) and all(isinstance(item, str) and item for item in value)
        artists = item.get("artists") if isinstance(item, dict) else None
        artist_ids = None
        if isinstance(artists, list):
            artist_ids = [artist.get("id") for artist in artists if isinstance(artist, dict) and isinstance(artist.get("id"), str)]
        artist_container = {"ids": artist_ids} if artists is not None else {}

        def precision(value):
            declared = album.get("release_date_precision") if isinstance(album, dict) else None
            if declared in ("year", "month", "day"):
                return declared
            return "day" if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else "month" if re.fullmatch(r"\d{4}-\d{2}", value) else "year"

        return [
            observation("spotify_track_id", item, "id", f"/{item_key}/id", string),
            observation("spotify_album_id", album, "id", f"/{item_key}/album/id", string),
            observation("spotify_artist_ids", artist_container, "ids", f"/{item_key}/artists", strings),
            observation("isrc", external_ids, "isrc", f"/{item_key}/external_ids/isrc", string),
            observation("duration_ms", item, "duration_ms", f"/{item_key}/duration_ms", integer, unit="ms"),
            observation("explicit", item, "explicit", f"/{item_key}/explicit", boolean),
            observation("popularity", item, "popularity", f"/{item_key}/popularity", integer),
            observation("added_at", raw, "added_at", "/added_at", string),
            observation("added_by_id", added_by, "id", "/added_by/id", string),
            observation("is_local", raw if isinstance(raw, dict) and "is_local" in raw else item, "is_local", "/is_local", boolean),
            observation("album_release_date", album, "release_date", f"/{item_key}/album/release_date", string,
                        date_precision=precision),
        ]

    async def provenance(self, version_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Return persisted typed provenance derived only from immutable archive JSON."""
        self._authorize()
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or not 0 <= offset <= 2**31 - 1:
            raise InvalidDataError("Provenance page requires limit 1..200 and nonnegative offset")
        try:
            version = await self._read_store(self._store.get_version, version_id)
            subscription = await self._read_store(self._store.get_subscription, version["subscription_id"])
        except KeyError:
            raise InvalidDataError("Archive version was not found") from None
        occurrences = version["occurrences"]
        # Persist each source once per request. Duplicate occurrences retain their own
        # raw references in the response while sharing the account-scoped subject.
        persisted: dict[str, dict[str, Any]] = {}
        for occurrence in occurrences:
            source_id = occurrence.get("source_item_id")
            if not isinstance(source_id, str) or not source_id:
                continue
            if source_id not in persisted:
                values = self._spotify_provenance_values(version_id, occurrence, version["created_at"])
                persisted[source_id] = await self._store_operation(
                    self._store.upsert_provenance_values, "spotify", subscription["account_id"], "track", source_id, values
                )
        page = []
        for occurrence in occurrences[offset : offset + limit]:
            source_id = occurrence.get("source_item_id")
            page.append({
                "position": occurrence["position"],
                "state": occurrence.get("state", "invalid"),
                "source_item_id": source_id,
                "provenance": persisted.get(source_id) if isinstance(source_id, str) else None,
            })
        return {
            "api_version": 1,
            "version_id": version_id,
            "subscription_id": version["subscription_id"],
            "items": page,
            "limit": limit,
            "offset": offset,
            "total": len(occurrences),
            "has_more": offset + len(page) < len(occurrences),
            "raw_payload_inline": False,
        }

    async def item_provenance(self, media_type: str = "playlist", library_item_id: str = "") -> dict[str, Any]:
        """Link one builtin playlist destination to its durable archive checkpoints."""
        self._authorize()
        if media_type != "playlist" or not isinstance(library_item_id, str) or not library_item_id:
            raise InvalidDataError("Item provenance supports a nonempty library playlist ID")

        def lookup():
            for subscription in self._store.list_subscriptions():
                archive = (
                    self._store.get_apply_for_version(subscription["applied_version_id"])
                    if subscription.get("applied_version_id")
                    else None
                )
                playback = self._store.get_playback_projection(subscription["id"])
                for kind, destination in (("archive", archive), ("playback", playback)):
                    if destination and str(destination.get("destination_item_id")) == library_item_id:
                        capture_jobs = [
                            job for job in self._store.list_jobs() if job["subscription_id"] == subscription["id"]
                        ]
                        return subscription, kind, destination, self._store.get_sync_status(subscription["id"]), capture_jobs
            return None

        found = await self._read_store(lookup)
        base = {"api_version": 1, "media_type": "playlist", "library_item_id": library_item_id}
        if found is None:
            return {**base, "linked": False, "state": "unknown", "destination": None,
                    "subscription": None, "snapshots": None, "check": None}
        subscription, kind, destination, sync, capture_jobs = found
        jobs = sync["jobs"]
        latest = jobs[-1] if jobs else None
        if any(job["state"] in ("queued", "running") for job in jobs) or any(job["state"] == "pending" for job in capture_jobs):
            state = "capture_pending"
        elif (latest and latest["state"] in ("failed", "cancelled", "interrupted")) or (
            capture_jobs and capture_jobs[-1]["state"] == "failed"
        ):
            state = "capture_failed"
        elif subscription.get("observed_snapshot") and subscription.get("observed_snapshot") != subscription.get("committed_snapshot"):
            state = "source_changed"
        elif subscription.get("committed_version_id"):
            state = "current"
        else:
            state = "unknown"
        check_state = sync["state"]
        return {
            **base,
            "linked": True,
            "state": state,
            "destination": {"kind": kind, "item_id": library_item_id,
                            "provider_instance_id": destination.get("destination_provider_instance"),
                            "version_id": destination.get("version_id"), "updated_at": destination.get("updated_at")},
            "subscription": {key: subscription.get(key) for key in
                             ("id", "provider_domain", "account_id", "source_playlist_id", "name")},
            "snapshots": {
                "observed": {"id": subscription.get("observed_snapshot"), "at": subscription.get("observed_at")},
                "attempted": {"id": subscription.get("attempted_snapshot"), "at": subscription.get("attempted_at")},
                "committed": {"id": subscription.get("committed_snapshot"), "at": subscription.get("committed_at"),
                              "version_id": subscription.get("committed_version_id")},
            },
            "check": {"state": latest.get("state") if latest else None,
                      **{key: check_state.get(key) for key in
                         ("last_check_at", "last_success_at", "next_check_at", "access_state", "last_error_code")}},
        }

    @staticmethod
    def _match_classification(overlay: dict[str, Any]) -> str:
        if overlay.get("approved_asset_id"):
            return "approved"
        candidates = overlay.get("candidates", ())
        available = [candidate for candidate in candidates if not candidate.get("rejected")]
        if candidates and not available:
            return "rejected"
        count = len(available)
        return "unmatched" if count == 0 else "candidate" if count == 1 else "ambiguous"

    def _local_mapping_candidate(self, mapping: Any) -> tuple[Any, dict[str, Any]] | None:
        """Classify an existing merged mapping without resolving or refreshing it."""
        provider = next(
            (item for item in self.mass.music.providers if item.instance_id == mapping.provider_instance),
            None,
        )
        if (
            provider is None
            or not provider.available
            or getattr(mapping, "available", True) is False
            or provider.domain != mapping.provider_domain
            or provider.domain in ("builtin", "spotify")
            or getattr(provider, "is_streaming_provider", True)
        ):
            return None
        evidence = {
            "kind": "existing_merged_provider_mapping",
            "provider_domain": str(mapping.provider_domain),
            "provider_instance_id": str(mapping.provider_instance),
            "provider_item_id": str(mapping.item_id),
        }
        return provider, evidence

    async def match_review(self, version_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Generate a bounded, read-only review overlay from existing MA mappings."""
        self._authorize()
        if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
            raise InvalidDataError("Match review page requires limit 1..200 and nonnegative offset")
        version = await self._read_store(self._store.get_version, version_id)
        subscription = await self._read_store(self._store.get_subscription, version["subscription_id"])
        occurrences = version["occurrences"]
        page = occurrences[offset : offset + limit]
        source = {
            "provider_domain": subscription["provider_domain"],
            "provider_instance_id": subscription["provider_instance_id"],
            "account_id": subscription["account_id"],
        }
        controller = self.mass.music.get_controller(MediaType.TRACK)
        overlays: dict[str, dict[str, Any]] = {}
        candidate_freshness = "fresh"
        candidate_error = None
        for source_item_id in dict.fromkeys(
            row.get("source_item_id")
            for row in page
            if row.get("state") == "track" and isinstance(row.get("source_item_id"), str)
        ):
            try:
                # This is the direct library lookup. Deliberately do not call the
                # controller's refresh-on-access get() or any provider search.
                item = await controller.get_library_item_by_prov_id(
                    source_item_id, subscription["provider_instance_id"]
                )
            except Exception:
                # A transient library read must not erase a previous candidate set.
                candidate_freshness = "stale"
                candidate_error = "library_read_failed"
                overlays[source_item_id] = await self._read_store(
                    self._store.get_match_overlay,
                    subscription["provider_domain"],
                    subscription["account_id"],
                    "track",
                    source_item_id,
                )
                continue
            candidates = []
            if item is not None:
                for mapping in sorted(
                    item.provider_mappings,
                    key=lambda value: (value.provider_domain, value.provider_instance, value.item_id),
                ):
                    classified = self._local_mapping_candidate(mapping)
                    if classified is None:
                        continue
                    _, evidence = classified
                    evidence = {
                        **evidence,
                        "identity_claim": "candidate_not_exact_recording_or_edition",
                        "source_provider_domain": subscription["provider_domain"],
                        "source_provider_instance_id": subscription["provider_instance_id"],
                        "source_item_id": source_item_id,
                        "library_item_id": str(item.item_id),
                    }
                    asset = await self._read_store(
                        self._store.upsert_local_asset,
                        "track",
                        mapping.provider_instance,
                        str(mapping.item_id),
                        {"provider_domain": str(mapping.provider_domain)},
                        evidence,
                    )
                    candidates.append({"asset_id": asset["id"], "score": 0.5, "evidence": evidence})
            overlays[source_item_id] = await self._read_store(
                self._store.replace_match_candidates,
                subscription["provider_domain"],
                subscription["account_id"],
                "track",
                source_item_id,
                candidates,
                "ma-merged-mapping-v1",
            )
        items = []
        for occurrence in page:
            source_item_id = occurrence.get("source_item_id")
            overlay = overlays.get(source_item_id)
            items.append(
                {
                    "position": occurrence["position"],
                    "state": occurrence.get("state", "invalid"),
                    "source_item_id": source_item_id,
                    "match": overlay,
                    "classification": self._match_classification(overlay) if overlay else "unmatched",
                }
            )
        return {
            "version_id": version_id,
            "subscription_id": version["subscription_id"],
            "source": source,
            "limit": limit,
            "offset": offset,
            "total": len(occurrences),
            "has_more": offset + len(page) < len(occurrences),
            "candidate_freshness": candidate_freshness,
            "candidate_error": candidate_error,
            "items": items,
        }

    async def set_match_decision(
        self,
        version_id: str,
        source_item_id: str,
        expected_revision: int,
        action: str,
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist one explicit decision; this never changes MA mappings or playback."""
        user = self._authorize()
        if not has_scope(user, Scope.LIBRARY_WRITE):
            raise InsufficientPermissions("Match decisions require library write permission")
        version = await self._read_store(self._store.get_version, version_id)
        subscription = await self._read_store(self._store.get_subscription, version["subscription_id"])
        if not any(
            row.get("state") == "track" and row.get("source_item_id") == source_item_id
            for row in version["occurrences"]
        ):
            raise InvalidDataError("Source item is not a reviewable occurrence in this archive version")
        try:
            overlay = await self._read_store(
                self._store.get_match_overlay,
                subscription["provider_domain"], subscription["account_id"], "track", source_item_id,
            )
            if action == "approve":
                if not isinstance(asset_id, str) or asset_id not in {
                    candidate["asset_id"] for candidate in overlay["candidates"]
                }:
                    raise InvalidDataError("Approved asset must be a current candidate")
                result = await self._read_store(
                    self._store.set_match_decision,
                    subscription["provider_domain"], subscription["account_id"], "track", source_item_id,
                    "approve", asset_id, expected_revision, user.user_id,
                    {"kind": "explicit_user_review", "version_id": version_id}, "ma-merged-mapping-v1",
                )
            elif action == "reject":
                if not isinstance(asset_id, str) or asset_id not in {
                    candidate["asset_id"] for candidate in overlay["candidates"]
                }:
                    raise InvalidDataError("Rejected asset must be a current candidate")
                result = await self._read_store(
                    self._store.set_match_decision,
                    subscription["provider_domain"], subscription["account_id"], "track", source_item_id,
                    "reject", asset_id, expected_revision, user.user_id,
                    {"kind": "explicit_user_review", "version_id": version_id}, "ma-merged-mapping-v1",
                )
            elif action == "clear":
                if asset_id is not None:
                    raise InvalidDataError("Clearing a decision does not accept an asset ID")
                result = await self._read_store(
                    self._store.clear_match_decision,
                    subscription["provider_domain"], subscription["account_id"], "track", source_item_id,
                    expected_revision, user.user_id,
                    {"kind": "explicit_user_review", "version_id": version_id},
                )
            else:
                raise InvalidDataError("Match decision action must be approve, reject or clear")
        except ValueError as err:
            raise InvalidDataError(str(err)) from None
        return {"match": result, "classification": self._match_classification(result)}

    async def sync_policy(self, subscription_id: str) -> dict[str, Any]:
        self._authorize()
        return await self._read_store(self._store.get_sync_policy, subscription_id)

    async def set_sync_policy(self, subscription_id: str, expected_revision: int, mode: str, interval_seconds: int) -> dict[str, Any]:
        user = self._authorize()
        subscription = await self._read_store(self._store.get_subscription, subscription_id)
        # Pausing future checks must remain possible while Spotify is offline or
        # requires reauthentication. Enabling a schedule verifies the instance now.
        if mode == "scheduled":
            self._provider(subscription["provider_instance_id"])
        try:
            return await self._read_store(self._store.set_sync_policy, subscription_id, mode, interval_seconds,
                                          user.user_id, expected_revision)
        except ValueError as err:
            raise InvalidDataError(str(err)) from None

    async def sync_now(self, subscription_id: str) -> dict[str, Any]:
        user = self._authorize()
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            operation = asyncio.create_task(self._queue_sync(subscription_id, user.user_id))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                try:
                    await operation
                finally:
                    raise asyncio.CancelledError from None

    async def _queue_sync(self, subscription_id, user_id):
        subscription = await self._store_operation(self._store.get_subscription, subscription_id)
        self._provider(subscription["provider_instance_id"])
        try:
            job = await self._store_operation(self._store.begin_sync, subscription_id, "manual")
        except (ValueError, sqlite3.IntegrityError):
            raise InvalidDataError("A capture or sync is already active for this source") from None
        task_id = f"library_enrichment_sync_{job['id']}"
        self._sync_jobs[job["id"]] = task_id
        try:
            self.mass.tasks.run_background_task(
                task_id=task_id, name="Refresh selected archived Spotify playlist",
                handler=lambda: self._run_sync(job, user_id), user_id=user_id,
                priority=True, allow_retry=False, allow_cancel=True,
            )
        except Exception:
            await self._store_operation(self._store.fail_sync, job["id"], "scheduling_failed", "Unable to schedule refresh")
            self._sync_jobs.pop(job["id"], None)
            raise
        return {"job_id": job["id"], "task_id": task_id, "state": "queued"}

    async def sync_status(self, subscription_id: str) -> dict[str, Any]:
        self._authorize()
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            for job_id, task_id in tuple(self._sync_jobs.items()):
                try:
                    task = self.mass.tasks.get_task(task_id)
                    state = getattr(task.status, "value", task.status)
                except InvalidDataError:
                    state = None
                if state not in ("idle", "pending", "running"):
                    job = await self._store_operation(self._store.get_sync_job, job_id)
                    if job["state"] in ("queued", "running"):
                        await self._store_operation(self._store.cancel_sync, job_id)
                    self._sync_jobs.pop(job_id, None)
            status = await self._store_operation(self._store.get_sync_status, subscription_id)
            return {**status, "latest_job": status["jobs"][-1] if status["jobs"] else None}

    async def _dispatch_sync(self):
        try:
            await self._dispatch_due()
        finally:
            self._sync_stopping.discard(self._sync_dispatcher_id)

    async def _dispatch_due(self):
        due = await self._store_operation(self._store.list_due, datetime.now(UTC).isoformat(), 10)
        for subscription in due[:10]:
            if self._closing:
                return
            policy = await self._store_operation(self._store.get_sync_policy, subscription["id"])
            if policy["mode"] != "scheduled":
                continue
            try:
                preparation = asyncio.create_task(asyncio.to_thread(self._store.begin_sync, subscription["id"], "scheduled"))
                try:
                    job = await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    try:
                        job = await preparation
                    except Exception:
                        raise asyncio.CancelledError from None
                    await self._store_operation(self._store.cancel_sync, job["id"])
                    raise
            except (ValueError, sqlite3.IntegrityError):
                continue  # Explicit capture or another refresh already owns this source.
            await self._run_sync(job, policy["initiating_user_id"])

    async def _run_sync(self, job, user_id):
        capture_id = None
        user_token = impersonation_token = None
        try:
            user = await self.mass.webserver.auth.get_user(user_id)
            user_token = current_user.set(user)
            impersonation_token = impersonated_user.set(None)
            self._authorize()
            await self._store_operation(self._store.mark_sync_running, job["id"])
            subscription = await self._store_operation(self._store.get_subscription, job["subscription_id"])
            provider = self._provider(subscription["provider_instance_id"])
            preview = await preview_playlist(provider, subscription["source_playlist_id"])
            if preview["account_id"] != subscription["account_id"]:
                raise SpotifyCaptureError("Spotify account changed; update the subscription explicitly", reason="account_changed")
            await self._store_operation(self._store.observe_sync, job["id"], preview["snapshot_id"])
            if preview["snapshot_id"] == subscription["committed_snapshot"]:
                await self._store_operation(self._store.succeed_sync, job["id"], subscription["committed_version_id"])
                return
            preparation = asyncio.create_task(asyncio.to_thread(
                self._store.begin_capture, subscription["id"], preview["snapshot_id"], job["id"]
            ))
            try:
                capture_id = await asyncio.shield(preparation)
            except asyncio.CancelledError:
                capture_id = await preparation
                raise

            received_count = 0

            async def progress(received):
                nonlocal received_count
                received_count = max(received_count, received)
                await self._store_operation(self._store.update_progress, capture_id, received_count, preview["total"])

            result = await capture_playlist(provider, subscription["source_playlist_id"], on_page=progress)
            if result["account_id"] != subscription["account_id"] or result["snapshot_before"] != preview["snapshot_id"]:
                raise SpotifyCaptureError("Spotify source identity or snapshot changed during refresh", reason="source_changed")
            version_id = await self._store_operation(
                lambda: self._store.commit_capture(capture_id, snapshot_before=result["snapshot_before"],
                    snapshot_after=result["snapshot_after"], total=result["total"], occurrences=result["occurrences"])
            )
            await self._store_operation(self._store.succeed_sync, job["id"], version_id)
        except BaseException as err:
            if capture_id:
                try:
                    await self._store_operation(self._store.fail_capture, capture_id, "Refresh interrupted before a complete commit")
                except ValueError:
                    pass  # An atomic commit that already completed remains valid.
            if isinstance(err, asyncio.CancelledError):
                code, access = "cancelled", "temporarily_unavailable"
            elif isinstance(err, InsufficientPermissions):
                code, access = "permission_denied", "access_denied"
            elif isinstance(err, SpotifyCaptureError):
                code = err.reason
                access = "access_denied" if code in ("account_changed", "source_inaccessible") else "temporarily_unavailable"
            elif err.__class__.__name__ == "LoginFailed":
                code, access = "authentication_required", "authentication_required"
            elif isinstance(err, InvalidDataError):
                code, access = "provider_offline", "provider_offline"
            else:
                code, access = "provider_error", "temporarily_unavailable"
            if isinstance(err, asyncio.CancelledError):
                try:
                    await self._store_operation(self._store.cancel_sync, job["id"])
                except ValueError:
                    pass  # A success transaction already executing may finish first.
                raise
            await self._store_operation(self._store.fail_sync, job["id"], code, f"Refresh stopped: {code}", access)
        finally:
            if impersonation_token is not None:
                impersonated_user.reset(impersonation_token)
            if user_token is not None:
                current_user.reset(user_token)
            task_id = self._sync_jobs.pop(job["id"], None)
            if task_id:
                self._sync_stopping.discard(task_id)

    @staticmethod
    def _projection(version):
        """Build a playback projection without changing the historical occurrences."""
        ids, omitted = [], []
        for row in version["occurrences"]:
            source_id = row.get("source_item_id")
            if row.get("state") == "track" and isinstance(source_id, str) and re.fullmatch(r"[A-Za-z0-9]{22}", source_id):
                ids.append(source_id)
            else:
                omitted.append({"position": row["position"], "state": row.get("state", "invalid")})
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", version.get("name", "") or "Spotify archive").strip()
        name = f"{name[:120] or 'Spotify archive'} - archive {version['id'][:8]}"
        document = {"version_id": version["id"], "name": name, "ids": ids, "omitted": omitted}
        digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"version_id": version["id"], "name": name, "source_count": version["total"], "projected_count": len(ids),
                "omitted": omitted, "omitted_count": len(omitted), "projection_digest": digest,
                "requires_partial_consent": bool(omitted)}, ids

    @staticmethod
    def _apply_result(job, version_id):
        if job is None:
            return {"version_id": version_id, "state": "not_applied", "retryable": True, "destination": None}
        result = dict(job)
        result["version_id"] = version_id
        result["state"] = {"prepared": "pending", "creating": "applying"}.get(job["state"], job["state"])
        result["omitted_count"] = job["source_count"] - job["projected_count"]
        if job["state"] == "applied" and result["omitted_count"]:
            result["state"] = "partial"
        # A failed state is only written before external playlist creation began.
        # Once creation starts, every error is uncertain and must be reconciled.
        result["retryable"] = job["state"] == "failed"
        result["destination"] = None
        if job.get("destination_item_id"):
            result["destination"] = {"item_id": job["destination_item_id"], "provider_instance": "library",
                                     "uri": f"library://playlist/{job['destination_item_id']}", "name": job["requested_name"],
                                     "builtin_provider_instance": job.get("destination_provider_instance")}
        return result

    async def playback_policy(self, subscription_id: str) -> dict[str, Any]:
        """Return the explicit source-selection policy for one subscription."""
        self._authorize()
        return await self._read_store(self._store.get_playback_policy, subscription_id)

    async def set_playback_policy(self, subscription_id: str, mode: str, expected_revision: int) -> dict[str, Any]:
        """CAS update only; changing policy never mutates a playlist."""
        user = self._authorize()
        try:
            return await self._read_store(
                self._store.set_playback_policy, subscription_id, mode, expected_revision, str(user.user_id)
            )
        except ValueError as err:
            raise InvalidDataError(str(err)) from None

    @staticmethod
    def _playback_result(row: dict | None, subscription_id: str) -> dict[str, Any]:
        if row is None:
            return {"subscription_id": subscription_id, "state": "not_applied", "destination": None,
                    "retryable": True}
        result = dict(row)
        result["gaps"] = json.loads(result.pop("gaps_json"))
        result["state"] = {"prepared": "pending", "writing": "applying"}.get(result["state"], result["state"])
        result["retryable"] = row["state"] == "failed"
        result["destination"] = None
        if row.get("destination_item_id"):
            result["destination"] = {
                "item_id": row["destination_item_id"], "provider_instance": "library",
                "uri": f"library://playlist/{row['destination_item_id']}",
                "builtin_provider_instance": row.get("destination_provider_instance"),
            }
        return result

    def _playback_projection(self, version: dict, policy: dict, overlay: dict) -> tuple[dict, list[str]]:
        """Derive ordered playback URIs solely from immutable occurrences and approved v4 assets."""
        matches = {item["position"]: item.get("match") for item in overlay["occurrences"]}
        rows, gaps, uris = [], [], []
        for occurrence in version["occurrences"]:
            position = occurrence["position"]
            source_id = occurrence.get("source_item_id")
            spotify_uri = f"spotify://track/{source_id}" if (
                occurrence.get("state") == "track" and isinstance(source_id, str)
                and re.fullmatch(r"[A-Za-z0-9]{22}", source_id)
            ) else None
            match = matches.get(position)
            approved = None
            if match and match.get("approved_asset_id"):
                approved = next((item for item in match["candidates"] if item.get("approved") and not item.get("rejected")), None)
            local_uri = None
            if approved and approved["asset"].get("locations"):
                location = approved["asset"]["locations"][0]
                local_uri = f"{location['provider_instance_id']}://track/{location['item_id']}"
            available = [item for item in (match or {}).get("candidates", ()) if not item.get("rejected")]
            gap_reason = None
            if local_uri is None:
                if match and match.get("candidates") and not available:
                    gap_reason = "rejected"
                elif len(available) > 1:
                    gap_reason = "ambiguous"
                else:
                    gap_reason = "missing"
            mode = policy["mode"]
            selected = None
            fallback = None
            strict_provider = None
            if mode == "local_only":
                if local_uri:
                    selected = local_uri
                    strict_provider = local_uri.split("://", 1)[0]
            elif mode == "prefer_local":
                selected = local_uri or spotify_uri
                fallback = "spotify" if local_uri is None and spotify_uri else None
            else:
                selected = spotify_uri or local_uri
                fallback = "local" if spotify_uri is None and local_uri else None
            if selected:
                uris.append(selected)
                rows.append({
                    "position": position,
                    "uri": selected,
                    "selected_source": "local" if local_uri and selected.endswith(local_uri) else "spotify",
                    "fallback": fallback,
                    "strict_provider": strict_provider,
                })
            else:
                gap_reason = "unsupported" if spotify_uri is None and gap_reason == "missing" else gap_reason
            if gap_reason:
                gaps.append({"position": position, "reason": gap_reason, "omitted": selected is None,
                             "fallback": fallback})
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", version.get("name", "") or "Spotify archive").strip()
        name = f"{name[:120] or 'Spotify archive'} - playback"
        document = {"version_id": version["id"], "subscription_id": version["subscription_id"],
                    "policy_revision": policy["revision"], "mode": policy["mode"], "name": name,
                    "rows": rows, "gaps": gaps}
        digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {**document, "source_count": version["total"], "projected_count": len(rows),
                "omitted_count": sum(bool(item["omitted"]) for item in gaps), "projection_digest": digest,
                "requires_partial_consent": any(item["omitted"] for item in gaps)}, uris

    async def playback_preview(self, version_id: str) -> dict[str, Any]:
        self._authorize()
        version = await self._read_store(self._store.get_version, version_id)
        policy = await self._read_store(self._store.get_playback_policy, version["subscription_id"])
        overlay = await self._read_store(self._store.get_version_match_overlay, version_id)
        preview, _ = self._playback_projection(version, policy, overlay)
        current = await self._read_store(self._store.get_playback_projection, version["subscription_id"])
        return {**preview, "destination": self._playback_result(current, version["subscription_id"])["destination"]}

    async def playback_status(self, subscription_id: str) -> dict[str, Any]:
        self._authorize()
        policy, projection = await self._read_store(
            lambda: (self._store.get_playback_policy(subscription_id), self._store.get_playback_projection(subscription_id))
        )
        return {"policy": policy, "projection": self._playback_result(projection, subscription_id)}

    async def playback_apply(self, version_id: str, expected_digest: str, expected_policy_revision: int,
                             allow_partial: bool = False) -> dict[str, Any]:
        user = self._authorize()
        if not has_scope(user, Scope.LIBRARY_WRITE):
            raise InsufficientPermissions("Updating the playback projection requires library write permission")
        if type(allow_partial) is not bool:
            raise InvalidDataError("Partial-projection consent must be a boolean")
        async with self._write_lock:
            version = await self._store_operation(self._store.get_version, version_id)
            policy = await self._store_operation(self._store.get_playback_policy, version["subscription_id"])
            overlay = await self._store_operation(self._store.get_version_match_overlay, version_id)
            preview, uris = self._playback_projection(version, policy, overlay)
            if policy["revision"] != expected_policy_revision:
                raise InvalidDataError("Playback policy changed; preview again")
            if preview["projection_digest"] != expected_digest:
                raise InvalidDataError("Playback projection changed; preview again")
            if preview["requires_partial_consent"] and not allow_partial:
                raise InvalidDataError("Explicit consent is required to omit unresolved occurrences")
            builtin = next((p for p in self.mass.music.providers if p.domain == "builtin" and p.available), None)
            if builtin is None or not callable(getattr(builtin, "_read_m3u_file", None)):
                raise InvalidDataError("An accessible compatible builtin playlist provider is required")
            row = await self._store_operation(
                self._store.prepare_playback_projection, version["subscription_id"], version_id, policy["revision"],
                expected_digest, preview["source_count"], preview["projected_count"], json.dumps(preview["gaps"])
            )
            started = False
            try:
                await self._store_operation(self._store.mark_playback_projection_writing, version["subscription_id"])
                started = True
                m3u_rows = []
                for projected, uri in zip(preview["rows"], uris, strict=True):
                    if projected["strict_provider"]:
                        m3u_rows.append(f"#EXTPROV:local_only||{projected['strict_provider']}\n")
                    m3u_rows.append(f"{uri}\n")
                m3u = "#EXTM3U\n#PLAYLIST:" + preview["name"] + "\n" + "".join(m3u_rows)
                if row.get("destination_item_id"):
                    destination = await self.mass.music.playlists.get_library_item(row["destination_item_id"])
                    mapping = next(
                        (m for m in destination.provider_mappings if m.provider_instance == builtin.instance_id), None
                    )
                    if mapping is None:
                        raise InvalidDataError("Playback destination lost its builtin mapping")
                    playlist_helpers = importlib.import_module("music_assistant.helpers.playlists")
                    await builtin._write_m3u_file(mapping.item_id, preview["name"], playlist_helpers.parse_m3u(m3u))
                else:
                    destination = await self.mass.music.playlists.import_playlist(m3u, library_matching=False)
                    mapping = next(
                        (m for m in destination.provider_mappings if m.provider_instance == builtin.instance_id), None
                    )
                if mapping is None:
                    raise InvalidDataError("Created playback playlist has no expected builtin mapping")
                raw = await builtin._read_m3u_file(mapping.item_id)
                persisted, pending_strict = [], None
                for raw_line in raw.splitlines():
                    line = raw_line.strip()
                    if line.startswith("#EXTPROV:local_only||"):
                        pending_strict = line.removeprefix("#EXTPROV:local_only||")
                    elif line and not line.startswith("#"):
                        persisted.append((line, pending_strict))
                        pending_strict = None
                expected = [(uri, row["strict_provider"]) for row, uri in zip(preview["rows"], uris, strict=True)]
                if pending_strict is not None or persisted != expected:
                    raise InvalidDataError("Playback playlist contents differ from the approved ordered projection")
                row = await self._store_operation(self._store.commit_playback_projection, version["subscription_id"],
                                                  str(destination.item_id), builtin.instance_id, expected_digest)
            except BaseException as err:
                await self._store_operation(self._store.fail_playback_projection, version["subscription_id"],
                                            "Playback projection outcome uncertain" if started else "Unable to prepare playback projection",
                                            started)
                if isinstance(err, asyncio.CancelledError):
                    raise
                raise InvalidDataError("Playback projection outcome uncertain; inspect builtin playlists") from None
            return self._playback_result(row, version["subscription_id"])

    async def apply_preview(self, version_id: str) -> dict[str, Any]:
        """Review the exact immutable-version projection before a local playlist write."""
        self._authorize()
        version, job = await self._read_store(lambda: (self._store.get_version(version_id), self._store.get_apply_for_version(version_id)))
        preview, _ = self._projection(version)
        result = self._apply_result(job, version_id)
        return {**preview, "already_applied": bool(job and job["state"] == "applied"), "destination": result["destination"]}

    async def apply_status(self, version_id: str) -> dict[str, Any]:
        self._authorize()
        version, job = await self._read_store(lambda: (self._store.get_version(version_id), self._store.get_apply_for_version(version_id)))
        return self._apply_result(job, version_id)

    async def apply(self, version_id: str, expected_digest: str, allow_partial: bool = False) -> dict[str, Any]:
        """Create one builtin playlist copy, with durable intent and no automatic uncertain retry."""
        user = self._authorize()
        if not has_scope(user, Scope.LIBRARY_WRITE):
            raise InsufficientPermissions("Creating a playlist copy requires library write permission")
        if type(allow_partial) is not bool:
            raise InvalidDataError("Partial-copy consent must be a boolean")
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            operation = asyncio.create_task(self._apply_version(version_id, expected_digest, allow_partial))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Keep lifecycle ownership until a durable outcome is known. An HTTP
                # disconnect cannot authorize a second import or race store teardown.
                try:
                    await operation
                finally:
                    raise asyncio.CancelledError from None

    async def _apply_version(self, version_id, expected_digest, allow_partial):
        version = await asyncio.to_thread(self._store.get_version, version_id)
        builtin = next((p for p in self.mass.music.providers if p.domain == "builtin" and p.available), None)
        if builtin is None or not callable(getattr(builtin, "_read_m3u_file", None)):
            raise InvalidDataError("An accessible compatible builtin playlist provider is required")
        preview, ids = self._projection(version)
        if expected_digest != preview["projection_digest"]:
            raise InvalidDataError("Playlist projection changed; preview again")
        if preview["requires_partial_consent"] and not allow_partial:
            raise InvalidDataError("Explicit consent is required to omit unsupported source occurrences")
        job = await asyncio.to_thread(self._store.prepare_apply, version_id, expected_digest, preview["source_count"],
                                      len(ids), json.dumps(preview["omitted"]), f"archive:{version_id}", preview["name"])
        if job["state"] != "prepared":
            return self._apply_result(job, version_id)
        started = False
        try:
            await asyncio.to_thread(self._store.mark_apply_creating, job["id"])
            started = True
            if ids:
                m3u = "#EXTM3U\n#PLAYLIST:" + preview["name"] + "\n" + "".join(f"spotify://track/{item_id}\n" for item_id in ids)
                destination = await self.mass.music.playlists.import_playlist(m3u, library_matching=False)
            else:
                destination = await self.mass.music.playlists.create_playlist(
                    preview["name"], media_types=[MediaType.TRACK], provider_instance_or_domain=builtin.instance_id
                )
            mapping = next((m for m in destination.provider_mappings if m.provider_instance == builtin.instance_id), None)
            if mapping is None:
                raise InvalidDataError("Created playlist has no expected builtin mapping")
            raw = await builtin._read_m3u_file(mapping.item_id)
            paths = [line.strip() for line in raw.splitlines() if line.strip() and not line.lstrip().startswith("#")]
            if paths != [f"spotify://track/{item_id}" for item_id in ids]:
                raise InvalidDataError("Created playlist contents differ from the approved ordered projection")
            await asyncio.to_thread(self._store.commit_apply, job["id"], str(destination.item_id), builtin.instance_id, expected_digest)
        except BaseException as err:
            error = (
                "Playlist creation outcome uncertain; inspect builtin playlists before recovery"
                if started else "Unable to prepare playlist copy"
            )
            await asyncio.to_thread(self._store.fail_apply, job["id"], error, uncertain=started)
            if isinstance(err, asyncio.CancelledError):
                raise
            raise InvalidDataError(error) from None
        return self._apply_result(await asyncio.to_thread(self._store.get_apply, job["id"]), version_id)

    async def _read_store(self, method, *args):
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            return await self._store_operation(method, *args)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self._authorize()
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            return await self._cancel(job_id)

    async def _cancel(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            raise InvalidDataError("No active capture with that job ID")
        if job_id in self._stopping_jobs:
            return {"job_id": job_id, "state": "stopping", "cancelled": False}
        stopped = await self.mass.tasks.unregister_scheduled_task_and_wait(f"library_enrichment_{job_id}")
        if not stopped:
            # MA unregisters before waiting. A second unregister would report True
            # even though the first worker is still unwinding; retain that knowledge.
            self._stopping_jobs.add(job_id)
            return {"job_id": job_id, "state": "stopping", "cancelled": False}
        try:
            await asyncio.to_thread(self._store.fail_capture, job_id, "Capture cancelled")
        except ValueError:
            # A transaction already running in to_thread may commit before cancellation.
            pass
        self._jobs.discard(job_id)
        job = next(item for item in await asyncio.to_thread(self._store.list_jobs) if item["id"] == job_id)
        return {"job_id": job_id, "state": job["state"], "cancelled": job["state"] == "failed"}
