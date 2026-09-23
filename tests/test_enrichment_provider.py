"""Exercise command authorization, capture orchestration and inspection boundaries."""

import asyncio
import importlib.util
import json
import sys
import threading
import types
import zipfile
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
    async def authenticated_request(_request):
        return auth.user
    web = types.SimpleNamespace(
        json_response=lambda payload, status=200: types.SimpleNamespace(
            status=status,
            text=json.dumps(payload),
        )
    )
    modules = {
        "aiohttp": {"web": web},
        "music_assistant_models.auth": {"Scope": Scope},
        "music_assistant_models.background_task": {"TaskSchedule": types.SimpleNamespace(hourly=lambda **kwargs: kwargs)},
        "music_assistant_models.enums": {"MediaType": MediaType},
        "music_assistant_models.errors": {"InsufficientPermissions": Error, "InvalidDataError": Error},
        "music_assistant.models.plugin": {"PluginProvider": object},
        "music_assistant.controllers.webserver.helpers.auth_middleware": {
            "get_current_user": lambda: auth.user if current.get() is unset else current.get(),
            "get_authenticated_user": authenticated_request,
            "has_scope": lambda user, scope: user.allowed and scope in getattr(user, "allowed_scopes", set(Scope)),
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
    routes = []
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
        get_library_item_by_prov_id=AsyncMock(return_value=None),
        get=AsyncMock(side_effect=AssertionError("inspection must not refresh")),
    )
    provider.mass = types.SimpleNamespace(
        storage_path=str(tmp_path),
        webserver=types.SimpleNamespace(
            auth=types.SimpleNamespace(get_user=AsyncMock(return_value=auth.user)),
            register_dynamic_route=lambda path, handler, method="*": routes.append((path, method, handler)) or (lambda: None),
        ),
        music=types.SimpleNamespace(
            providers=[spotify],
            get_controller=lambda media_type: controller,
            tracks=controller,
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
        routes=routes,
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


def test_sync_commits_archive_even_when_enabled_mirror_write_is_uncertain(plugin):
    subscription, old = _sync_fixture(plugin)
    plugin.module.preview_playlist.return_value.update(snapshot_id="B")
    plugin.module.capture_playlist.return_value.update(snapshot_before="B", snapshot_after="B")
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True,
        _read_m3u_file=AsyncMock(), _write_m3u_file=AsyncMock(),
        _get_playlist_lock=lambda _: asyncio.Lock(),
    )
    plugin.provider.mass.music.providers.append(builtin)
    plugin.provider.mass.music.playlists.import_playlist = AsyncMock(side_effect=RuntimeError("import response lost"))
    asyncio.run(plugin.provider.mirror_configure(subscription, True, True, 0))

    async def run():
        await plugin.provider.sync_now(subscription)
        await plugin.handlers[-1]["handler"]()
        status = await plugin.provider.sync_status(subscription)
        assert status["latest_job"]["state"] == "succeeded"
        new = plugin.provider._store.get_subscription(subscription)["committed_version_id"]
        assert new != old
        mirror = await plugin.provider.mirror_status(subscription)
        assert mirror["state"] == "uncertain" and mirror["applied_version_id"] is None
        await plugin.provider.sync_now(subscription)
        await plugin.handlers[-1]["handler"]()
        assert plugin.provider.mass.music.playlists.import_playlist.await_count == 1
        assert (await plugin.provider.sync_status(subscription))["latest_job"]["state"] == "succeeded"

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
        plugin.provider.diagnostics(),
        plugin.provider.inspect("track", "1"),
        plugin.provider.preview("spotify-a", "playlist"),
        plugin.provider.status(),
        plugin.provider.capture("spotify-a", "playlist"),
        plugin.provider.archive_version("unknown"),
        plugin.provider.archive_versions("unknown"),
        plugin.provider.sources("spotify-a"),
        plugin.provider.cancel("unknown"),
        plugin.provider.match_review("unknown"),
        plugin.provider.set_match_decision("unknown", "source", 0, "clear"),
        plugin.provider.itunes_inspect("missing.xml"),
    ):
        with pytest.raises(Exception, match="permission"):
            asyncio.run(coroutine)
    plugin.module.preview_playlist.assert_not_called()


def test_scoped_instance_required_and_all_commands_have_scope(plugin):
    with pytest.raises(Exception, match="accessible"):
        asyncio.run(plugin.provider.preview("spotify", "playlist"))
    asyncio.run(plugin.provider.loaded_in_mass())
    assert len(plugin.registered) == 37
    assert all(scope == "config.providers.write" for _, scope in plugin.registered)


def _write_itunes_xml(plugin):
    source = plugin.provider._itunes_import_root / "legacy.xml"
    source.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><plist version="1.0"><dict>'
        '<key>Library Persistent ID</key><string>LIBRARY</string>'
        '<key>Tracks</key><dict><key>1</key><dict>'
        '<key>Persistent ID</key><string>TRACK</string><key>Name</key><string>Song</string>'
        '<key>Kind</key><string>MPEG audio file</string>'
        '<key>Location</key><string>file://localhost/G:/Music/Artist/Song.mp3</string>'
        '</dict></dict><key>Playlists</key><array><dict>'
        '<key>Name</key><string>Favorites</string><key>Playlist Persistent ID</key><string>PLAYLIST</string>'
        '<key>Playlist Items</key><array><dict><key>Track ID</key><integer>1</integer></dict></array>'
        '</dict></array></dict></plist>',
        encoding="utf-8",
    )
    return source


def test_itunes_inspect_and_preview_are_staged_digest_bound_and_do_not_write_library(plugin):
    source = _write_itunes_xml(plugin)
    filesystem = types.SimpleNamespace(
        instance_id="filesystem-a", domain="filesystem_local", available=True, is_streaming_provider=False,
    )
    plugin.provider.mass.music.providers.append(filesystem)

    async def run():
        capabilities = await plugin.provider.capabilities()
        assert capabilities["itunes_import"] is True and capabilities["itunes_apply"] is True
        inspected = await plugin.provider.itunes_inspect(source.name)
        assert inspected["tracks_total"] == 1 and inspected["playlists_total"] == 1
        assert inspected["playlists"][0]["id"] == "PLAYLIST"
        preview = await plugin.provider.itunes_preview(
            inspected["inspection_id"], inspected["source_digest"],
            [{"source_root": "G:/Music/", "target_root": "Music", "provider_instance_id": "filesystem-a"}],
            ["PLAYLIST"],
        )
        assert preview["matched"] == 1 and preview["unresolved"] == 0
        assert preview["selected_playlists"] == 1 and len(preview["preview_digest"]) == 64
        assert plugin.provider._store.get_itunes_import(preview["inspection_id"])["status"] == "previewed"

    mapping = types.SimpleNamespace(provider_instance="filesystem-a", item_id="Music/Artist/Song.mp3")
    plugin.controller.get_library_item_by_prov_id.return_value = types.SimpleNamespace(
        item_id="library-track-1", provider_mappings=[mapping]
    )
    asyncio.run(run())
    plugin.controller.get.assert_not_called()


def _itunes_apply_preview(plugin, *, resolved=True, duplicate=False):
    source = _write_itunes_xml(plugin)
    if duplicate:
        text = source.read_text(encoding="utf-8")
        member = '<dict><key>Track ID</key><integer>1</integer></dict>'
        source.write_text(text.replace(member, member + member), encoding="utf-8")
    filesystem = types.SimpleNamespace(
        instance_id="filesystem-a", domain="filesystem_local", available=True, is_streaming_provider=False,
    )
    plugin.provider.mass.music.providers.append(filesystem)
    mapping = types.SimpleNamespace(provider_instance="filesystem-a", item_id="Music/Artist/Song.mp3")
    plugin.controller.get_library_item_by_prov_id.return_value = (
        types.SimpleNamespace(item_id="library-track-1", provider_mappings=[mapping]) if resolved else None
    )
    inspected = asyncio.run(plugin.provider.itunes_inspect(source.name))
    preview = asyncio.run(plugin.provider.itunes_preview(
        inspected["inspection_id"], inspected["source_digest"],
        [{"source_root": "G:/Music/", "target_root": "Music", "provider_instance_id": "filesystem-a"}],
        ["PLAYLIST"],
    ))
    return inspected, preview


def test_itunes_apply_always_suffixes_the_created_playlist_name_with_itunes(plugin):
    inspected, preview = _itunes_apply_preview(plugin)
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True, is_streaming_provider=False,
        _read_m3u_file=AsyncMock(),
    )
    plugin.provider.mass.music.providers.append(builtin)
    destination = types.SimpleNamespace(
        item_id="itunes-playlist-1",
        provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="itunes-file")],
    )
    seen_header = {}

    async def import_playlist(m3u, *, library_matching):
        seen_header["line"] = m3u.splitlines()[1]
        builtin._read_m3u_file.return_value = m3u
        return destination

    plugin.provider.mass.music.playlists.import_playlist = AsyncMock(side_effect=import_playlist)
    asyncio.run(plugin.provider.itunes_apply(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    ))
    # Source playlist in the fixture is named "Favorites" (see _write_itunes_xml).
    assert seen_header["line"] == "#PLAYLIST:Favorites (iTunes)"
    assert plugin.provider._store.get_itunes_apply(preview["inspection_id"])["requested_name"] == "Favorites (iTunes)"


def test_itunes_apply_long_playlist_name_keeps_the_itunes_suffix_within_the_length_limit(plugin):
    long_name = "A" * 200
    source = _write_itunes_xml(plugin)
    source.write_text(source.read_text(encoding="utf-8").replace("Favorites", long_name), encoding="utf-8")
    filesystem = types.SimpleNamespace(
        instance_id="filesystem-a", domain="filesystem_local", available=True, is_streaming_provider=False,
    )
    plugin.provider.mass.music.providers.append(filesystem)
    mapping = types.SimpleNamespace(provider_instance="filesystem-a", item_id="Music/Artist/Song.mp3")
    plugin.controller.get_library_item_by_prov_id.return_value = types.SimpleNamespace(
        item_id="library-track-1", provider_mappings=[mapping]
    )
    inspected = asyncio.run(plugin.provider.itunes_inspect(source.name))
    preview = asyncio.run(plugin.provider.itunes_preview(
        inspected["inspection_id"], inspected["source_digest"],
        [{"source_root": "G:/Music/", "target_root": "Music", "provider_instance_id": "filesystem-a"}],
        ["PLAYLIST"],
    ))
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True, is_streaming_provider=False,
        _read_m3u_file=AsyncMock(),
    )
    plugin.provider.mass.music.providers.append(builtin)
    destination = types.SimpleNamespace(
        item_id="itunes-playlist-1",
        provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="itunes-file")],
    )
    seen_header = {}

    async def import_playlist(m3u, *, library_matching):
        seen_header["line"] = m3u.splitlines()[1]
        builtin._read_m3u_file.return_value = m3u
        return destination

    plugin.provider.mass.music.playlists.import_playlist = AsyncMock(side_effect=import_playlist)
    asyncio.run(plugin.provider.itunes_apply(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    ))
    applied_name = seen_header["line"].removeprefix("#PLAYLIST:")
    assert len(applied_name) == 120
    assert applied_name.endswith(" (iTunes)")


def test_itunes_apply_uses_verified_library_ids_and_preserves_ordered_duplicates(plugin):
    inspected, preview = _itunes_apply_preview(plugin, duplicate=True)
    assert preview["matched"] == 2 and preview["unresolved"] == 0
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True, is_streaming_provider=False,
        _read_m3u_file=AsyncMock(),
    )
    plugin.provider.mass.music.providers.append(builtin)
    destination = types.SimpleNamespace(
        item_id="itunes-playlist-1",
        provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="itunes-file")],
    )

    async def import_playlist(m3u, *, library_matching):
        assert library_matching is False
        assert m3u.count("library://track/library-track-1") == 2
        builtin._read_m3u_file.return_value = m3u
        return destination

    plugin.provider.mass.music.playlists.import_playlist = AsyncMock(side_effect=import_playlist)
    args = dict(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    )
    applied = asyncio.run(plugin.provider.itunes_apply(**args))
    replay = asyncio.run(plugin.provider.itunes_apply(**args))
    assert applied["state"] == replay["state"] == "applied"
    assert applied["source_count"] == 2
    assert applied["destination"] == {"item_id": "itunes-playlist-1", "provider_instance_id": "builtin"}
    assert plugin.provider.mass.music.playlists.import_playlist.await_count == 1


def test_itunes_near_limit_playlist_preserves_all_occurrences_on_apply(plugin):
    source = _write_itunes_xml(plugin)
    member = '<dict><key>Track ID</key><integer>1</integer></dict>'
    source.write_text(source.read_text(encoding="utf-8").replace(member, member * 9_999), encoding="utf-8")
    plugin.provider.mass.music.providers.append(types.SimpleNamespace(
        instance_id="filesystem-a", domain="filesystem_local", available=True, is_streaming_provider=False,
    ))
    mapping = types.SimpleNamespace(provider_instance="filesystem-a", item_id="Music/Artist/Song.mp3")
    plugin.controller.get_library_item_by_prov_id.return_value = types.SimpleNamespace(
        item_id="library-track-1", provider_mappings=[mapping],
    )
    inspected = asyncio.run(plugin.provider.itunes_inspect(source.name))
    assert inspected["occurrences_total"] == 9_999
    preview = asyncio.run(plugin.provider.itunes_preview(
        inspected["inspection_id"], inspected["source_digest"],
        [{"source_root": "G:/Music/", "target_root": "Music", "provider_instance_id": "filesystem-a"}],
        ["PLAYLIST"],
    ))
    assert preview["selected_occurrences"] == preview["matched"] == 9_999
    assert preview["unresolved"] == preview["ambiguous"] == 0
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True, is_streaming_provider=False,
        _read_m3u_file=AsyncMock(),
    )
    plugin.provider.mass.music.providers.append(builtin)
    destination = types.SimpleNamespace(
        item_id="itunes-large-playlist", provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="large-file")],
    )

    async def import_playlist(m3u, *, library_matching):
        assert library_matching is False
        assert m3u.count("library://track/library-track-1") == 9_999
        builtin._read_m3u_file.return_value = m3u
        return destination

    plugin.provider.mass.music.playlists.import_playlist = AsyncMock(side_effect=import_playlist)
    args = dict(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    )
    applied = asyncio.run(plugin.provider.itunes_apply(**args))
    replay = asyncio.run(plugin.provider.itunes_apply(**args))
    assert applied["state"] == replay["state"] == "applied"
    assert applied["source_count"] == 9_999
    assert plugin.provider.mass.music.playlists.import_playlist.await_count == 1


def test_itunes_apply_fails_closed_for_unresolved_and_requires_library_write(plugin):
    inspected, preview = _itunes_apply_preview(plugin, resolved=False)
    plugin.provider.mass.music.playlists.import_playlist = AsyncMock()
    args = dict(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    )
    with pytest.raises(Exception, match="Explicit consent"):
        asyncio.run(plugin.provider.itunes_apply(**args))
    plugin.auth.user.allowed_scopes = {plugin.module.Scope.CONFIG_PROVIDERS_WRITE}
    with pytest.raises(Exception, match="library write"):
        asyncio.run(plugin.provider.itunes_apply(**args))
    plugin.provider.mass.music.playlists.import_playlist.assert_not_called()


def test_itunes_preview_rejects_unbound_or_streaming_path_provider(plugin):
    source = _write_itunes_xml(plugin)
    inspected = asyncio.run(plugin.provider.itunes_inspect(source.name))
    with pytest.raises(Exception, match="explicit filesystem"):
        asyncio.run(plugin.provider.itunes_preview(
            inspected["inspection_id"], inspected["source_digest"],
            [{"source_root": "G:/Music/", "target_root": "Music"}], ["PLAYLIST"],
        ))
    with pytest.raises(Exception, match="filesystem provider"):
        asyncio.run(plugin.provider.itunes_preview(
            inspected["inspection_id"], inspected["source_digest"],
            [{"source_root": "G:/Music/", "target_root": "Music", "provider_instance_id": "spotify-a"}],
            ["PLAYLIST"],
        ))


def test_itunes_preview_marks_duplicate_exact_provider_mappings_as_ambiguous(plugin):
    source = _write_itunes_xml(plugin)
    filesystem = types.SimpleNamespace(
        instance_id="filesystem-a", domain="filesystem_local", available=True, is_streaming_provider=False,
    )
    plugin.provider.mass.music.providers.append(filesystem)
    # Two provider_mappings entries both exactly match the resolved (provider, item_id)
    # key, so the library lookup itself cannot tell which library item is the real one.
    mapping = types.SimpleNamespace(provider_instance="filesystem-a", item_id="Music/Artist/Song.mp3")
    plugin.controller.get_library_item_by_prov_id.return_value = types.SimpleNamespace(
        item_id="library-track-1", provider_mappings=[mapping, mapping]
    )
    inspected = asyncio.run(plugin.provider.itunes_inspect(source.name))
    preview = asyncio.run(plugin.provider.itunes_preview(
        inspected["inspection_id"], inspected["source_digest"],
        [{"source_root": "G:/Music/", "target_root": "Music", "provider_instance_id": "filesystem-a"}],
        ["PLAYLIST"],
    ))
    assert preview["ambiguous"] == 1 and preview["matched"] == 0
    assert preview["playlists"][0]["rows"][0]["state"] == "ambiguous"


def test_itunes_apply_rejects_source_that_changed_after_preview(plugin):
    inspected, preview = _itunes_apply_preview(plugin)
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True, is_streaming_provider=False,
        _read_m3u_file=AsyncMock(),
    )
    plugin.provider.mass.music.providers.append(builtin)
    plugin.provider.mass.music.playlists.import_playlist = AsyncMock()

    # Mutate the staged XML on disk after preview committed a digest, before apply re-reads it.
    source = plugin.provider._itunes_import_root / "legacy.xml"
    source.write_text(source.read_text(encoding="utf-8").replace("Song", "Song (edited)"), encoding="utf-8")

    args = dict(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    )
    with pytest.raises(Exception, match="source changed"):
        asyncio.run(plugin.provider.itunes_apply(**args))
    plugin.provider.mass.music.playlists.import_playlist.assert_not_called()


def test_itunes_apply_rejects_filesystem_provider_that_disappeared_after_preview(plugin):
    inspected, preview = _itunes_apply_preview(plugin)
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True, is_streaming_provider=False,
        _read_m3u_file=AsyncMock(),
    )
    plugin.provider.mass.music.providers.append(builtin)
    plugin.provider.mass.music.playlists.import_playlist = AsyncMock()

    # The filesystem provider that resolved during preview is now gone (removed, rebound
    # to a different instance id, or just marked unavailable) by the time apply runs.
    plugin.provider.mass.music.providers = [
        candidate for candidate in plugin.provider.mass.music.providers
        if candidate.instance_id != "filesystem-a"
    ]

    args = dict(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    )
    with pytest.raises(Exception, match="unavailable to this user"):
        asyncio.run(plugin.provider.itunes_apply(**args))
    plugin.provider.mass.music.playlists.import_playlist.assert_not_called()


def test_itunes_apply_becomes_uncertain_and_is_not_retried_when_verification_fails_after_creation(plugin):
    inspected, preview = _itunes_apply_preview(plugin)
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True, is_streaming_provider=False,
        _read_m3u_file=AsyncMock(side_effect=OSError("disk read failed")),
    )
    plugin.provider.mass.music.providers.append(builtin)
    destination = types.SimpleNamespace(
        item_id="itunes-playlist-1",
        provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="itunes-file")],
    )
    import_playlist = AsyncMock(return_value=destination)
    plugin.provider.mass.music.playlists.import_playlist = import_playlist
    args = dict(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    )
    # The playlist was actually created in Music Assistant before the crash, so this must
    # not be reported as a clean failure, and a retry must not create a second playlist.
    with pytest.raises(Exception, match="outcome uncertain"):
        asyncio.run(plugin.provider.itunes_apply(**args))
    status = asyncio.run(plugin.provider.itunes_apply_status(preview["inspection_id"]))
    assert status["state"] == "uncertain"
    # A retry with the identical, still-matching intent must not blindly recreate the
    # playlist: it returns the existing uncertain outcome instead of trying again.
    retried = asyncio.run(plugin.provider.itunes_apply(**args))
    assert retried["state"] == "uncertain"
    assert import_playlist.await_count == 1


def test_itunes_preview_and_apply_enforce_the_occurrence_limit(plugin, monkeypatch):
    inspected, preview = _itunes_apply_preview(plugin)
    assert preview["selected_occurrences"] == 1
    # Lowering the configured limit below what was already resolved at preview time
    # (e.g. an admin retightened it) must still fail closed at apply.
    monkeypatch.setattr(plugin.module, "MAX_ITUNES_APPLY_OCCURRENCES", 0)
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True, is_streaming_provider=False,
        _read_m3u_file=AsyncMock(),
    )
    plugin.provider.mass.music.providers.append(builtin)
    plugin.provider.mass.music.playlists.import_playlist = AsyncMock()
    args = dict(
        inspection_id=preview["inspection_id"], revision=preview["revision"],
        source_digest=inspected["source_digest"], preview_digest=preview["preview_digest"],
        playlist_id="PLAYLIST",
    )
    with pytest.raises(Exception, match="exceeds its limit"):
        asyncio.run(plugin.provider.itunes_apply(**args))
    plugin.provider.mass.music.playlists.import_playlist.assert_not_called()

    # And preview itself refuses to stage a selection above the configured limit.
    source = _write_itunes_xml(plugin)
    monkeypatch.setattr(plugin.module, "MAX_ITUNES_APPLY_OCCURRENCES", 0)
    inspected_again = asyncio.run(plugin.provider.itunes_inspect(source.name))
    with pytest.raises(Exception, match="exceed 0 occurrences"):
        asyncio.run(plugin.provider.itunes_preview(
            inspected_again["inspection_id"], inspected_again["source_digest"],
            [], ["PLAYLIST"],
        ))


def test_diagnostics_report_build_and_redacted_store_health(plugin):
    subscription = plugin.provider._store.upsert_subscription(
        "spotify", "private-account", "private-playlist", "spotify-a", "Private name"
    )
    job = plugin.provider._store.begin_capture(subscription["id"], "private-snapshot")
    plugin.provider._store.fail_capture(job, "raw secret error")

    result = asyncio.run(plugin.provider.diagnostics(10))

    assert result["api_version"] == 1
    assert result["build"] == {
        "server_version": "2.10.4",
        "frontend_version": "2.10.4",
        "supported_server": "2.10.4",
        "server_compatible": True,
    }
    assert result["store"]["queues"]["capture"] == {"failed": 1}
    assert result["store"]["match_review"] == {"approved_sources": 0, "recent_decisions": []}
    assert result["store"]["recent_jobs"][0]["kind"] == "capture"
    serialized = json.dumps(result)
    for private_value in (
        subscription["id"], job, "private-account", "private-playlist",
        "private-snapshot", "Private name", "raw secret error",
    ):
        assert private_value not in serialized


def test_itunes_import_rejects_paths_outside_staging_and_changed_sources(plugin, tmp_path):
    outside = tmp_path / "outside.xml"
    outside.write_text("<plist><dict></dict></plist>", encoding="utf-8")
    with pytest.raises(Exception, match="inside an advertised directory"):
        asyncio.run(plugin.provider.itunes_inspect(str(outside)))
    source = _write_itunes_xml(plugin)
    inspected = asyncio.run(plugin.provider.itunes_inspect(source.name))
    source.write_text("<plist><dict></dict></plist>", encoding="utf-8")
    with pytest.raises(Exception, match="changed after inspection"):
        asyncio.run(
            plugin.provider.itunes_preview(
                inspected["inspection_id"], inspected["source_digest"], [], ["PLAYLIST"]
            )
        )


def test_itunes_zip_inspection_inventories_package_without_extracting_media(plugin, tmp_path):
    zip_root = tmp_path / "media" / "music-assistant-imports"
    zip_root.mkdir(parents=True)
    plugin.provider._itunes_zip_root = zip_root
    source = zip_root / "library.zip"
    xml = _write_itunes_xml(plugin).read_bytes()
    with zipfile.ZipFile(source, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("old/iTunes Library.xml", xml)
        archive.writestr("old/iTunes Media/Artist/Song.mp3", b"audio")

    result = asyncio.run(plugin.provider.itunes_inspect(str(source)))
    assert result["source_kind"] == "zip"
    assert result["package"]["media_files_total"] == 1
    assert result["package"]["selected_xml_path"] == "old/iTunes Library.xml"
    assert result["localization"]["state"] == "preview_only"
    assert result["playlists"][0]["id"] == "PLAYLIST"
    assert not any(zip_root.rglob("*.mp3"))


def test_itunes_zip_upload_stages_xml_only_transport_for_inspection(plugin, tmp_path):
    zip_root = tmp_path / "media" / "music-assistant-imports"
    plugin.provider._itunes_zip_root = zip_root
    xml = _write_itunes_xml(plugin).read_bytes()
    package_path = tmp_path / "transport.zip"
    with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("iTunes Library.xml", xml)
    payload = package_path.read_bytes()

    class Content:
        async def iter_chunked(self, _size):
            yield payload

    request = types.SimpleNamespace(
        headers={"X-Filename": "iTunes Library.zip"}, content_length=len(payload), content=Content()
    )
    response = asyncio.run(plugin.provider._itunes_zip_upload(request))
    result = json.loads(response.text)
    assert response.status == 200 and result["api_version"] == 1
    staged = Path(result["library_path"])
    assert staged.is_file() and staged.parent == zip_root
    assert asyncio.run(plugin.provider.itunes_inspect(str(staged)))["tracks_total"] == 1


def test_itunes_zip_upload_rejects_media_payload_and_cleans_partial_file(plugin, tmp_path):
    zip_root = tmp_path / "media" / "music-assistant-imports"
    plugin.provider._itunes_zip_root = zip_root
    package_path = tmp_path / "transport.zip"
    with zipfile.ZipFile(package_path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("iTunes Library.xml", _write_itunes_xml(plugin).read_bytes())
        archive.writestr("Music/song.mp3", b"audio")
    payload = package_path.read_bytes()

    class Content:
        async def iter_chunked(self, _size):
            yield payload

    request = types.SimpleNamespace(
        headers={"X-Filename": "transport.zip"}, content_length=len(payload), content=Content()
    )
    response = asyncio.run(plugin.provider._itunes_zip_upload(request))
    assert response.status == 400 and not list(zip_root.glob("*"))


def _provenance_fixture(plugin, account="account-a"):
    store = plugin.provider._store
    subscription = store.upsert_subscription("spotify", account, "P" * 22, "spotify-a", "Evidence")
    job = store.begin_capture(subscription["id"], "snapshot")
    payload = {
        "added_at": "2026-09-19T12:00:00Z",
        "added_by": {"id": "user-1"},
        "is_local": False,
        "item": {
            "id": "T" * 22,
            "type": "track",
            "external_ids": {"isrc": "USRC17607839"},
            "duration_ms": 123456,
            "explicit": True,
            "popularity": 73,
            "artists": [{"id": "R" * 22}, {"id": "S" * 22}],
            "album": {"id": "A" * 22, "release_date": "2024-03", "release_date_precision": "month"},
        },
    }
    version = store.commit_capture(
        job, snapshot_before="snapshot", snapshot_after="snapshot", total=3,
        occurrences=[
            {"position": 0, "state": "track", "source_item_id": "T" * 22, "source_payload": payload},
            {"position": 1, "state": "track", "source_item_id": "T" * 22, "source_payload": payload},
            {"position": 2, "state": "null", "source_item_id": None, "source_payload": None},
        ],
    )
    return subscription["id"], version


def test_provenance_is_typed_deterministic_redacted_and_never_refreshes(plugin):
    _, version = _provenance_fixture(plugin)
    result = asyncio.run(plugin.provider.provenance(version, limit=2))
    assert result["api_version"] == 1 and result["has_more"] is True
    assert result["items"][0]["provenance"]["subject"]["id"] == result["items"][1]["provenance"]["subject"]["id"]
    fields = result["items"][0]["provenance"]["fields"]
    assert fields["duration_ms"]["effective"]["value"] == 123456
    assert fields["duration_ms"]["effective"]["unit"] == "ms"
    assert fields["explicit"]["effective"]["value"] is True
    assert fields["spotify_artist_ids"]["effective"]["value"] == ["R" * 22, "S" * 22]
    assert fields["album_release_date"]["effective"]["date_precision"] == "month"
    assert "source_payload" not in json.dumps(result)
    plugin.controller.get.assert_not_called()
    plugin.controller.get_library_item.assert_not_called()
    plugin.module.preview_playlist.assert_not_called()
    plugin.module.capture_playlist.assert_not_called()


def test_provenance_adds_distinct_musicbrainz_identities_and_ordered_artist_credits(plugin):
    _, version = _provenance_fixture(plugin)
    library_track = types.SimpleNamespace(to_dict=lambda: {
        "external_ids": [
            ["musicbrainz_recordingid", "recording-id"],
            ["musicbrainz_trackid", "release-track-id"],
        ],
        "album": {"external_ids": [
            ["musicbrainz_albumid", "release-id"],
            ["musicbrainz_releasegroupid", "release-group-id"],
        ]},
        "artists": [
            {"name": "Second Credit", "external_ids": [["musicbrainz_artistid", "artist-2"]]},
            {"name": "First Credit", "external_ids": [["musicbrainz_artistid", "artist-1"]]},
        ],
    })
    plugin.controller.get_library_item_by_prov_id.return_value = library_track

    result = asyncio.run(plugin.provider.provenance(version, limit=2))
    fields = result["items"][0]["provenance"]["fields"]

    assert fields["musicbrainz_recording_id"]["effective"]["value"] == "recording-id"
    assert fields["musicbrainz_release_track_id"]["effective"]["value"] == "release-track-id"
    assert fields["musicbrainz_release_id"]["effective"]["value"] == "release-id"
    assert fields["musicbrainz_release_group_id"]["effective"]["value"] == "release-group-id"
    credits = fields["musicbrainz_artist_credits"]["effective"]["value"]
    assert [(row["position"], row["musicbrainz_artist_id"]) for row in credits] == [
        (0, "artist-2"), (1, "artist-1")
    ]
    assert fields["musicbrainz_recording_id"]["effective"]["source"] == "music_assistant.library"
    plugin.controller.get_library_item_by_prov_id.assert_awaited_once_with("T" * 22, "spotify-a")
    plugin.controller.get_library_item.assert_not_called()
    plugin.controller.get.assert_not_called()


def test_musicbrainz_identity_capability_is_explicit(plugin):
    capabilities = asyncio.run(plugin.provider.capabilities())
    assert capabilities["musicbrainz_identity_api_version"] == 1


def _next_provenance_version(plugin, subscription_id, snapshot, source_ids):
    job = plugin.provider._store.begin_capture(subscription_id, snapshot)
    return plugin.provider._store.commit_capture(
        job, snapshot_before=snapshot, snapshot_after=snapshot, total=len(source_ids),
        occurrences=[
            {"position": position, "state": "track", "source_item_id": source_id,
             "source_payload": {"item": {"id": source_id}}}
            for position, source_id in enumerate(source_ids)
        ],
    )


def test_musicbrainz_identity_snapshot_has_explicit_no_match_state(plugin):
    _, version = _provenance_fixture(plugin)
    plugin.controller.get_library_item_by_prov_id.return_value = None
    missing = asyncio.run(plugin.provider.provenance(version, limit=1))
    missing_fields = missing["items"][0]["provenance"]["fields"]
    assert missing_fields["musicbrainz_recording_id"]["effective"]["state"] == "not_loaded"
    assert missing_fields["musicbrainz_artist_credits"]["effective"]["state"] == "not_loaded"


def test_transient_identity_failure_preserves_prior_value_as_stale(plugin):
    subscription_id, first = _provenance_fixture(plugin)
    plugin.controller.get_library_item_by_prov_id.return_value = types.SimpleNamespace(to_dict=lambda: {
        "external_ids": [["musicbrainz_recordingid", "known-recording"]],
        "album": None,
        "artists": [],
    })
    asyncio.run(plugin.provider.provenance(first, limit=1))
    second = _next_provenance_version(plugin, subscription_id, "snapshot-2", ["T" * 22])
    plugin.controller.get_library_item_by_prov_id.side_effect = RuntimeError("library unavailable")
    failed = asyncio.run(plugin.provider.provenance(second, limit=1))
    failed_fields = failed["items"][0]["provenance"]["fields"]
    assert failed_fields["musicbrainz_recording_id"]["effective"]["state"] == "stale"
    assert failed_fields["musicbrainz_recording_id"]["effective"]["value"] == "known-recording"
    assert failed_fields["musicbrainz_release_id"]["effective"]["state"] == "inaccessible"


def test_first_identity_read_failure_is_inaccessible_and_retryable(plugin):
    _, version = _provenance_fixture(plugin)
    plugin.controller.get_library_item_by_prov_id.side_effect = RuntimeError("library unavailable")
    failed = asyncio.run(plugin.provider.provenance(version, limit=1))
    failed_fields = failed["items"][0]["provenance"]["fields"]
    assert failed_fields["musicbrainz_recording_id"]["effective"]["state"] == "inaccessible"
    assert failed_fields["musicbrainz_recording_id"]["effective"]["value"] is None

    plugin.controller.get_library_item_by_prov_id.side_effect = None
    plugin.controller.get_library_item_by_prov_id.return_value = types.SimpleNamespace(to_dict=lambda: {
        "external_ids": [["musicbrainz_recordingid", "recovered-recording"]],
        "album": None,
        "artists": [],
    })
    recovered = asyncio.run(plugin.provider.provenance(version, limit=1))
    recovered_field = recovered["items"][0]["provenance"]["fields"]["musicbrainz_recording_id"]["effective"]
    assert recovered_field["state"] == "value"
    assert recovered_field["value"] == "recovered-recording"
    assert plugin.controller.get_library_item_by_prov_id.await_count == 2


def test_identity_lookup_is_page_bounded_and_deduplicated(plugin):
    store = plugin.provider._store
    subscription = store.upsert_subscription("spotify", "account-a", "P" * 22, "spotify-a", "Paged")
    version = _next_provenance_version(
        plugin, subscription["id"], "paged", ["A" * 22, "B" * 22, "B" * 22, "C" * 22]
    )
    asyncio.run(plugin.provider.provenance(version, limit=2, offset=1))
    plugin.controller.get_library_item_by_prov_id.assert_awaited_once_with("B" * 22, "spotify-a")


def test_identity_overlay_is_frozen_per_archive_version(plugin):
    subscription_id, first = _provenance_fixture(plugin)

    def track(recording):
        return types.SimpleNamespace(to_dict=lambda: {
            "external_ids": [["musicbrainz_recordingid", recording]], "album": None, "artists": []
        })

    plugin.controller.get_library_item_by_prov_id.return_value = track("recording-a")
    first_read = asyncio.run(plugin.provider.provenance(first, limit=1))
    second = _next_provenance_version(plugin, subscription_id, "snapshot-2", ["T" * 22])
    plugin.controller.get_library_item_by_prov_id.return_value = track("recording-b")
    second_read = asyncio.run(plugin.provider.provenance(second, limit=1))
    plugin.controller.get_library_item_by_prov_id.return_value = track("recording-c")
    first_again = asyncio.run(plugin.provider.provenance(first, limit=1))

    def recording(response):
        return response["items"][0]["provenance"]["fields"]["musicbrainz_recording_id"]["effective"]["value"]

    assert recording(first_read) == recording(first_again) == "recording-a"
    assert recording(second_read) == "recording-b"
    assert plugin.controller.get_library_item_by_prov_id.await_count == 2


def test_musicbrainz_identity_parser_does_not_conflate_entities_or_reorder_credits(plugin):
    item = types.SimpleNamespace(to_dict=lambda: {
        "external_ids": [["musicbrainz_recordingid", "recording"]],
        "album": {"external_ids": [["musicbrainz_releasegroupid", "group"]]},
        "artists": [{"name": "No MBID", "external_ids": []}],
    })
    rows = plugin.provider._musicbrainz_identity_values(item, "2026-09-20T12:00:00+00:00")
    fields = {row["field_name"]: row for row in rows}
    assert fields["musicbrainz_recording_id"]["value"] == "recording"
    assert fields["musicbrainz_release_track_id"]["state"] == "missing"
    assert fields["musicbrainz_release_id"]["state"] == "missing"
    assert fields["musicbrainz_release_group_id"]["value"] == "group"
    assert fields["musicbrainz_artist_credits"]["value"] == [{
        "position": 0, "name": "No MBID", "musicbrainz_artist_id": None, "identity_state": "missing"
    }]


def test_provenance_states_pagination_unknown_version_and_account_isolation(plugin):
    _, first = _provenance_fixture(plugin, "account-a")
    _, second = _provenance_fixture(plugin, "account-b")
    one = asyncio.run(plugin.provider.provenance(first, limit=1, offset=0))
    two = asyncio.run(plugin.provider.provenance(second, limit=1, offset=0))
    assert one["items"][0]["provenance"]["subject"]["id"] != two["items"][0]["provenance"]["subject"]["id"]
    assert asyncio.run(plugin.provider.provenance(first, limit=1, offset=2))["items"][0]["provenance"] is None
    with pytest.raises(plugin.module.InvalidDataError, match="not found"):
        asyncio.run(plugin.provider.provenance("unknown"))
    for kwargs in ({"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1}):
        with pytest.raises(plugin.module.InvalidDataError, match="Provenance page"):
            asyncio.run(plugin.provider.provenance(first, **kwargs))


def test_spotify_provenance_distinguishes_missing_empty_not_loaded_and_inaccessible(plugin):
    base = {"position": 0, "state": "track", "source_item_id": "T" * 22}
    missing = plugin.provider._spotify_provenance_values("v", {**base, "source_payload": {"item": {}}}, "at")
    empty = plugin.provider._spotify_provenance_values(
        "v", {**base, "source_payload": {"item": {"id": "", "artists": []}}}, "at"
    )
    unloaded = plugin.provider._spotify_provenance_values("v", {**base, "source_payload": None}, "at")
    inaccessible = plugin.provider._spotify_provenance_values(
        "v", {**base, "source_payload": {"item": "not-an-object"}}, "at"
    )
    def by_name(rows):
        return {row["field_name"]: row for row in rows}
    assert by_name(missing)["spotify_track_id"]["state"] == "missing"
    assert by_name(empty)["spotify_track_id"]["state"] == "empty"
    assert by_name(empty)["spotify_artist_ids"]["state"] == "empty"
    assert by_name(unloaded)["spotify_track_id"]["state"] == "not_loaded"
    assert by_name(inaccessible)["spotify_track_id"]["state"] == "inaccessible"


def test_item_provenance_links_archive_and_reports_checkpoint_states(plugin):
    version, _, _ = _apply_fixture(plugin)
    preview = asyncio.run(plugin.provider.apply_preview(version))
    asyncio.run(plugin.provider.apply(version, preview["projection_digest"]))
    current = asyncio.run(plugin.provider.item_provenance("playlist", "123"))
    assert current["linked"] is True and current["state"] == "current"
    assert current["destination"]["kind"] == "archive"
    subscription_id = current["subscription"]["id"]
    plugin.provider._store.observe(subscription_id, "changed")
    assert asyncio.run(plugin.provider.item_provenance("playlist", "123"))["state"] == "source_changed"
    job = plugin.provider._store.begin_capture(subscription_id, "changed")
    assert asyncio.run(plugin.provider.item_provenance("playlist", "123"))["state"] == "capture_pending"
    plugin.provider._store.fail_capture(job, "stopped")
    assert asyncio.run(plugin.provider.item_provenance("playlist", "123"))["state"] == "capture_failed"


def test_item_provenance_links_playback_and_unknown_is_explicit(plugin):
    version, _, _ = _apply_fixture(plugin)
    subscription = plugin.provider._store.get_version(version)["subscription_id"]
    digest = "d" * 64
    plugin.provider._store.prepare_playback_projection(subscription, version, 0, digest, 2, 2, "[]")
    plugin.provider._store.mark_playback_projection_writing(subscription)
    plugin.provider._store.commit_playback_projection(subscription, "playback-1", "builtin", digest, "a" * 64)
    linked = asyncio.run(plugin.provider.item_provenance("playlist", "playback-1"))
    assert linked["linked"] is True and linked["destination"]["kind"] == "playback"
    unknown = asyncio.run(plugin.provider.item_provenance("playlist", "missing"))
    assert unknown == {"api_version": 1, "media_type": "playlist", "library_item_id": "missing",
                       "linked": False, "state": "unknown", "destination": None,
                       "subscription": None, "snapshots": None, "check": None}


def _match_fixture(plugin, *, account="account-a"):
    store = plugin.provider._store
    subscription = store.upsert_subscription("spotify", account, "A" * 22, "spotify-a", "Match archive")
    job = store.begin_capture(subscription["id"], "snapshot")
    version_id = store.commit_capture(
        job,
        snapshot_before="snapshot",
        snapshot_after="snapshot",
        total=3,
        occurrences=[
            {"position": 0, "state": "track", "source_item_id": "T" * 22},
            {"position": 1, "state": "track", "source_item_id": "T" * 22},
            {"position": 2, "state": "null", "source_item_id": None},
        ],
    )
    local_a = types.SimpleNamespace(
        instance_id="local-a", domain="filesystem_local", available=True, is_streaming_provider=False
    )
    local_b = types.SimpleNamespace(
        instance_id="local-b", domain="filesystem_local", available=True, is_streaming_provider=False
    )
    streaming = types.SimpleNamespace(
        instance_id="stream-a", domain="qobuz", available=True, is_streaming_provider=True
    )
    plugin.provider.mass.music.providers.extend((local_a, local_b, streaming))

    def mapping(domain, instance, item_id):
        return types.SimpleNamespace(provider_domain=domain, provider_instance=instance, item_id=item_id)

    plugin.controller.get_library_item_by_prov_id.return_value = types.SimpleNamespace(
        item_id="ma-track-7",
        provider_mappings=[
            mapping("spotify", "spotify-a", "T" * 22),
            mapping("filesystem_local", "local-a", "music/track.flac"),
            mapping("filesystem_local", "local-b", "archive/track.flac"),
            mapping("qobuz", "stream-a", "remote-track"),
        ],
    )
    return version_id


def test_match_review_uses_existing_local_mappings_and_preserves_duplicate_occurrences(plugin):
    version_id = _match_fixture(plugin)

    result = asyncio.run(plugin.provider.match_review(version_id, limit=3))

    assert result["candidate_freshness"] == "fresh"
    assert result["candidate_error"] is None
    assert [item["position"] for item in result["items"]] == [0, 1, 2]
    assert result["items"][0]["classification"] == "ambiguous"
    assert result["items"][0]["match"] == result["items"][1]["match"]
    candidates = result["items"][0]["match"]["candidates"]
    assert len(candidates) == 2
    assert {candidate["evidence"]["provider_instance_id"] for candidate in candidates} == {"local-a", "local-b"}
    assert all(candidate["evidence"]["kind"] == "existing_merged_provider_mapping" for candidate in candidates)
    assert result["items"][2]["match"] is None
    plugin.controller.get_library_item_by_prov_id.assert_awaited_once_with("T" * 22, "spotify-a")
    plugin.controller.get.assert_not_called()


def test_match_review_is_bounded_and_account_isolated(plugin):
    first = _match_fixture(plugin, account="account-a")
    second = _match_fixture(plugin, account="account-b")
    first_result = asyncio.run(plugin.provider.match_review(first, limit=1, offset=0))
    second_result = asyncio.run(plugin.provider.match_review(second, limit=1, offset=1))

    assert first_result["has_more"] is True and first_result["total"] == 3
    assert second_result["items"][0]["position"] == 1
    assert first_result["items"][0]["match"]["source"]["account_id"] == "account-a"
    assert second_result["items"][0]["match"]["source"]["account_id"] == "account-b"
    assert first_result["items"][0]["match"]["source"]["source_item_id"] == "T" * 22


def test_match_decision_requires_library_write_and_uses_revision_cas(plugin):
    version_id = _match_fixture(plugin)
    review = asyncio.run(plugin.provider.match_review(version_id, limit=1))
    candidate = review["items"][0]["match"]["candidates"][0]
    plugin.auth.user.allowed_scopes = {plugin.module.Scope.CONFIG_PROVIDERS_WRITE}
    with pytest.raises(Exception, match="library write permission"):
        asyncio.run(
            plugin.provider.set_match_decision(version_id, "T" * 22, 0, "approve", candidate["asset_id"])
        )
    plugin.auth.user.allowed_scopes = set(plugin.module.Scope)
    approved = asyncio.run(
        plugin.provider.set_match_decision(version_id, "T" * 22, 0, "approve", candidate["asset_id"])
    )
    assert approved["classification"] == "approved"
    assert approved["match"]["decision"]["asset_id"] == candidate["asset_id"]
    with pytest.raises(Exception, match="revision"):
        asyncio.run(
            plugin.provider.set_match_decision(
                version_id, "T" * 22, 0, "reject", candidate["asset_id"]
            )
        )
    cleared = asyncio.run(
        plugin.provider.set_match_decision(
            version_id, "T" * 22, approved["match"]["revision"], "clear"
        )
    )
    assert cleared["classification"] == "ambiguous"
    assert cleared["match"]["decision"]["action"] == "clear"
    assert cleared["match"]["approved_asset_id"] is None
    rejected = asyncio.run(
        plugin.provider.set_match_decision(
            version_id,
            "T" * 22,
            cleared["match"]["revision"],
            "reject",
            candidate["asset_id"],
        )
    )
    assert rejected["classification"] == "candidate"
    assert next(
        item for item in rejected["match"]["candidates"] if item["asset_id"] == candidate["asset_id"]
    )["rejected"] is True


def test_bulk_match_approval_uses_checked_subset_and_is_idempotent(plugin):
    version_id = _match_fixture(plugin)
    review = asyncio.run(plugin.provider.match_review(version_id, limit=1))
    match = review["items"][0]["match"]
    candidate = match["candidates"][0]
    approvals = [{
        "source_item_id": "T" * 22,
        "asset_id": candidate["asset_id"],
        "expected_revision": match["revision"],
    }]
    plugin.auth.user.allowed_scopes = {plugin.module.Scope.CONFIG_PROVIDERS_WRITE}
    with pytest.raises(Exception, match="library write permission"):
        asyncio.run(
            plugin.provider.approve_match_candidates(version_id, "browser-operation-1", approvals)
        )
    plugin.auth.user.allowed_scopes = set(plugin.module.Scope)
    result = asyncio.run(
        plugin.provider.approve_match_candidates(version_id, "browser-operation-1", approvals)
    )
    assert result["approved_count"] == 1
    assert result["idempotent_replay"] is False
    assert result["items"][0]["classification"] == "approved"
    assert "matches" not in result
    asyncio.run(
        plugin.provider.set_match_decision(version_id, "T" * 22, 1, "clear")
    )
    replay = asyncio.run(
        plugin.provider.approve_match_candidates(version_id, "browser-operation-1", approvals)
    )
    assert replay["idempotent_replay"] is True
    assert replay["items"][0]["match"]["revision"] == 1
    assert plugin.provider._store.get_match_overlay(
        "spotify", "account-a", "track", "T" * 22
    )["revision"] == 2


def test_bulk_match_approval_rejects_unreviewable_or_unbounded_requests(plugin):
    version_id = _match_fixture(plugin)
    with pytest.raises(Exception, match="1..200"):
        asyncio.run(plugin.provider.approve_match_candidates(version_id, "operation", []))
    with pytest.raises(Exception, match="unique reviewable"):
        asyncio.run(plugin.provider.approve_match_candidates(version_id, "operation", [{
            "source_item_id": "not-in-version", "asset_id": "asset", "expected_revision": 0,
        }]))


def test_playback_policy_preview_is_explicit_revisioned_and_strict(plugin):
    version_id = _match_fixture(plugin)
    review = asyncio.run(plugin.provider.match_review(version_id, limit=1))
    candidate = review["items"][0]["match"]["candidates"][0]
    local_instance = candidate["asset"]["locations"][0]["provider_instance_id"]
    asyncio.run(plugin.provider.set_match_decision(version_id, "T" * 22, 0, "approve", candidate["asset_id"]))
    subscription_id = plugin.provider._store.get_version(version_id)["subscription_id"]

    initial = asyncio.run(plugin.provider.playback_policy(subscription_id))
    assert initial["mode"] == "prefer_spotify" and initial["revision"] == 0
    policy = asyncio.run(plugin.provider.set_playback_policy(subscription_id, "local_only", 0))
    assert policy["revision"] == 1
    with pytest.raises(Exception, match="revision conflict"):
        asyncio.run(plugin.provider.set_playback_policy(subscription_id, "prefer_local", 0))

    preview = asyncio.run(plugin.provider.playback_preview(version_id))
    assert preview["mode"] == "local_only" and preview["policy_revision"] == 1
    assert preview["projected_count"] == 2 and preview["omitted_count"] == 1
    assert preview["rows"][0]["uri"].startswith(f"{local_instance}://track/")
    assert preview["rows"][0]["strict_provider"] == local_instance
    assert preview["gaps"] == [{"position": 2, "reason": "unsupported", "omitted": True, "fallback": None}]
    assert plugin.provider._store.get_version(version_id)["occurrences"][0]["source_item_id"] == "T" * 22


def test_prefer_local_and_prefer_spotify_report_explicit_fallback(plugin):
    version_id = _match_fixture(plugin)
    # Candidate discovery without approval is intentionally insufficient for local playback.
    asyncio.run(plugin.provider.match_review(version_id, limit=3))
    subscription_id = plugin.provider._store.get_version(version_id)["subscription_id"]
    asyncio.run(plugin.provider.set_playback_policy(subscription_id, "prefer_local", 0))
    local_preview = asyncio.run(plugin.provider.playback_preview(version_id))
    assert local_preview["rows"][0]["uri"] == f"spotify://track/{'T' * 22}"
    assert local_preview["rows"][0]["fallback"] == "spotify"
    assert local_preview["gaps"][0]["reason"] == "ambiguous"
    asyncio.run(plugin.provider.set_playback_policy(subscription_id, "prefer_spotify", 1))
    spotify_preview = asyncio.run(plugin.provider.playback_preview(version_id))
    assert spotify_preview["rows"][0]["selected_source"] == "spotify"
    assert spotify_preview["rows"][0]["fallback"] is None


def test_local_only_apply_writes_verified_strict_provider_sentinels(plugin):
    version_id = _match_fixture(plugin)
    review = asyncio.run(plugin.provider.match_review(version_id, limit=1))
    candidate = review["items"][0]["match"]["candidates"][0]
    local_instance = candidate["asset"]["locations"][0]["provider_instance_id"]
    asyncio.run(plugin.provider.set_match_decision(version_id, "T" * 22, 0, "approve", candidate["asset_id"]))
    subscription_id = plugin.provider._store.get_version(version_id)["subscription_id"]
    asyncio.run(plugin.provider.set_playback_policy(subscription_id, "local_only", 0))
    preview = asyncio.run(plugin.provider.playback_preview(version_id))
    builtin = types.SimpleNamespace(instance_id="builtin", domain="builtin", available=True, _read_m3u_file=AsyncMock())
    destination = types.SimpleNamespace(
        item_id="playback-1", provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="projection")]
    )

    async def import_playlist(m3u, *, library_matching):
        assert library_matching is False
        assert m3u.count(f"#EXTPROV:local_only||{local_instance}") == 2
        builtin._read_m3u_file.return_value = m3u
        return destination

    plugin.provider.mass.music.providers.append(builtin)
    plugin.provider.mass.music.playlists.import_playlist = AsyncMock(side_effect=import_playlist)
    result = asyncio.run(
        plugin.provider.playback_apply(
            version_id, preview["projection_digest"], preview["policy_revision"], allow_partial=True
        )
    )
    assert result["state"] == "applied"
    assert result["destination"]["item_id"] == "playback-1"
    assert plugin.provider._store.get_subscription(subscription_id)["applied_version_id"] is None


def test_local_only_apply_rejects_destination_that_drops_strict_sentinels(plugin):
    version_id = _match_fixture(plugin)
    review = asyncio.run(plugin.provider.match_review(version_id, limit=1))
    candidate = review["items"][0]["match"]["candidates"][0]
    asyncio.run(plugin.provider.set_match_decision(version_id, "T" * 22, 0, "approve", candidate["asset_id"]))
    subscription_id = plugin.provider._store.get_version(version_id)["subscription_id"]
    asyncio.run(plugin.provider.set_playback_policy(subscription_id, "local_only", 0))
    preview = asyncio.run(plugin.provider.playback_preview(version_id))
    builtin = types.SimpleNamespace(instance_id="builtin", domain="builtin", available=True, _read_m3u_file=AsyncMock())
    destination = types.SimpleNamespace(
        item_id="playback-1", provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="projection")]
    )

    async def import_playlist(m3u, *, library_matching):
        assert library_matching is False
        builtin._read_m3u_file.return_value = "\n".join(
            line for line in m3u.splitlines() if not line.startswith("#EXTPROV:local_only||")
        )
        return destination

    plugin.provider.mass.music.providers.append(builtin)
    plugin.provider.mass.music.playlists.import_playlist = AsyncMock(side_effect=import_playlist)
    with pytest.raises(plugin.module.InvalidDataError, match="outcome uncertain"):
        asyncio.run(
            plugin.provider.playback_apply(
                version_id, preview["projection_digest"], preview["policy_revision"], allow_partial=True
            )
        )
    assert asyncio.run(plugin.provider.playback_status(subscription_id))["projection"]["state"] == "uncertain"


def test_playback_projection_preserves_edited_destination(plugin):
    version_id = _match_fixture(plugin)
    preview = asyncio.run(plugin.provider.playback_preview(version_id))
    subscription_id = plugin.provider._store.get_version(version_id)["subscription_id"]
    builtin = types.SimpleNamespace(
        instance_id="builtin", domain="builtin", available=True,
        _read_m3u_file=AsyncMock(), _write_m3u_file=AsyncMock(),
        _get_playlist_lock=lambda _: asyncio.Lock(),
    )
    destination = types.SimpleNamespace(
        item_id="playback-1", provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="projection")]
    )

    async def import_playlist(m3u, *, library_matching):
        builtin._read_m3u_file.return_value = m3u
        return destination

    plugin.provider.mass.music.providers.append(builtin)
    plugin.provider.mass.music.playlists.import_playlist = AsyncMock(side_effect=import_playlist)
    plugin.provider.mass.music.playlists.get_library_item = AsyncMock(return_value=destination)
    first = asyncio.run(plugin.provider.playback_apply(
        version_id, preview["projection_digest"], preview["policy_revision"], allow_partial=True,
    ))
    assert first["state"] == "applied"
    builtin._read_m3u_file.return_value += "spotify://track/user-added\n"
    with pytest.raises(plugin.module.InvalidDataError, match="destination was edited"):
        asyncio.run(plugin.provider.playback_apply(
            version_id, preview["projection_digest"], preview["policy_revision"], allow_partial=True,
        ))
    builtin._write_m3u_file.assert_not_awaited()
    assert builtin._read_m3u_file.return_value.endswith("spotify://track/user-added\n")
    failed = asyncio.run(plugin.provider.playback_status(subscription_id))["projection"]
    assert failed["state"] == "failed"
    detached = asyncio.run(plugin.provider.playback_detach(
        subscription_id, "playback-1", failed["destination_content_digest"],
    ))
    assert detached["detached_destination"]["item_id"] == "playback-1"
    assert asyncio.run(plugin.provider.playback_status(subscription_id))["projection"]["state"] == "not_applied"
    assert builtin._read_m3u_file.return_value.endswith("spotify://track/user-added\n")


def test_maintained_mirror_preserves_user_edits_and_detaches_without_deleting(plugin):
    version_id, builtin, playlists = _apply_fixture(plugin)
    subscription_id = plugin.provider._store.get_version(version_id)["subscription_id"]
    builtin._write_m3u_file = AsyncMock()
    builtin._get_playlist_lock = lambda _: asyncio.Lock()
    playlists.get_library_item = AsyncMock(return_value=types.SimpleNamespace(
        item_id="123", provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="copy")],
    ))
    preview = asyncio.run(plugin.provider.mirror_preview(subscription_id))
    assert preview["projected_count"] == 2 and not preview["requires_partial_consent"]
    configured = asyncio.run(plugin.provider.mirror_configure(subscription_id, True, False, 0))
    assert configured["state"] == "pending"
    applied = asyncio.run(plugin.provider.mirror_apply(
        subscription_id, version_id, preview["projection_digest"],
    ))
    assert applied["state"] == "applied" and applied["applied_version_id"] == version_id
    assert playlists.import_playlist.await_count == 1
    assert builtin._read_m3u_file.return_value.count(f"spotify://track/{'T' * 22}") == 2
    provenance = asyncio.run(plugin.provider.item_provenance("playlist", "123"))
    assert provenance["destination"]["kind"] == "mirror" and provenance["state"] == "current"
    builtin._read_m3u_file.return_value += "spotify://track/user-added\n"
    with pytest.raises(plugin.module.InvalidDataError, match="destination was edited"):
        asyncio.run(plugin.provider.mirror_apply(subscription_id, version_id, preview["projection_digest"]))
    conflict = asyncio.run(plugin.provider.mirror_status(subscription_id))
    assert conflict["state"] == "conflict" and conflict["enabled"] == 0
    assert conflict["applied_version_id"] == version_id
    assert asyncio.run(plugin.provider.item_provenance("playlist", "123"))["state"] == "mirror_conflict"
    builtin._write_m3u_file.assert_not_awaited()
    detached = asyncio.run(plugin.provider.mirror_detach(
        subscription_id, "123", conflict["destination_content_digest"],
    ))
    assert detached["state"] == "detached" and builtin._read_m3u_file.return_value.endswith("user-added\n")
    assert asyncio.run(plugin.provider.item_provenance("playlist", "123"))["state"] == "mirror_detached"


def test_maintained_mirror_updates_changed_source_and_skips_unchanged(plugin, monkeypatch):
    version_a, builtin, playlists = _apply_fixture(plugin)
    store = plugin.provider._store
    subscription_id = store.get_version(version_a)["subscription_id"]
    builtin._get_playlist_lock = lambda _: asyncio.Lock()
    async def write_m3u(_item_id, _name, parsed):
        builtin._read_m3u_file.return_value = parsed
    builtin._write_m3u_file = AsyncMock(side_effect=write_m3u)
    playlists.get_library_item = AsyncMock(return_value=types.SimpleNamespace(
        item_id="123", provider_mappings=[types.SimpleNamespace(provider_instance="builtin", item_id="copy")],
    ))
    helpers = types.ModuleType("music_assistant.helpers.playlists")
    helpers.parse_m3u = lambda m3u: m3u
    monkeypatch.setitem(sys.modules, "music_assistant.helpers.playlists", helpers)
    asyncio.run(plugin.provider.mirror_configure(subscription_id, True, False, 0))
    preview_a = asyncio.run(plugin.provider.mirror_preview(subscription_id))
    asyncio.run(plugin.provider.mirror_apply(subscription_id, version_a, preview_a["projection_digest"]))
    again = asyncio.run(plugin.provider.mirror_apply(subscription_id, version_a, preview_a["projection_digest"]))
    assert again["applied_version_id"] == version_a
    assert playlists.import_playlist.await_count == 1
    builtin._write_m3u_file.assert_not_awaited()

    capture_id = store.begin_capture(subscription_id, "snapshot-b")
    version_b = store.commit_capture(
        capture_id, snapshot_before="snapshot-b", snapshot_after="snapshot-b", total=3,
        occurrences=[
            {"position": index, "state": "track", "source_item_id": source_id}
            for index, source_id in enumerate(("T" * 22, "U" * 22, "T" * 22))
        ],
    )
    preview_b = asyncio.run(plugin.provider.mirror_preview(subscription_id))
    updated = asyncio.run(plugin.provider.mirror_apply(subscription_id, version_b, preview_b["projection_digest"]))
    assert updated["state"] == "applied" and updated["applied_version_id"] == version_b
    assert updated["destination_item_id"] == "123"
    assert builtin._write_m3u_file.await_count == 1
    assert [line for line in builtin._read_m3u_file.return_value.splitlines() if line.startswith("spotify://")] == [
        f"spotify://track/{'T' * 22}", f"spotify://track/{'U' * 22}", f"spotify://track/{'T' * 22}",
    ]
    assert store.get_version(version_a)["occurrences"][0]["source_item_id"] == "T" * 22


def test_match_review_returns_stale_overlay_without_retry_or_ma_mutation(plugin):
    version_id = _match_fixture(plugin)
    fresh = asyncio.run(plugin.provider.match_review(version_id, limit=1))
    plugin.controller.get_library_item_by_prov_id.reset_mock()
    plugin.controller.get_library_item_by_prov_id.side_effect = RuntimeError("database temporarily unavailable")

    stale = asyncio.run(plugin.provider.match_review(version_id, limit=1))

    assert stale["candidate_freshness"] == "stale"
    assert stale["candidate_error"] == "library_read_failed"
    assert stale["items"][0]["match"] == fresh["items"][0]["match"]
    plugin.controller.get_library_item_by_prov_id.assert_awaited_once()
    plugin.controller.get.assert_not_called()


@pytest.mark.parametrize("args", [{"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1}])
def test_match_review_rejects_unbounded_pages(plugin, args):
    with pytest.raises(Exception, match="Match review page"):
        asyncio.run(plugin.provider.match_review("version", **args))
    plugin.controller.get_library_item_by_prov_id.assert_not_called()


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
