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
        LIBRARY_WRITE = "library.write"

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
        "music_assistant_models.background_task": {"TaskSchedule": types.SimpleNamespace(hourly=lambda **kwargs: kwargs)},
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
    scheduled = []
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
            register_scheduled_task=lambda **kwargs: scheduled.append(kwargs) or types.SimpleNamespace(id=kwargs["task_id"]),
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
        scheduled=scheduled,
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


def _apply_fixture(plugin, *, omitted=False, empty=False):
    store = plugin.provider._store
    subscription = store.upsert_subscription("spotify", "account-a", "A" * 22, "spotify-a", "My archive")
    job = store.begin_capture(subscription["id"], "source-snapshot")
    rows = [] if empty else [{"position": index, "state": "track", "source_item_id": "T" * 22} for index in range(2)]
    if omitted:
        rows.append({"position": len(rows), "state": "null", "source_payload": None})
    version_id = store.commit_capture(job, snapshot_before="source-snapshot", snapshot_after="source-snapshot",
                                      total=len(rows), occurrences=rows)
    builtin = types.SimpleNamespace(instance_id="builtin", domain="builtin", available=True, _read_m3u_file=AsyncMock())
    destination = types.SimpleNamespace(
        item_id="123", provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="copy")]
    )

    async def import_playlist(m3u, *, library_matching):
        assert library_matching is False
        builtin._read_m3u_file.return_value = m3u
        return destination

    builtin._read_m3u_file.return_value = "#EXTM3U\n#PLAYLIST:Empty\n"
    plugin.provider.mass.music.providers.append(builtin)
    playlists = plugin.provider.mass.music.playlists
    playlists.import_playlist = AsyncMock(side_effect=import_playlist)
    playlists.create_playlist = AsyncMock(return_value=destination)
    return version_id, builtin, playlists


def test_apply_preserves_repeated_occurrences_and_is_idempotent(plugin):
    version_id, builtin, playlists = _apply_fixture(plugin)

    async def run():
        preview = await plugin.provider.apply_preview(version_id)
        assert preview["projected_count"] == 2 and not preview["requires_partial_consent"]
        result = await plugin.provider.apply(version_id, preview["projection_digest"])
        assert result["state"] == "applied"
        assert result["destination"]["item_id"] == "123"
        assert result["destination"]["uri"] == "library://playlist/123"
        again = await plugin.provider.apply(version_id, preview["projection_digest"])
        assert again["id"] == result["id"]
        assert playlists.import_playlist.await_count == 1
        m3u = playlists.import_playlist.call_args.args[0]
        assert m3u.count("spotify://track/" + "T" * 22) == 2
        builtin._read_m3u_file.assert_awaited_once_with("copy")

    asyncio.run(run())


def test_apply_requires_exact_preview_and_partial_consent(plugin):
    version_id, _, playlists = _apply_fixture(plugin, omitted=True)

    async def run():
        preview = await plugin.provider.apply_preview(version_id)
        assert preview["omitted"] == [{"position": 2, "state": "null"}]
        with pytest.raises(Exception, match="preview again"):
            await plugin.provider.apply(version_id, "stale-digest", True)
        with pytest.raises(Exception, match="Explicit consent"):
            await plugin.provider.apply(version_id, preview["projection_digest"])
        assert plugin.provider._store.get_apply_for_version(version_id) is None
        result = await plugin.provider.apply(version_id, preview["projection_digest"], True)
        assert result["state"] == "partial" and result["omitted_count"] == 1
        assert playlists.import_playlist.await_count == 1

    asyncio.run(run())


def test_empty_projection_creates_visible_builtin_playlist(plugin):
    version_id, _, playlists = _apply_fixture(plugin, empty=True)

    async def run():
        preview = await plugin.provider.apply_preview(version_id)
        result = await plugin.provider.apply(version_id, preview["projection_digest"])
        assert result["state"] == "applied" and result["projected_count"] == 0
        playlists.import_playlist.assert_not_called()
        assert playlists.create_playlist.call_args.kwargs["provider_instance_or_domain"] == "builtin"

    asyncio.run(run())


def test_apply_import_failure_is_uncertain_and_cannot_duplicate_on_retry(plugin):
    version_id, _, playlists = _apply_fixture(plugin)
    playlists.import_playlist.side_effect = RuntimeError("untrusted remote error")

    async def run():
        preview = await plugin.provider.apply_preview(version_id)
        with pytest.raises(Exception, match="outcome uncertain"):
            await plugin.provider.apply(version_id, preview["projection_digest"])
        result = await plugin.provider.apply(version_id, preview["projection_digest"])
        assert result["state"] == "uncertain" and not result["retryable"]
        assert playlists.import_playlist.await_count == 1
        assert "untrusted" not in result["error"]

    asyncio.run(run())


def test_apply_verifies_exact_destination_order_before_success(plugin):
    version_id, builtin, playlists = _apply_fixture(plugin)
    builtin._read_m3u_file.side_effect = AsyncMock(return_value="#EXTM3U\nspotify://track/WRONG\n")

    async def run():
        preview = await plugin.provider.apply_preview(version_id)
        with pytest.raises(Exception, match="outcome uncertain"):
            await plugin.provider.apply(version_id, preview["projection_digest"])
        assert (await plugin.provider.apply_status(version_id))["state"] == "uncertain"
        assert playlists.import_playlist.await_count == 1

    asyncio.run(run())


def test_apply_remains_available_when_archived_source_provider_is_offline(plugin):
    version_id, _, playlists = _apply_fixture(plugin)
    plugin.provider.mass.music.providers[0].available = False

    async def run():
        preview = await plugin.provider.apply_preview(version_id)
        result = await plugin.provider.apply(version_id, preview["projection_digest"])
        assert result["state"] == "applied"

    asyncio.run(run())
    playlists.import_playlist.assert_awaited_once()


def test_disconnected_apply_waits_for_known_outcome_and_does_not_duplicate(plugin):
    version_id, _, playlists = _apply_fixture(plugin)

    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        original = playlists.import_playlist.side_effect

        async def blocked_import(*args, **kwargs):
            started.set()
            await release.wait()
            return await original(*args, **kwargs)

        playlists.import_playlist.side_effect = blocked_import
        preview = await plugin.provider.apply_preview(version_id)
        operation = asyncio.create_task(plugin.provider.apply(version_id, preview["projection_digest"]))
        await started.wait()
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done() and plugin.provider._write_lock.locked()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        result = await plugin.provider.apply(version_id, preview["projection_digest"])
        assert result["state"] == "applied"
        assert playlists.import_playlist.await_count == 1

    asyncio.run(run())


def test_apply_requires_library_write_even_with_configuration_permission(plugin, monkeypatch):
    version_id, _, playlists = _apply_fixture(plugin)
    monkeypatch.setattr(plugin.module, "has_scope", lambda user, scope: scope != plugin.module.Scope.LIBRARY_WRITE)
    with pytest.raises(Exception, match="library write permission"):
        asyncio.run(plugin.provider.apply(version_id, "unused"))
    playlists.import_playlist.assert_not_called()


def _sync_fixture(plugin):
    store = plugin.provider._store
    sub = store.upsert_subscription("spotify", "account-a", "A" * 22, "spotify-a", "Source")
    job = store.begin_capture(sub["id"], "A")
    version = store.commit_capture(job, snapshot_before="A", snapshot_after="A", total=0, occurrences=[])
    return sub["id"], version


def test_sync_unchanged_uses_metadata_only_and_links_existing_version(plugin):
    subscription, version = _sync_fixture(plugin)

    async def run():
        queued = await plugin.provider.sync_now(subscription)
        assert queued["state"] == "queued" and plugin.handlers[-1]["priority"]
        await plugin.handlers[-1]["handler"]()
        status = await plugin.provider.sync_status(subscription)
        assert status["latest_job"]["state"] == "succeeded"
        assert status["latest_job"]["version_id"] == version
        assert len(plugin.provider._store.list_versions(subscription)) == 1
        plugin.module.capture_playlist.assert_not_called()

    asyncio.run(run())


def test_sync_changed_commits_new_version_without_applying(plugin):
    subscription, old = _sync_fixture(plugin)
    plugin.module.preview_playlist.return_value.update(snapshot_id="B")
    plugin.module.capture_playlist.return_value.update(snapshot_before="B", snapshot_after="B")

    async def run():
        await plugin.provider.sync_now(subscription)
        await plugin.handlers[-1]["handler"]()
        status = await plugin.provider.sync_status(subscription)
        assert status["latest_job"]["state"] == "succeeded"
        assert status["latest_job"]["version_id"] != old
        assert len(plugin.provider._store.list_versions(subscription)) == 2
        assert plugin.provider._store.list_applies() == []

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["capture", "account", "owner"])
def test_sync_failure_retains_prior_version_and_records_access(plugin, failure):
    subscription, old = _sync_fixture(plugin)
    plugin.module.preview_playlist.return_value.update(snapshot_id="B")
    if failure == "capture":
        plugin.module.capture_playlist.side_effect = RuntimeError("secret exception detail")
    elif failure == "account":
        plugin.module.preview_playlist.return_value["account_id"] = "another-account"
    else:
        plugin.provider.mass.webserver.auth.get_user.return_value = None

    async def run():
        await plugin.provider.sync_now(subscription)
        await plugin.handlers[-1]["handler"]()
        status = await plugin.provider.sync_status(subscription)
        assert status["latest_job"]["state"] == "failed"
        assert plugin.provider._store.get_subscription(subscription)["committed_version_id"] == old
        assert "secret" not in status["latest_job"]["error"]
        if failure != "capture":
            assert status["state"]["access_state"] == "access_denied"
            plugin.module.capture_playlist.assert_not_called()

    asyncio.run(run())


def test_sync_core_queued_cancel_reconciles_durable_job(plugin):
    subscription, _ = _sync_fixture(plugin)

    async def run():
        queued = await plugin.provider.sync_now(subscription)
        plugin.tasks[queued["task_id"]].status = "cancelled"
        status = await plugin.provider.sync_status(subscription)
        assert status["latest_job"]["state"] == "cancelled"
        plugin.module.preview_playlist.assert_not_called()

    asyncio.run(run())


def test_dispatcher_bounds_batch_to_ten_and_never_applies(plugin, monkeypatch):
    subscription, _ = _sync_fixture(plugin)
    plugin.provider._store.set_sync_policy(subscription, "scheduled", 3600, "admin", 0)
    monkeypatch.setattr(plugin.provider._store, "list_due", lambda now, limit: [{"id": subscription}] * 15)
    plugin.provider._run_sync = AsyncMock(side_effect=lambda *args: plugin.provider._store.cancel_sync(args[0]["id"]))
    asyncio.run(plugin.provider._dispatch_sync())
    assert plugin.provider._run_sync.await_count == 10
    assert plugin.provider._store.list_applies() == []


def test_sync_running_cancellation_finishes_both_jobs(plugin):
    subscription, _ = _sync_fixture(plugin)
    plugin.module.preview_playlist.return_value.update(snapshot_id="B")

    async def run():
        started = asyncio.Event()

        async def capture(*args, **kwargs):
            started.set()
            await asyncio.Future()

        plugin.module.capture_playlist.side_effect = capture
        await plugin.provider.sync_now(subscription)
        worker = asyncio.create_task(plugin.handlers[-1]["handler"]())
        await started.wait()
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert (await plugin.provider.sync_status(subscription))["latest_job"]["state"] == "cancelled"
        assert not any(job["state"] == "pending" for job in plugin.provider._store.list_jobs())

    asyncio.run(run())


def test_sync_policy_revision_and_scheduler_lifecycle(plugin):
    subscription, _ = _sync_fixture(plugin)

    async def run():
        await plugin.provider.loaded_in_mass()
        schedule = plugin.scheduled[0]
        assert schedule["schedule"] == {"every": 1}
        assert schedule["initial_delay"] == 60
        policy = await plugin.provider.set_sync_policy(subscription, 0, "scheduled", 3600)
        assert policy["revision"] == 1 and policy["initiating_user_id"] == "admin"
        with pytest.raises(Exception, match="revision conflict"):
            await plugin.provider.set_sync_policy(subscription, 0, "manual", 3600)
        await plugin.provider.unload()
        plugin.provider.mass.tasks.unregister_scheduled_task_and_wait.assert_any_await(
            "library_enrichment_sync_dispatcher", clear_persisted_state=False
        )

    asyncio.run(run())


def test_manual_capture_and_sync_exclude_each_other(plugin):
    subscription, _ = _sync_fixture(plugin)

    async def run():
        await plugin.provider.sync_now(subscription)
        with pytest.raises(Exception, match="already pending"):
            await plugin.provider.capture("spotify-a", "A" * 22)
        assert len(plugin.provider._store.list_jobs()) == 1

    asyncio.run(run())


def test_restart_interrupts_queued_sync_without_changing_committed_version(plugin):
    subscription, committed = _sync_fixture(plugin)
    plugin.provider._store.begin_sync(subscription)
    assert plugin.provider._store.recover_sync_pending() == 1
    status = asyncio.run(plugin.provider.sync_status(subscription))
    assert status["latest_job"]["state"] == "interrupted"
    assert plugin.provider._store.get_subscription(subscription)["committed_version_id"] == committed


def test_scheduled_checks_can_be_paused_while_source_provider_is_offline(plugin):
    subscription, _ = _sync_fixture(plugin)

    async def run():
        enabled = await plugin.provider.set_sync_policy(subscription, 0, "scheduled", 3600)
        plugin.provider.mass.music.providers = []
        paused = await plugin.provider.set_sync_policy(subscription, enabled["revision"], "manual", 3600)
        assert paused["mode"] == "manual"
        assert (await plugin.provider.sync_status(subscription))["state"]["next_check_at"] is None
        with pytest.raises(Exception, match="available Spotify"):
            await plugin.provider.set_sync_policy(subscription, paused["revision"], "scheduled", 3600)

    asyncio.run(run())


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
    assert len(plugin.registered) == 16
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
