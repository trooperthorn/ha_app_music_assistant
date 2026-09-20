"""Exercise command authorization, capture orchestration and inspection boundaries."""

import asyncio
import importlib.util
import json
import sys
import threading
import types
from contextvars import ContextVar
from enum import StrEnum
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "music_assistant_lm/providers/library_enrichment"


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    """Stub MA infrastructure only; execute the real provider and SQLite store."""

    class Scope(StrEnum):
        CONFIG_PROVIDERS_WRITE = "config.providers.write"

    class MediaType(StrEnum):
        TRACK = "track"
        ARTIST = "artist"
        ALBUM = "album"
        PLAYLIST = "playlist"

    class Error(Exception):
        pass

    auth = types.SimpleNamespace(user=types.SimpleNamespace(user_id="admin", allowed=True))
    unset = object()
    current = ContextVar("test_current_user", default=unset)
    impersonated = ContextVar("test_impersonated_user", default=None)
    modules = {
        "music_assistant_models.auth": {"Scope": Scope},
        "music_assistant_models.enums": {"MediaType": MediaType},
        "music_assistant_models.errors": {"InsufficientPermissions": Error, "InvalidDataError": Error},
        "music_assistant.models.plugin": {"PluginProvider": object},
        "music_assistant.controllers.webserver.helpers.auth_middleware": {
            "get_current_user": lambda: auth.user if current.get() is unset else current.get(),
            "has_scope": lambda user, scope: user.allowed,
            "current_user": current,
            "impersonated_user": impersonated,
        },
    }
    for name, attributes in modules.items():
        module = types.ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("tested_enrichment", SOURCE / "__init__.py", submodule_search_locations=[str(SOURCE)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "tested_enrichment", module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "version", lambda name: "2.10.4")
    provider = module.LibraryEnrichmentProvider()
    spotify = types.SimpleNamespace(instance_id="spotify-a", domain="spotify", available=True)
    handlers = []
    registered = []
    tasks = {}

    def schedule(**kwargs):
        handlers.append(kwargs)
        task = types.SimpleNamespace(id=kwargs["task_id"], status="pending", started_at=None)
        tasks[task.id] = task
        return task

    def get_task(task_id):
        if task_id not in tasks:
            raise Error(f"Task {task_id} not found")
        return tasks[task_id]

    controller = types.SimpleNamespace(
        get_library_item=AsyncMock(return_value=types.SimpleNamespace(to_dict=lambda: {"external_ids": []})),
        get=AsyncMock(side_effect=AssertionError("inspection must not refresh")),
    )
    provider.mass = types.SimpleNamespace(
        storage_path=str(tmp_path),
        webserver=types.SimpleNamespace(auth=types.SimpleNamespace(get_user=AsyncMock(return_value=auth.user))),
        music=types.SimpleNamespace(
            providers=[spotify],
            get_controller=lambda media_type: controller,
            playlists=types.SimpleNamespace(library_items=AsyncMock(return_value=[])),
        ),
        tasks=types.SimpleNamespace(
            run_background_task=schedule,
            get_task=get_task,
            unregister_scheduled_task_and_wait=AsyncMock(return_value=True),
        ),
        register_api_command=lambda command, handler, required_scope: registered.append((command, required_scope)) or (lambda: None),
    )
    preview = {"account_id": "account-a", "snapshot_id": "A", "name": "Same name", "total": 3}
    rows = [{"position": index, "source_payload": raw} for index, raw in enumerate([{"id": "song"}, None, {"id": "song"}])]
    monkeypatch.setattr(module, "preview_playlist", AsyncMock(return_value=preview))
    monkeypatch.setattr(
        module,
        "capture_playlist",
        AsyncMock(
            return_value={
                **preview,
                "snapshot_before": "A",
                "snapshot_after": "A",
                "occurrences": rows,
            }
        ),
    )
    asyncio.run(provider.handle_async_init())
    yield types.SimpleNamespace(
        provider=provider,
        module=module,
        auth=auth,
        handlers=handlers,
        tasks=tasks,
        controller=controller,
        registered=registered,
    )
    provider._store.close()


def test_preview_and_inspection_do_not_mutate_archive_or_schedule_refresh(plugin):
    async def run():
        await plugin.provider.preview("spotify-a", "playlist")
        result = await plugin.provider.inspect("track", "42")
        assert result == {"record_kind": "full", "external_ids_loaded": True, "item": {"external_ids": []}}
        assert await plugin.provider.status() == {"subscriptions": [], "jobs": []}

    asyncio.run(run())
    plugin.controller.get.assert_not_called()
    plugin.controller.get_library_item.assert_awaited_once_with("42")
    assert not plugin.handlers


@pytest.mark.parametrize("user", [None, types.SimpleNamespace(user_id="guest", allowed=False)])
def test_unauthorized_archive_and_inspection_calls_are_denied(plugin, user):
    plugin.auth.user = user
    for coroutine in (
        plugin.provider.capabilities(),
        plugin.provider.inspect("track", "1"),
        plugin.provider.preview("spotify-a", "playlist"),
        plugin.provider.status(),
        plugin.provider.capture("spotify-a", "playlist"),
        plugin.provider.archive_version("unknown"),
        plugin.provider.archive_versions("unknown"),
        plugin.provider.sources("spotify-a"),
        plugin.provider.cancel("unknown"),
    ):
        with pytest.raises(Exception, match="permission"):
            asyncio.run(coroutine)
    plugin.module.preview_playlist.assert_not_called()


def test_scoped_instance_required_and_all_commands_have_scope(plugin):
    with pytest.raises(Exception, match="accessible"):
        asyncio.run(plugin.provider.preview("spotify", "playlist"))
    asyncio.run(plugin.provider.loaded_in_mass())
    assert len(plugin.registered) == 9
    assert all(scope == "config.providers.write" for _, scope in plugin.registered)


def test_selected_capture_commits_real_ordered_store(plugin):
    async def run():
        result = await plugin.provider.capture("spotify-a", "playlist")
        assert plugin.handlers[0]["user_id"] == "admin"
        assert plugin.handlers[0]["priority"] is True
        queued = (await plugin.provider.status())["jobs"][0]
        assert queued["state"] == "pending"
        assert queued["task_status"] == "pending"
        assert queued["task_started_at"] is None
        await plugin.handlers[0]["handler"]()
        status = await plugin.provider.status()
        assert status["jobs"][0]["state"] == "committed"
        version = await plugin.provider.archive_version(status["subscriptions"][0]["committed_version_id"])
        assert len(version["occurrences"]) == 3
        assert version["occurrences"][1]["source_payload"] is None
        assert result["job_id"] not in plugin.provider._jobs

    asyncio.run(run())


def test_core_task_cancellation_reconciles_durable_pending_job(plugin):
    async def run():
        result = await plugin.provider.capture("spotify-a", "playlist")
        plugin.tasks[result["task_id"]].status = "cancelled"

        status = await plugin.provider.status()

        assert status["jobs"][0]["state"] == "failed"
        assert status["jobs"][0]["error"] == "Capture task stopped before completion"
        assert result["job_id"] not in plugin.provider._jobs
        plugin.module.capture_playlist.assert_not_called()

    asyncio.run(run())


def test_failed_new_capture_keeps_previous_committed_version(plugin):
    async def run():
        await plugin.provider.capture("spotify-a", "playlist")
        await plugin.handlers[-1]["handler"]()
        original = (await plugin.provider.status())["subscriptions"][0]["committed_version_id"]
        plugin.module.preview_playlist.return_value = {"account_id": "account-a", "snapshot_id": "B", "name": "Rename", "total": 3}
        await plugin.provider.capture("spotify-a", "playlist")
        plugin.module.capture_playlist.side_effect = RuntimeError("sensitive provider error")
        with pytest.raises(Exception, match="previous committed archive retained"):
            await plugin.handlers[-1]["handler"]()
        status = await plugin.provider.status()
        subscription = status["subscriptions"][0]
        assert subscription["observed_snapshot"] == "B"
        assert subscription["committed_snapshot"] == "A"
        assert subscription["committed_version_id"] == original
        assert "sensitive" not in json.dumps(status)

    asyncio.run(run())


def test_account_change_cannot_commit_under_old_identity(plugin):
    async def run():
        await plugin.provider.capture("spotify-a", "playlist")
        plugin.module.capture_playlist.return_value["account_id"] = "account-b"
        with pytest.raises(Exception, match="previous committed archive retained"):
            await plugin.handlers[-1]["handler"]()
        status = await plugin.provider.status()
        assert status["subscriptions"][0]["committed_version_id"] is None
        assert status["jobs"][0]["state"] == "failed"

    asyncio.run(run())


def test_queued_capture_restores_requesting_user_context(plugin):
    async def run():
        await plugin.provider.capture("spotify-a", "playlist")
        plugin.auth.user = types.SimpleNamespace(user_id="guest", allowed=False)
        await plugin.handlers[-1]["handler"]()
        assert plugin.provider._store.list_jobs()[0]["state"] == "committed"
        with pytest.raises(Exception, match="permission"):
            await plugin.provider.status()

    asyncio.run(run())


@pytest.mark.parametrize("user", [None, types.SimpleNamespace(user_id="admin", allowed=False)])
def test_queued_capture_rechecks_revoked_permissions(plugin, user):
    async def run():
        await plugin.provider.capture("spotify-a", "playlist")
        plugin.provider.mass.webserver.auth.get_user.return_value = user
        with pytest.raises(Exception, match="previous committed archive retained"):
            await plugin.handlers[-1]["handler"]()
        assert plugin.provider._store.list_jobs()[0]["state"] == "failed"
        plugin.module.capture_playlist.assert_not_called()

    asyncio.run(run())


def test_cancel_before_worker_starts_has_durable_failed_state(plugin):
    async def run():
        result = await plugin.provider.capture("spotify-a", "playlist")
        cancelled = await plugin.provider.cancel(result["job_id"])
        assert cancelled["cancelled"] is True
        assert (await plugin.provider.status())["jobs"][0]["state"] == "failed"
        plugin.module.capture_playlist.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("expected", [{"expected_account_id": "another-account"}, {"expected_snapshot_id": "stale"}])
def test_capture_preconditions_reject_stale_preview_without_writes(plugin, expected):
    async def run():
        with pytest.raises(Exception, match="preview again"):
            await plugin.provider.capture("spotify-a", "playlist", **expected)
        assert await plugin.provider.status() == {"subscriptions": [], "jobs": []}
        assert not plugin.handlers

    asyncio.run(run())


def test_source_listing_is_library_only_scoped_and_paginated(plugin):
    def mapping(source_id, instance="spotify-a"):
        return types.SimpleNamespace(provider_domain="spotify", provider_instance=instance, item_id=source_id)

    rows = [
        types.SimpleNamespace(item_id="1", name="Same name", provider_mappings=[mapping("A" * 22), mapping("B" * 22, "private")]),
        types.SimpleNamespace(item_id="2", name="Liked songs", provider_mappings=[mapping("liked")]),
        types.SimpleNamespace(item_id="3", name="Next page", provider_mappings=[mapping("C" * 22)]),
    ]
    plugin.provider.mass.music.playlists.library_items.return_value = rows
    result = asyncio.run(plugin.provider.sources("spotify-a", limit=2, offset=10))
    assert result["items"] == [{"source_playlist_id": "A" * 22, "name": "Same name", "library_item_id": "1"}]
    assert result["excluded"] == [{"name": "Liked songs", "library_item_id": "2", "reason": "unsupported_source_identity"}]
    assert result["has_more"] is True
    assert result["scope"] == "imported_library_only"
    plugin.provider.mass.music.playlists.library_items.assert_awaited_once_with(
        provider="spotify-a", summary=True, limit=3, offset=10, order_by="name", collapse_collections=False
    )
    plugin.module.preview_playlist.assert_not_called()
    plugin.controller.get.assert_not_called()
    assert plugin.provider._store.list_subscriptions() == []


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1}])
def test_source_listing_rejects_unbounded_pages(plugin, args):
    with pytest.raises(Exception, match="Source page"):
        asyncio.run(plugin.provider.sources("spotify-a", **args))
    plugin.provider.mass.music.playlists.library_items.assert_not_called()


def test_cancel_timeout_does_not_report_success_or_fail_active_job(plugin):
    async def run():
        result = await plugin.provider.capture("spotify-a", "playlist")
        plugin.provider.mass.tasks.unregister_scheduled_task_and_wait.return_value = False
        cancelled = await plugin.provider.cancel(result["job_id"])
        assert cancelled == {"job_id": result["job_id"], "state": "stopping", "cancelled": False}
        assert (await plugin.provider.status())["jobs"][0]["state"] == "pending"
        assert result["job_id"] in plugin.provider._jobs
        plugin.provider.mass.tasks.unregister_scheduled_task_and_wait.return_value = True
        assert await plugin.provider.cancel(result["job_id"]) == cancelled
        plugin.provider.mass.tasks.unregister_scheduled_task_and_wait.assert_awaited_once()
        with pytest.raises(Exception, match="still stopping"):
            await plugin.provider.unload()
        assert plugin.provider._store.list_jobs()[0]["state"] == "pending"

    asyncio.run(run())


def test_unload_timeout_leaves_archive_open_for_unwinding_job(plugin):
    async def run():
        await plugin.provider.capture("spotify-a", "playlist")
        plugin.provider.mass.tasks.unregister_scheduled_task_and_wait.return_value = False
        with pytest.raises(Exception, match="still stopping"):
            await plugin.provider.unload()
        assert plugin.provider._store.list_jobs()[0]["state"] == "pending"
        with pytest.raises(Exception, match="stopping"):
            await plugin.provider.status()
        with pytest.raises(Exception, match="stopping"):
            await plugin.provider.capture("spotify-a", "playlist")

    asyncio.run(run())


def test_version_listing_retains_old_captures_without_loading_occurrences(plugin):
    async def run():
        result = await plugin.provider.capture("spotify-a", "playlist")
        await plugin.handlers[-1]["handler"]()
        versions = await plugin.provider.archive_versions(result["subscription_id"])
        assert len(versions) == 1
        assert versions[0]["snapshot_id"] == "A"
        assert "occurrences" not in versions[0]

    asyncio.run(run())


def test_request_cancellation_during_sqlite_preparation_finishes_job_before_unlock(plugin, monkeypatch):
    async def run():
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = plugin.provider._store.begin_capture

        def delayed_begin(*args):
            job = original(*args)
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=5)
            return job

        monkeypatch.setattr(plugin.provider._store, "begin_capture", delayed_begin)
        request = asyncio.create_task(plugin.provider.capture("spotify-a", "playlist"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        request.cancel()
        await asyncio.sleep(0)
        assert plugin.provider._write_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert plugin.provider._store.list_jobs()[0]["state"] == "failed"
        assert not plugin.provider._jobs
        assert not plugin.handlers
        assert not plugin.provider._write_lock.locked()
        # No orphan pending constraint prevents an explicit retry.
        await plugin.provider.capture("spotify-a", "playlist")

    asyncio.run(run())


def test_safe_adapter_failure_reason_survives_in_job_without_raw_provider_details(plugin):
    async def run():
        await plugin.provider.capture("spotify-a", "playlist")
        plugin.module.capture_playlist.side_effect = plugin.module.SpotifyCaptureError("Playlist pagination was incomplete or changed")
        with pytest.raises(Exception, match="previous committed"):
            await plugin.handlers[-1]["handler"]()
        assert (await plugin.provider.status())["jobs"][0]["error"] == "Playlist pagination was incomplete or changed"

    asyncio.run(run())


def test_installer_is_complete_compilable_and_idempotent(tmp_path):
    path = ROOT / "music_assistant_lm/patches/library_enrichment.py"
    spec = importlib.util.spec_from_file_location("enrichment_installer", path)
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.write_provider(tmp_path)
    before = {path.name: path.read_bytes() for path in (tmp_path / "library_enrichment").iterdir()}
    installer.write_provider(tmp_path)
    assert before == {path.name: path.read_bytes() for path in (tmp_path / "library_enrichment").iterdir()}
    assert json.loads(before["manifest.json"])["builtin"] is False
