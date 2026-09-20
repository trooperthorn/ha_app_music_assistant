"""Selected Spotify metadata archives, isolated from the MA library database.

Compatibility is deliberately limited to the inspected server release. Archives
preserve references and occurrences; they are not downloaded audio or MA mirrors.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from importlib.metadata import version
from pathlib import Path
from typing import Any

from music_assistant.controllers.webserver.helpers.auth_middleware import (
    current_user,
    get_current_user,
    has_scope,
    impersonated_user,
)
from music_assistant.models.plugin import PluginProvider
from music_assistant_models.auth import Scope
from music_assistant_models.enums import MediaType
from music_assistant_models.errors import InsufficientPermissions, InvalidDataError

from .spotify import SpotifyCaptureError, capture_playlist, preview_playlist
from .store import ArchiveStore

SUPPORTED_SERVER = "2.10.4"


async def setup(mass, manifest, config):
    """Construct the provider using the standard MA lifecycle."""
    return LibraryEnrichmentProvider(mass, manifest, config)


class LibraryEnrichmentProvider(PluginProvider):
    """Admin-operated selected captures; no remote mutation or local matching."""

    async def handle_async_init(self) -> None:
        if version("music-assistant") != SUPPORTED_SERVER:
            raise InvalidDataError("Library Enrichment requires compatibility review for this server version")
        self._handles = []
        self._jobs: set[str] = set()
        self._closing = False
        self._write_lock = asyncio.Lock()
        self._store = await asyncio.to_thread(ArchiveStore, Path(self.mass.storage_path) / "library_enrichment" / "enrichment.db")
        await asyncio.to_thread(self._store.recover_pending)

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
            ("cancel", self.cancel),
        ):
            self._handles.append(
                self.mass.register_api_command(f"library_enrichment/{command}", handler, required_scope=Scope.CONFIG_PROVIDERS_WRITE)
            )

    async def unload(self, is_removed: bool = False) -> None:
        self._closing = True
        for handle in self._handles:
            handle()
        async with self._write_lock:
            for job_id in tuple(self._jobs):
                stopped = await self.mass.tasks.unregister_scheduled_task_and_wait(f"library_enrichment_{job_id}")
                if not stopped:
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
            "inspection": "library_only_no_refresh",
            "mirror_apply": False,
            "local_matching": False,
            "liked_songs": False,
            "audio_backup": False,
            "max_items": 10000,
            "access": "provider_configuration_administrators",
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
            except sqlite3.IntegrityError:
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

    async def status(self) -> dict[str, Any]:
        self._authorize()
        return await self._read_store(lambda: {"subscriptions": self._store.list_subscriptions(), "jobs": self._store.list_jobs()})

    async def archive_version(self, version_id: str) -> dict[str, Any]:
        self._authorize()
        return await self._read_store(self._store.get_version, version_id)

    async def archive_versions(self, subscription_id: str, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        self._authorize()
        return await self._read_store(self._store.list_versions, subscription_id, limit, offset)

    async def _read_store(self, method, *args):
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            operation = asyncio.create_task(asyncio.to_thread(method, *args))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Do not let unload close a connection still queued in the thread pool.
                try:
                    await operation
                finally:
                    raise asyncio.CancelledError from None

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self._authorize()
        async with self._write_lock:
            if self._closing:
                raise InvalidDataError("Archive provider is stopping")
            return await self._cancel(job_id)

    async def _cancel(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            raise InvalidDataError("No active capture with that job ID")
        stopped = await self.mass.tasks.unregister_scheduled_task_and_wait(f"library_enrichment_{job_id}")
        if not stopped:
            return {"job_id": job_id, "state": "stopping", "cancelled": False}
        try:
            await asyncio.to_thread(self._store.fail_capture, job_id, "Capture cancelled")
        except ValueError:
            # A transaction already running in to_thread may commit before cancellation.
            pass
        self._jobs.discard(job_id)
        job = next(item for item in await asyncio.to_thread(self._store.list_jobs) if item["id"] == job_id)
        return {"job_id": job_id, "state": job["state"], "cancelled": job["state"] == "failed"}
