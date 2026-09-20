"""Selected Spotify metadata archives, isolated from the MA library database.

Compatibility is deliberately limited to the inspected server release. Archives
preserve references and occurrences; they are not downloaded audio or MA mirrors.
"""

from __future__ import annotations

import asyncio
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

from .spotify import capture_playlist, preview_playlist
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
        self._write_lock = asyncio.Lock()
        self._store = await asyncio.to_thread(ArchiveStore, Path(self.mass.storage_path) / "library_enrichment" / "enrichment.db")
        await asyncio.to_thread(self._store.recover_pending)

    async def loaded_in_mass(self) -> None:
        for command, handler in (
            ("capabilities", self.capabilities),
            ("inspect", self.inspect),
            ("preview", self.preview),
            ("capture", self.capture),
            ("status", self.status),
            ("version", self.archive_version),
            ("cancel", self.cancel),
        ):
            self._handles.append(
                self.mass.register_api_command(f"library_enrichment/{command}", handler, required_scope=Scope.CONFIG_PROVIDERS_WRITE)
            )

    async def unload(self, is_removed: bool = False) -> None:
        for handle in self._handles:
            handle()
        for job_id in tuple(self._jobs):
            await self.mass.tasks.unregister_scheduled_task_and_wait(f"library_enrichment_{job_id}")
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
        return await preview_playlist(self._provider(provider_instance_id), source_playlist_id, max_items=max_items)

    async def capture(self, provider_instance_id: str, source_playlist_id: str, max_items: int = 10000) -> dict[str, Any]:
        user = self._authorize()
        provider = self._provider(provider_instance_id)
        preview = await preview_playlist(provider, source_playlist_id, max_items=max_items)
        async with self._write_lock:
            subscription = await asyncio.to_thread(
                self._store.upsert_subscription, "spotify", preview["account_id"], source_playlist_id, provider_instance_id, preview["name"]
            )
            subscription_id = subscription["id"]
            await asyncio.to_thread(self._store.observe, subscription_id, preview["snapshot_id"])
            job_id = await asyncio.to_thread(self._store.begin_capture, subscription_id, preview["snapshot_id"])
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
            await asyncio.to_thread(self._store.fail_capture, job_id, f"Capture failed: {type(err).__name__}")
            raise InvalidDataError("Capture failed; previous committed archive retained") from None
        finally:
            self._jobs.discard(job_id)

    async def status(self) -> dict[str, Any]:
        self._authorize()
        return {
            "subscriptions": await asyncio.to_thread(self._store.list_subscriptions),
            "jobs": await asyncio.to_thread(self._store.list_jobs),
        }

    async def archive_version(self, version_id: str) -> dict[str, Any]:
        self._authorize()
        return await asyncio.to_thread(self._store.get_version, version_id)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        self._authorize()
        if job_id not in self._jobs:
            raise InvalidDataError("No active capture with that job ID")
        await self.mass.tasks.unregister_scheduled_task_and_wait(f"library_enrichment_{job_id}")
        try:
            await asyncio.to_thread(self._store.fail_capture, job_id, "Capture cancelled")
        except ValueError:
            # A transaction already running in to_thread may commit before cancellation.
            pass
        self._jobs.discard(job_id)
        job = next(item for item in await asyncio.to_thread(self._store.list_jobs) if item["id"] == job_id)
        return {"job_id": job_id, "state": job["state"], "cancelled": job["state"] == "failed"}
