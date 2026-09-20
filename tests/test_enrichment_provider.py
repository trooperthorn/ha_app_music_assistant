"""Exercise command authorization, capture orchestration and inspection boundaries."""

import asyncio
import importlib.util
import json
import sys
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

    def schedule(**kwargs):
        handlers.append(kwargs)
        return types.SimpleNamespace(id=kwargs["task_id"])

    controller = types.SimpleNamespace(
        get_library_item=AsyncMock(return_value=types.SimpleNamespace(to_dict=lambda: {"external_ids": []})),
        get=AsyncMock(side_effect=AssertionError("inspection must not refresh")),
    )
    provider.mass = types.SimpleNamespace(
        storage_path=str(tmp_path),
        webserver=types.SimpleNamespace(auth=types.SimpleNamespace(get_user=AsyncMock(return_value=auth.user))),
        music=types.SimpleNamespace(providers=[spotify], get_controller=lambda media_type: controller),
        tasks=types.SimpleNamespace(run_background_task=schedule, unregister_scheduled_task_and_wait=AsyncMock()),
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
        provider=provider, module=module, auth=auth, handlers=handlers, controller=controller, registered=registered
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
        plugin.provider.cancel("unknown"),
    ):
        with pytest.raises(Exception, match="permission"):
            asyncio.run(coroutine)
    plugin.module.preview_playlist.assert_not_called()


def test_scoped_instance_required_and_all_commands_have_scope(plugin):
    with pytest.raises(Exception, match="accessible"):
        asyncio.run(plugin.provider.preview("spotify", "playlist"))
    asyncio.run(plugin.provider.loaded_in_mass())
    assert len(plugin.registered) == 7
    assert all(scope == "config.providers.write" for _, scope in plugin.registered)


def test_selected_capture_commits_real_ordered_store(plugin):
    async def run():
        result = await plugin.provider.capture("spotify-a", "playlist")
        assert plugin.handlers[0]["user_id"] == "admin"
        assert (await plugin.provider.status())["jobs"][0]["state"] == "pending"
        await plugin.handlers[0]["handler"]()
        status = await plugin.provider.status()
        assert status["jobs"][0]["state"] == "committed"
        version = await plugin.provider.archive_version(status["subscriptions"][0]["committed_version_id"])
        assert len(version["occurrences"]) == 3
        assert version["occurrences"][1]["source_payload"] is None
        assert result["job_id"] not in plugin.provider._jobs

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
