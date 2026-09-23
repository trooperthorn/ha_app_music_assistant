"""The build-time edits of the server (no network)."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "music_assistant_lm" / "patches"))

import folder_browser as folders  # noqa: E402
import hass_source_select as patch  # noqa: E402
import library_trash as trash  # noqa: E402
import play_source_steer as steer  # noqa: E402
import playlist_bridge as bridge  # noqa: E402
import sendspin_cast_delay as cast_delay  # noqa: E402
import sendspin_cast_status as cast_status  # noqa: E402
import sendspin_opus_bitrate as opus  # noqa: E402

# streams_audio_2_10_4.py and sendspin_player_2_10_4.py are verbatim pinned
# copies of upstream, which deliberately uses PEP 758
# parenthesis-free multi-exception syntax (`except A, B:`, server PR #4254).
# That syntax is only valid on Python 3.14+ (the server's own requires-python
# and the container's venv); any test that compile()s or ast.parse()s one of
# these fixtures, or a patch result derived from one, cannot run below 3.14.
requires_py314 = pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="fixtures use PEP 758 syntax, valid only on Python 3.14+",
)

# the modules exactly as the pinned server release ships them
UPSTREAM = ROOT / "tests" / "fixtures" / "hass_player_2_10_4.py"
QUEUE_LOADER = ROOT / "tests" / "fixtures" / "queue_loader_2_10_4.py"
STREAMS_AUDIO = ROOT / "tests" / "fixtures" / "streams_audio_2_10_4.py"
# aiosendspin as server 2.10.4 pins it (aiosendspin[server]==9.1.1)
AIOSENDSPIN_CODECS = ROOT / "tests" / "fixtures" / "aiosendspin_codecs_9_1_1.py"
AIOSENDSPIN_PLAYER_V1 = ROOT / "tests" / "fixtures" / "aiosendspin_player_v1_9_1_1.py"
SENDSPIN_PLAYER = ROOT / "tests" / "fixtures" / "sendspin_player_2_10_4.py"
CHROMECAST_SENDSPIN_BRIDGE = ROOT / "tests" / "fixtures" / "chromecast_sendspin_bridge_2_10_4.py"


@requires_py314
def test_cast_receiver_states_reach_player_without_log_text() -> None:
    original = CHROMECAST_SENDSPIN_BRIDGE.read_text(encoding="utf-8")
    patched = cast_status.apply(original)
    assert cast_status.apply(patched) == patched
    assert "sendspin_cast_status.py" in (ROOT / "music_assistant_lm" / "Dockerfile").read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="Cast status anchor found 0 times"):
        cast_status.apply("class SendspinCastController: pass")

    tree = ast.parse(patched)
    controller = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SendspinCastController")

    class BaseController:
        def __init__(self, namespace):
            self.namespace = namespace

    class Logger:
        def error(self, *_args):
            pass

    namespace = {
        "BaseController": BaseController,
        "SENDSPIN_CAST_NAMESPACE": "cast",
        "_CAST_LOG_LEVEL_MAP": {},
        "logging": __import__("logging"),
    }
    exec("from __future__ import annotations\n" + ast.unparse(controller), namespace)  # noqa: S102
    seen: list[str] = []
    receiver = namespace["SendspinCastController"](Logger(), on_cast_status=seen.append)
    for state in ("connecting", "connected", "playing", "stopped", "error", "unknown"):
        receiver._handle_status({"state": state, "message": "private receiver diagnostic"})
    assert seen == ["connecting", "connected", "playing", "stopped", "error"]

    bridge = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SendspinChromecastBridge")
    methods = [
        node
        for node in bridge.body
        if isinstance(node, ast.FunctionDef) and node.name in {"_publish_cast_receiver_status", "_on_stream_start"}
    ]
    method_source = "\n".join("\n".join(f"    {line}" for line in ast.unparse(method).splitlines()) for method in methods)

    class PlayerCommandFailed(Exception):
        pass

    runtime = {"PlayerCommandFailed": PlayerCommandFailed}
    exec("from __future__ import annotations\nclass Bridge:\n" + method_source, runtime)  # noqa: S102
    player = type("Player", (), {"extra_attributes": {}, "update_state": lambda self: seen.append("event")})()
    bridge_instance = runtime["Bridge"]()
    bridge_instance._bridge_client_id = "cast-id"
    bridge_instance.mass = type(
        "Mass",
        (),
        {
            "players": type(
                "Players",
                (),
                {
                    "get_player": lambda self, _: player,
                },
            )()
        },
    )()
    bridge_instance._publish_cast_receiver_status("connecting")
    bridge_instance._publish_cast_receiver_status("connecting")
    bridge_instance._publish_cast_receiver_status("error", "launch_timeout")
    bridge_instance._publish_cast_receiver_status("error", "private receiver diagnostic")
    assert player.extra_attributes == {
        "sendspin_cast_state": "error",
        "sendspin_cast_failure": "receiver_error",
    }
    bridge_instance._publish_cast_receiver_status("connected")
    assert player.extra_attributes == {"sendspin_cast_state": "connected"}
    bridge_instance._publish_cast_receiver_status("error", "device_unavailable")
    assert player.extra_attributes["sendspin_cast_failure"] == "device_unavailable"
    assert 'self._resolve_cast_app_ready(PlayerCommandFailed(f"{self.cast_player.display_name} is unavailable."))' in patched
    assert 'PlayerCommandFailed(f"Timed out launching Sendspin on {self.cast_player.display_name}.")' in patched
    assert 'self._resolve_cast_app_ready(PlayerCommandFailed(f"Failed to launch Sendspin on {self.cast_player.display_name}."))' in patched
    assert seen[-5:] == ["event"] * 5

    class BridgeLogger:
        def debug(self, *_args):
            pass

        def warning(self, *_args):
            pass

    failed: list[BaseException] = []
    bridge_instance.logger = BridgeLogger()
    bridge_instance.cast_player = type("CastPlayer", (), {"available": False, "display_name": "Kitchen TV"})()
    bridge_instance.ensure_cast_app_ready = lambda: None
    bridge_instance._resolve_cast_app_ready = failed.append
    bridge_instance._on_stream_start(type("Request", (), {"connection_reason": "playback"})())
    assert isinstance(failed[0], PlayerCommandFailed)
    assert "unavailable" in str(failed[0])
    assert player.extra_attributes["sendspin_cast_failure"] == "device_unavailable"


@requires_py314
def test_cast_bridge_delay_is_configurable_before_receiver_connects() -> None:
    source = SENDSPIN_PLAYER.read_text(encoding="utf-8")
    patched = cast_delay.apply(source)
    assert cast_delay.apply(patched) == patched
    assert 'underlying.provider.domain == "chromecast"' in patched
    assert "PlayerCommand.SET_STATIC_DELAY in player_role.state_supported_commands" in patched
    assert "range=(0, 5000)" in patched
    assert "sendspin_cast_delay.py" in (ROOT / "music_assistant_lm" / "Dockerfile").read_text(encoding="utf-8")
    with pytest.raises(SystemExit, match="Cast delay anchor found 0 times"):
        cast_delay.apply("class SendspinPlayer: pass")

    tree = ast.parse(patched)
    player = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SendspinPlayer")
    method = next(node for node in player.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_config_entries")
    method_source = "\n".join(f"    {line}" for line in ast.unparse(method).splitlines())
    namespace = {
        "ConfigEntry": lambda **kwargs: kwargs,
        "ConfigEntryType": type("ConfigEntryType", (), {"INTEGER": "integer"}),
        "CONF_SENDSPIN_STATIC_DELAY": "sendspin_static_delay",
        "PlayerCommand": type("PlayerCommand", (), {"SET_STATIC_DELAY": "set_static_delay"}),
        "HIDDEN_ANNOUNCE_VOLUME_CONFIG_ENTRIES": [],
    }
    class_source = "class Base:\n    async def get_config_entries(self): return []\nclass Player(Base):\n" + method_source
    exec(class_source, namespace)  # noqa: S102
    instance = namespace["Player"]()
    instance.static_delay_default_ms = 330
    instance._hass_announce_entity_id = None
    instance._player_role = None
    for domain, expected in (("chromecast", 1), ("airplay", 0)):
        underlying = type("Underlying", (), {"provider": type("Provider", (), {"domain": domain})()})()
        players = type("Players", (), {"get_player": lambda self, _, u=underlying: u})()
        instance.mass = type("Mass", (), {"players": players})()
        instance.underlying_player_id = "base-id"
        entries = asyncio.run(instance.get_config_entries())
        assert len(entries) == expected
        if expected:
            assert entries[0]["key"] == "sendspin_static_delay"
            assert entries[0]["default_value"] == 330
            assert entries[0]["range"] == (0, 5000)
    instance._player_role = type("Role", (), {"get_supported_formats": lambda self: [], "state_supported_commands": {"set_static_delay"}})()
    assert len(asyncio.run(instance.get_config_entries())) == 1


def test_the_fixture_matches_the_pinned_server_release() -> None:
    dockerfile = (ROOT / "music_assistant_lm" / "Dockerfile").read_text(encoding="utf-8")
    assert 'ARG SERVER_VERSION="2.10.4"' in dockerfile, (
        "the server moved; refresh the tests/fixtures/*_<version>.py copies from the new "
        "release and re-check the anchors in music_assistant_lm/patches/*.py"
    )


def _parse_server_version(dockerfile_text: str) -> tuple[int, ...]:
    """Parse a Dockerfile's pinned SERVER_VERSION as a tuple of ints.

    CalVer-ish MAJOR.MINOR.PATCH, but tolerate a trailing dev/beta suffix
    (e.g. "2.11.0b1") by only parsing the leading digit groups. Split out
    from `_pinned_server_version` so the parsing/compare logic itself can be
    unit tested against synthetic input without touching the real Dockerfile.
    """
    match = re.search(r'ARG SERVER_VERSION="(\d+)\.(\d+)\.(\d+)', dockerfile_text)
    assert match, 'could not find ARG SERVER_VERSION="MAJOR.MINOR.PATCH..." in the Dockerfile'
    return tuple(int(part) for part in match.groups())


def _pinned_server_version() -> tuple[int, ...]:
    """Parse the real Dockerfile's pinned SERVER_VERSION as a tuple of ints."""
    dockerfile = (ROOT / "music_assistant_lm" / "Dockerfile").read_text(encoding="utf-8")
    return _parse_server_version(dockerfile)


def test_parse_server_version_compares_correctly_around_the_2_11_boundary() -> None:
    """The tripwire's version compare must pass at 2.10.5 and fail at 2.11.0."""
    assert _parse_server_version('ARG SERVER_VERSION="2.10.5"') < (2, 11, 0)
    assert _parse_server_version('ARG SERVER_VERSION="2.11.0"') >= (2, 11, 0)
    # a trailing dev/beta suffix must not break parsing
    assert _parse_server_version('ARG SERVER_VERSION="2.11.0b1"') >= (2, 11, 0)


def test_playlist_bridge_requires_responsibility_review_at_2_11() -> None:
    """Migration parity does not establish archive parity at an upstream bump."""
    assert _pinned_server_version() < (2, 11, 0), (
        "SERVER_VERSION reached 2.11.0+: review migration parity and preserve or "
        "replace archival before retiring the bridge. Read "
        "docs/playlist-bridge-vs-upstream.md. Selected enrichment captures do not "
        "yet replace builtin mirror application or bulk controls."
    )


def test_apply_adds_source_mirroring_and_select_source() -> None:
    patched = patch.apply(UPSTREAM.read_text(encoding="utf-8"))

    tree = ast.parse(patched)
    player = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HomeAssistantPlayer")
    methods = {node.name for node in player.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    assert "select_source" in methods
    assert "_set_hass_source_list" in methods

    assert 'elif key == "source_list":' in patched
    assert 'self._extra_attributes["hass_source"]' in patched
    assert 'service="select_source"' in patched
    assert "from music_assistant_models.errors import PlayerCommandFailed" in patched
    # the External passive source the provider adds for next/previous stays
    assert 'if x.id == "External"]' in patched


def test_apply_is_idempotent() -> None:
    once = patch.apply(UPSTREAM.read_text(encoding="utf-8"))
    assert patch.apply(once) == once


def test_apply_refuses_a_provider_that_moved() -> None:
    with pytest.raises(SystemExit, match="anchor found 0 times"):
        patch.apply("class HomeAssistantPlayer:\n    pass\n")


@requires_py314
def test_steer_marks_queue_items_and_prefers_their_provider() -> None:
    loader = steer.apply(QUEUE_LOADER.read_text(encoding="utf-8"), steer.EDITS[steer.QUEUE_LOADER])
    audio = steer.apply(STREAMS_AUDIO.read_text(encoding="utf-8"), steer.EDITS[steer.STREAMS_AUDIO])

    for text in (loader, audio):
        ast.parse(text)
    loader_tree = ast.parse(loader)
    names = {node.name for node in ast.walk(loader_tree) if isinstance(node, ast.FunctionDef)}
    assert "_play_source_steer" in names
    assert "_play_source_provider" in names
    # the request's uri is read before it resolves to the library item
    assert loader.index("steer_uri, strict_provider = self._play_source_steer(item)") < loader.index(
        "media_item = await self.mass.music.get_item_by_uri(steer_uri)"
    )
    assert 'queue_item.extra_attributes["preferred_provider"] = steer_provider' in loader
    assert 'queue_item.extra_attributes["strict_provider"] = steer_provider' in loader
    # the streams side puts it ahead of the quality order, before the user's filter
    assert "preferred_providers = [steer, *preferred_providers]" in audio
    assert audio.index("preferred_providers = [steer, *preferred_providers]") < audio.index(
        "candidates = self._get_streamdetail_candidates("
    )
    # strict applies to initial candidates, cached details, capacity retry candidates,
    # and disables on-demand cross-provider matching.
    assert "# strict plays may never reuse details from another provider" in audio
    assert "and not strict_stream_provider.is_streaming_provider" in audio
    assert "if not provider.is_streaming_provider" in audio
    assert "not strict_provider  # trooperthorn: play_source_steer" in audio
    assert "Local-only provider {strict_provider!r} cannot serve" in audio


@requires_py314
def test_strict_steer_contract_rejects_bad_targets_and_consumes_m3u_marker() -> None:
    loader = steer.apply(QUEUE_LOADER.read_text(encoding="utf-8"), steer.EDITS[steer.QUEUE_LOADER])
    tree = ast.parse(loader)
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in {"_play_source_steer", "_play_source_provider"}
    }
    source = "from __future__ import annotations\nclass Ctl:\n" + "\n".join(
        "\n".join(f"    {line}" if line else "" for line in ast.unparse(methods[name]).splitlines())
        for name in ("_play_source_steer", "_play_source_provider")
    )

    class InvalidDataError(Exception):
        pass

    namespace = {"InvalidDataError": InvalidDataError}
    exec(source, namespace)  # noqa: S102
    ctl = namespace["Ctl"]()

    class Provider:
        def __init__(self, instance_id, domain, *, available=True, streaming=False):
            self.instance_id = instance_id
            self.domain = domain
            self.available = available
            self.is_streaming_provider = streaming

    local = Provider("filesystem_local--abc", "filesystem_local")
    spotify = Provider("spotify--abc", "spotify", streaming=True)
    unavailable = Provider("filesystem_local--gone", "filesystem_local", available=False)
    providers = {item.instance_id: item for item in (local, spotify, unavailable)}
    providers["filesystem_local"] = local
    ctl.mass = type("Mass", (), {"get_provider": lambda self, key, return_unavailable=False: providers.get(key)})()

    uri = "filesystem_local--abc://track/Music/example.flac"
    assert ctl._play_source_steer(f"local-only:{uri}") == (uri, "filesystem_local--abc")
    assert ctl._play_source_provider(uri, "filesystem_local--abc") == "filesystem_local--abc"
    assert ctl._play_source_provider(uri, "filesystem_local") == "filesystem_local"
    # Ordinary source steering still accepts streaming providers and remains a preference.
    assert ctl._play_source_provider("spotify--abc://track/1", None) == "spotify--abc"
    with pytest.raises(InvalidDataError, match="expected local-only:<provider-uri>"):
        ctl._play_source_steer("local-only:garbage")
    with pytest.raises(InvalidDataError, match="unavailable or is a streaming provider"):
        ctl._play_source_provider("spotify--abc://track/1", "spotify--abc")
    with pytest.raises(InvalidDataError, match="unavailable or is a streaming provider"):
        ctl._play_source_provider(uri, "filesystem_local--gone")

    class Mapping:
        def __init__(self, domain, item_id, instance=""):
            self.provider_domain = domain
            self.provider_instance = instance
            self.item_id = item_id

    marker = Mapping("local_only", "filesystem_local--abc")
    real = Mapping("filesystem_local", "Music/example.flac", "filesystem_local--abc")
    item = type("Item", (), {"uri": uri, "provider_mappings": {marker, real}})()
    assert ctl._play_source_steer(item) == (uri, "filesystem_local--abc")
    assert item.provider_mappings == {real}


def test_steer_is_idempotent_and_refuses_a_moved_module() -> None:
    once = steer.apply(QUEUE_LOADER.read_text(encoding="utf-8"), steer.EDITS[steer.QUEUE_LOADER])
    assert steer.apply(once, steer.EDITS[steer.QUEUE_LOADER]) == once
    with pytest.raises(SystemExit, match="anchor found 0 times"):
        steer.apply("class PlayerQueuesController:\n    pass\n", steer.EDITS[steer.QUEUE_LOADER])


@requires_py314
def test_steer_main_patches_both_files_once(tmp_path: Path) -> None:
    loader = tmp_path / "queue_loader.py"
    audio = tmp_path / "audio.py"
    loader.write_bytes(QUEUE_LOADER.read_bytes())
    audio.write_bytes(STREAMS_AUDIO.read_bytes())
    assert steer.main(["x", str(loader), str(audio)]) == 0
    first = (loader.read_bytes(), audio.read_bytes())
    assert all(steer.MARKER.encode() in text and b"\r\n" not in text for text in first)
    assert steer.main(["x", str(loader), str(audio)]) == 0
    assert (loader.read_bytes(), audio.read_bytes()) == first
    with pytest.raises(SystemExit, match="expected 2 paths"):
        steer.main(["x", str(loader)])


@requires_py314
def test_opus_bitrate_reaches_the_encoder_and_the_config() -> None:
    codecs = opus.apply(AIOSENDSPIN_CODECS.read_text(encoding="utf-8"), opus.EDITS[opus.CODECS])
    role = opus.apply(AIOSENDSPIN_PLAYER_V1.read_text(encoding="utf-8"), opus.EDITS[opus.PLAYER_ROLE])
    provider = opus.apply(SENDSPIN_PLAYER.read_text(encoding="utf-8"), opus.EDITS[opus.PROVIDER])
    for text in (codecs, role, provider):
        ast.parse(text)

    # the encoder reads the option once it opens its context
    assert "self._encoder.bit_rate = int(bit_rate)" in codecs
    assert codecs.index('self._encoder.format = "s16"') < codecs.index("self._encoder.bit_rate = int(bit_rate)")
    # the role hands the option to the pool and to the stream requirements
    role_tree = ast.parse(role)
    names = {node.name for node in ast.walk(role_tree) if isinstance(node, ast.FunctionDef)}
    assert {"set_opus_bit_rate", "_opus_options"} <= names
    assert "options=self._opus_options()," in role
    assert "transform_options=(" in role
    # the provider exposes and applies the setting on every config update
    assert "key=CONF_SENDSPIN_OPUS_BITRATE" in provider
    assert provider.index("self._apply_opus_bitrate()") < provider.index("await self._apply_preferred_format()")


def test_opus_bitrate_is_idempotent_and_refuses_a_moved_module() -> None:
    once = opus.apply(AIOSENDSPIN_CODECS.read_text(encoding="utf-8"), opus.EDITS[opus.CODECS])
    assert opus.apply(once, opus.EDITS[opus.CODECS]) == once
    with pytest.raises(SystemExit, match="anchor found 0 times"):
        opus.apply("class OpusEncoder:\n    pass\n", opus.EDITS[opus.CODECS])


@requires_py314
def test_opus_bitrate_main_patches_all_three_once(tmp_path: Path) -> None:
    targets = []
    for fixture in (AIOSENDSPIN_CODECS, AIOSENDSPIN_PLAYER_V1, SENDSPIN_PLAYER):
        target = tmp_path / fixture.name
        target.write_bytes(fixture.read_bytes())
        targets.append(target)
    assert opus.main(["x", *map(str, targets)]) == 0
    first = [target.read_bytes() for target in targets]
    assert all(opus.MARKER.encode() in text and b"\r\n" not in text for text in first)
    assert opus.main(["x", *map(str, targets)]) == 0
    assert [target.read_bytes() for target in targets] == first


def test_folder_browser_manifest_and_module_are_valid() -> None:
    import json

    manifest = json.loads(folders.MANIFEST_JSON)
    assert manifest["type"] == "plugin"
    assert manifest["domain"] == "folder_browser"
    assert manifest["codeowners"] == ["@trooperthorn"]
    # this backs the Filesystem provider's folder-picker setup flow; a user
    # disabling it would silently break that flow, so it must not be
    # disableable
    assert manifest["builtin"] is True
    assert manifest["allow_disable"] is False
    compile(folders.INIT_PY, "folder_browser/__init__.py", "exec")

    tree = ast.parse(folders.INIT_PY)
    provider = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "FolderBrowserProvider")
    methods = {node.name for node in provider.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    assert "browse_path" in methods
    assert "loaded_in_mass" in methods
    assert "unload" in methods

    # the command name and scope are a contract with the fork frontend's
    # folder picker; carried over unchanged from the retired browse_path.py
    # anchor patch
    assert '"config/providers/browse_path"' in folders.INIT_PY
    assert "required_scope=Scope.CONFIG_PROVIDERS_WRITE" in folders.INIT_PY
    assert "from music_assistant.helpers.security import is_safe_path" in folders.INIT_PY
    # the same roots, safety check and listing behavior as the anchor patch
    assert '{"path": "/music", "label": "Music drive"}' in folders.INIT_PY
    assert '{"path": "/media", "label": "Media"}' in folders.INIT_PY
    assert '{"path": "/share", "label": "Share"}' in folders.INIT_PY
    assert 'entry.name.startswith(".")' in folders.INIT_PY
    assert "folders[:500]" in folders.INIT_PY

    strings = json.loads(folders.STRINGS_JSON)
    assert strings["manifest"]["description"] == manifest["description"]


def test_folder_browser_main_writes_once_and_is_idempotent(tmp_path: Path) -> None:
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    assert folders.main(["folder_browser.py", str(providers_dir)]) == 0
    provider_dir = providers_dir / "folder_browser"
    manifest_once = (provider_dir / "manifest.json").read_bytes()
    strings_once = (provider_dir / "strings.json").read_bytes()
    init_once = (provider_dir / "__init__.py").read_bytes()
    assert b"\r\n" not in manifest_once
    assert b"\r\n" not in strings_once
    assert b"\r\n" not in init_once
    assert folders.main(["folder_browser.py", str(providers_dir)]) == 0
    assert (provider_dir / "manifest.json").read_bytes() == manifest_once
    assert (provider_dir / "strings.json").read_bytes() == strings_once
    assert (provider_dir / "__init__.py").read_bytes() == init_once


def test_main_writes_once_and_keeps_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "player.py"
    target.write_bytes(UPSTREAM.read_bytes())
    assert patch.main(["x", str(target)]) == 0
    first = target.read_bytes()
    assert b"\r\n" not in first
    assert patch.MARKER.encode() in first
    assert patch.main(["x", str(target)]) == 0
    assert target.read_bytes() == first


# library_trash: the methods are exercised for real by lifting the plugin's
# class body onto a stub with the two things the code touches (mass, logger)


def _library_trash_provider(base: Path):
    """A LibraryTrashProvider stand-in carrying only the trash methods from the plugin."""
    import asyncio
    import logging
    import os
    import shutil
    from collections.abc import Callable

    body = trash.INIT_PY.split("class LibraryTrashProvider(PluginProvider):\n", 1)[1]

    class InvalidDataError(Exception):
        pass

    class MediaNotFoundError(Exception):
        pass

    def is_safe_path(path: str, base_path: str | None = None) -> bool:
        norm_path = os.path.normpath(path)
        if norm_path.startswith("..") or "/../" in norm_path or "\\..\\" in norm_path:
            return False
        if base_path is None:
            return True
        norm_base = os.path.normpath(base_path)
        if not Path(norm_path).is_absolute():
            norm_path = os.path.normpath(os.path.join(norm_base, norm_path))
        try:
            return os.path.commonpath((norm_base, norm_path)) == norm_base
        except ValueError:
            return False

    class Provider:
        def __init__(self, base_path: str | None) -> None:
            if base_path is not None:
                self.base_path = base_path

    class Mass:
        def __init__(self) -> None:
            self.providers = {"fs": Provider(str(base)), "spotify": Provider(None)}

        def get_provider(self, instance: str):
            return self.providers.get(instance)

    namespace = {
        "asyncio": asyncio,
        "os": os,
        "shutil": shutil,
        "Any": object,
        "Callable": Callable,
        "TRASH_DIR": ".music-assistant-trash",
        "InvalidDataError": InvalidDataError,
        "MediaNotFoundError": MediaNotFoundError,
        "is_safe_path": is_safe_path,
    }
    exec("class Ctl:\n" + body, namespace)  # noqa: S102
    ctl = namespace["Ctl"]()
    ctl.mass = Mass()
    ctl.logger = logging.getLogger("test")
    ctl.errors = (InvalidDataError, MediaNotFoundError)
    return ctl


def test_library_trash_manifest_and_module_are_valid() -> None:
    import json

    manifest = json.loads(trash.MANIFEST_JSON)
    assert manifest["type"] == "plugin"
    assert manifest["domain"] == "library_trash"
    assert manifest["codeowners"] == ["@trooperthorn"]
    # this backs the Duplicates page's trash actions; a user disabling it
    # would silently break that flow, so it must not be disableable
    assert manifest["builtin"] is True
    assert manifest["allow_disable"] is False
    compile(trash.INIT_PY, "library_trash/__init__.py", "exec")

    tree = ast.parse(trash.INIT_PY)
    provider = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "LibraryTrashProvider")
    methods = {node.name for node in provider.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}
    assert {"trash_move", "trash_list", "trash_restore", "trash_empty", "loaded_in_mass", "unload"} <= methods

    # the command names and scope are a contract with the fork frontend's
    # Duplicates page; carried over unchanged from the retired music_trash.py
    # anchor patch
    for name in ("move", "list", "restore", "empty"):
        assert f'"music/trash/{name}"' in trash.INIT_PY
    assert trash.INIT_PY.count("required_scope=Scope.LIBRARY_MANAGE") == 4
    assert "from music_assistant.helpers.security import is_safe_path" in trash.INIT_PY

    strings = json.loads(trash.STRINGS_JSON)
    assert strings["manifest"]["description"] == manifest["description"]


def test_library_trash_main_writes_once_and_is_idempotent(tmp_path: Path) -> None:
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    assert trash.main(["library_trash.py", str(providers_dir)]) == 0
    provider_dir = providers_dir / "library_trash"
    manifest_once = (provider_dir / "manifest.json").read_bytes()
    strings_once = (provider_dir / "strings.json").read_bytes()
    init_once = (provider_dir / "__init__.py").read_bytes()
    assert b"\r\n" not in manifest_once
    assert b"\r\n" not in strings_once
    assert b"\r\n" not in init_once
    assert trash.main(["library_trash.py", str(providers_dir)]) == 0
    assert (provider_dir / "manifest.json").read_bytes() == manifest_once
    assert (provider_dir / "strings.json").read_bytes() == strings_once
    assert (provider_dir / "__init__.py").read_bytes() == init_once


def test_library_trash_moves_lists_restores_and_empties(tmp_path: Path) -> None:
    import asyncio

    base = tmp_path / "music"
    (base / "Artist" / "Album").mkdir(parents=True)
    song = base / "Artist" / "Album" / "01.mp3"
    song.write_bytes(b"mp3")
    sheet = base / "Artist" / "Album" / "Album.cue"
    sheet.write_bytes(b"cue")
    ctl = _library_trash_provider(base)
    run = asyncio.run

    # relative and absolute paths both land in the dot folder, path kept
    assert run(ctl.trash_move("fs", "Artist/Album/01.mp3")) == {
        "path": os.path.join("Artist", "Album", "01.mp3"),
        "trashed_path": os.path.join("Artist", "Album", "01.mp3"),
    }
    assert run(ctl.trash_move("fs", str(sheet)))["trashed_path"].endswith("Album.cue")
    trash = base / ".music-assistant-trash"
    assert not song.exists() and (trash / "Artist" / "Album" / "01.mp3").read_bytes() == b"mp3"

    # a second file with the same path is kept beside the first, never over it
    song.write_bytes(b"again")
    assert run(ctl.trash_move("fs", "Artist/Album/01.mp3"))["trashed_path"].endswith("01 (1).mp3")

    listed = run(ctl.trash_list("fs"))
    assert [item["path"] for item in listed] == sorted(
        [
            os.path.join("Artist", "Album", "01 (1).mp3"),
            os.path.join("Artist", "Album", "01.mp3"),
            os.path.join("Artist", "Album", "Album.cue"),
        ],
        key=str.casefold,
    )
    assert all(item["size"] > 0 and item["trashed_at"] > 0 for item in listed)

    # restore puts one back; a second restore to the same place is refused
    assert run(ctl.trash_restore("fs", "Artist/Album/01.mp3"))["path"] == os.path.join("Artist", "Album", "01.mp3")
    assert song.read_bytes() == b"mp3"
    sheet.write_bytes(b"new cue")
    with pytest.raises(ctl.errors, match="exists"):
        run(ctl.trash_restore("fs", "Artist/Album/Album.cue"))
    assert sheet.read_bytes() == b"new cue"

    # what is refused: other providers, paths outside the folder, the folder
    # itself, a file already in the trash, a missing file
    for provider, path in (("spotify", "x"), ("nope", "x")):
        with pytest.raises(ctl.errors, match="not a Filesystem provider"):
            run(ctl.trash_move(provider, path))
    for bad in ("../outside.mp3", str(tmp_path / "outside.mp3"), ".", ""):
        with pytest.raises(ctl.errors, match="outside"):
            run(ctl.trash_move("fs", bad))
    with pytest.raises(ctl.errors, match="already in the trash"):
        run(ctl.trash_move("fs", ".music-assistant-trash/Artist/Album/Album.cue"))
    with pytest.raises(ctl.errors, match="not a file"):
        run(ctl.trash_move("fs", "Artist/Album"))
    with pytest.raises(ctl.errors, match="outside"):
        run(ctl.trash_restore("fs", "../Artist/Album/01.mp3"))

    # empty deletes the folder and reports the count; an empty trash lists nothing
    assert run(ctl.trash_empty("fs")) == {"deleted": 2}
    assert not trash.exists()
    assert run(ctl.trash_list("fs")) == []
    assert run(ctl.trash_empty("fs")) == {"deleted": 0}
    assert song.read_bytes() == b"mp3"


def test_playlist_bridge_manifest_and_module_are_valid() -> None:
    import json

    manifest = json.loads(bridge.MANIFEST_JSON)
    assert manifest["type"] == "plugin"
    assert manifest["domain"] == "playlist_bridge"
    assert manifest["codeowners"] == ["@trooperthorn"]
    compile(bridge.INIT_PY, "playlist_bridge/__init__.py", "exec")
    assert "migrate_playlist" in bridge.INIT_PY
    assert "archive_playlists" in bridge.INIT_PY
    assert '"playlist_bridge/archive_playlists"' in bridge.INIT_PY
    assert bridge.INIT_PY.count("required_scope=Scope.LIBRARY_WRITE") == 2
    # a long bulk archive job must never jump ahead of interactive tasks
    assert "priority=True" not in bridge.INIT_PY.split("async def archive_playlists")[1].split("async def _archive_playlists")[0]
    # the throwaway builtin copy created during a migration must always be
    # cleaned up, success or failure
    assert "await builtin.library_remove(matched_playlist.item_id, MediaType.PLAYLIST)" in bridge.INIT_PY
    # the plugin must resolve destinations from the caller's own configured
    # providers, not a global domain lookup, or it reintroduces the
    # scope-escape bug the closed upstream PR shipped
    assert "self.mass.music.providers" in bridge.INIT_PY
    assert "self.mass.get_provider(destination" not in bridge.INIT_PY
    # translation_owner must come from the base Provider property
    # (f"provider.{domain}"), not the bare domain, or the background task's
    # translation key never resolves
    assert "translation_owner=self.translation_owner" in bridge.INIT_PY

    # guards ported from upstream #5989 -- see the module docstring and the
    # inline "mirrors upstream #5989" comments for why each one exists
    assert "item.available" in bridge.INIT_PY  # exclude unavailable providers before matching
    assert "is_dynamic" in bridge.INIT_PY  # reject dynamic source playlists
    assert "is_streaming_provider" in bridge.INIT_PY  # reject non-streaming, non-builtin destinations
    assert "PLAYLIST_TRACKS_EDIT" in bridge.INIT_PY  # destination must support editing playlists
    assert "supported_media_types" in bridge.INIT_PY  # destination must support track playlists
    assert "is_safe_name" in bridge.INIT_PY  # validate the destination playlist name
    assert "from music_assistant.helpers.security import is_safe_name" in bridge.INIT_PY
    # the source playlist's own provider must also be available/allowed, not
    # just the destination
    assert "playlist.provider_mappings" in bridge.INIT_PY

    strings = json.loads(bridge.STRINGS_JSON)
    assert strings["manifest"]["description"] == manifest["description"]
    assert strings["background_task"]["playlist_bridge_migrate"] == "Migrate playlist {0} to {1}"
    assert strings["background_task"]["playlist_bridge_archive"] == "Archive playlists ({0})"


def test_playlist_bridge_match_policy_docstring_states_it_has_no_effect() -> None:
    """match_policy must never be presented as honoured; see docs/playlist-bridge-vs-upstream.md."""
    assert "HAS NO EFFECT IN THIS BRIDGE" in bridge.INIT_PY
    assert "PlaylistMatchPolicy" in bridge.INIT_PY
    assert "server PR #5989" in bridge.INIT_PY
    assert "2.11.0" in bridge.INIT_PY


# playlist_bridge behavior: the real server package is not installed in this
# test environment, so exec the plugin's class body onto stand-ins for the
# handful of server names it actually touches at runtime, same technique as
# _library_trash_provider above. Annotations are postponed (the plugin
# itself carries `from __future__ import annotations`) so server-only types
# used only as type hints (Playlist, MusicProvider, BackgroundTask) never
# need real stand-ins; only names used in executable statements do.


class _StubMediaType:
    TRACK = "track"
    PLAYLIST = "playlist"


class _StubMusicAssistantError(Exception):
    pass


class _StubProviderUnavailableError(_StubMusicAssistantError):
    pass


class _StubMusicProvider:
    """Marker base so the plugin's isinstance(provider, MusicProvider) checks pass."""


def _playlist_bridge_provider():
    """A PlaylistBridgeProvider stand-in carrying only the plugin's own methods."""
    import logging

    body = bridge.INIT_PY.split("class PlaylistBridgeProvider(PluginProvider):\n", 1)[1]

    calls: dict[str, list] = {"progress": [], "failures": [], "report": []}

    def update_current_task_progress_from_index(current, total, text=None):
        calls["progress"].append((current, total, text))

    def report_current_task_failure(message):
        calls["failures"].append(message)

    def set_current_task_report(markdown):
        calls["report"].append(markdown)

    namespace = {
        "MediaType": _StubMediaType,
        "MusicAssistantError": _StubMusicAssistantError,
        "ProviderUnavailableError": _StubProviderUnavailableError,
        "InvalidDataError": _StubMusicAssistantError,
        "MusicProvider": _StubMusicProvider,
        "is_safe_name": lambda name: ".." not in name and "/" not in name,
        "update_current_task_progress_from_index": update_current_task_progress_from_index,
        "report_current_task_failure": report_current_task_failure,
        "set_current_task_report": set_current_task_report,
    }
    exec("from __future__ import annotations\n\nclass Ctl:\n" + body, namespace)  # noqa: S102
    ctl = namespace["Ctl"]()
    ctl.logger = logging.getLogger("test")
    ctl.translation_owner = "provider.playlist_bridge"
    return ctl, calls


class _Mapping:
    def __init__(self, provider_domain: str, provider_instance: str, item_id: str = "x") -> None:
        self.provider_domain = provider_domain
        self.provider_instance = provider_instance
        self.item_id = item_id


class _Playlist:
    def __init__(self, item_id, name, mappings, is_dynamic=False) -> None:
        self.item_id = item_id
        self.name = name
        self.provider_mappings = mappings
        self.is_dynamic = is_dynamic


class _Track:
    def __init__(self, provider_mappings) -> None:
        self.provider_mappings = provider_mappings


class _BuiltinProvider(_StubMusicProvider):
    """Stands in for self.mass.get_provider("builtin") in _migrate_playlist."""

    def __init__(self) -> None:
        self.removed: list[tuple] = []

    async def import_playlist(self, m3u_data):
        return _Playlist("matched-1", "Matched", [])

    async def match_imported_playlist_tracks(self, item_id, instance_ids):
        return None

    async def library_remove(self, item_id, media_type):
        self.removed.append((item_id, media_type))
        return True


class _Destination:
    def __init__(self, instance_id="spotify1", name="Spotify", fail=False) -> None:
        self.instance_id = instance_id
        self.name = name
        self.fail = fail

    async def add_playlist_tracks(self, item_id, track_ids):
        if self.fail:
            raise _StubMusicAssistantError("destination rejected tracks")


class _MigratePlaylistsController:
    """The self.mass.music.playlists surface _migrate_playlist calls."""

    def __init__(self, tracks_for_matched) -> None:
        self._tracks_for_matched = tracks_for_matched

    async def export_playlist(self, item_id):
        return f"m3u:{item_id}"

    async def create_playlist(self, name, media_types, instance_id):
        return _Playlist("new-1", name, [_Mapping("spotify", instance_id, "new-item-1")])

    async def tracks(self, item_id, provider):
        for track in self._tracks_for_matched:
            yield track


def _migrate_mass(builtin, playlists_controller):
    music = type("Music", (), {"playlists": playlists_controller})()
    return type("Mass", (), {"music": music, "get_provider": lambda self, domain: builtin if domain == "builtin" else None})()


def test_migrate_playlist_removes_throwaway_builtin_playlist_on_success() -> None:
    import asyncio

    builtin = _BuiltinProvider()
    matched_track = _Track([_Mapping("spotify", "spotify1", "dest-track-1")])
    ctl, _calls = _playlist_bridge_provider()
    ctl.mass = _migrate_mass(builtin, _MigratePlaylistsController([matched_track]))

    playlist = _Playlist("10", "My Playlist", [])
    destination = _Destination()

    asyncio.run(ctl._migrate_playlist(playlist, destination, "My Playlist"))

    # the throwaway builtin copy used only to run the matching pipeline must
    # not leak an orphaned .m3u file behind a successful migration
    assert builtin.removed == [("matched-1", _StubMediaType.PLAYLIST)]


def test_migrate_playlist_removes_throwaway_builtin_playlist_when_migration_raises() -> None:
    import asyncio

    builtin = _BuiltinProvider()
    matched_track = _Track([_Mapping("spotify", "spotify1", "dest-track-1")])
    ctl, _calls = _playlist_bridge_provider()
    ctl.mass = _migrate_mass(builtin, _MigratePlaylistsController([matched_track]))

    playlist = _Playlist("10", "My Playlist", [])
    destination = _Destination(fail=True)

    with pytest.raises(_StubMusicAssistantError, match="rejected the migrated tracks"):
        asyncio.run(ctl._migrate_playlist(playlist, destination, "My Playlist"))

    # cleanup must still run (via finally) even though the migration itself raised
    assert builtin.removed == [("matched-1", _StubMediaType.PLAYLIST)]


class _ArchivePlaylistsController:
    """The self.mass.music.playlists surface _archive_playlists calls."""

    def __init__(self, playlists, fail_item_ids: set[str] | None = None) -> None:
        self._playlists = playlists
        self._fail_item_ids = fail_item_ids or set()
        self.exported: list[str] = []
        self.imported: list[tuple[str, bool]] = []

    async def iter_library_items(self):
        for playlist in self._playlists:
            yield playlist

    async def export_playlist(self, item_id):
        self.exported.append(item_id)
        if item_id in self._fail_item_ids:
            raise _StubMusicAssistantError(f"item {item_id} failed")
        return f"m3u:{item_id}"

    async def import_playlist(self, m3u_data, library_matching=False):
        self.imported.append((m3u_data, library_matching))
        return _Playlist(f"new-{m3u_data}", m3u_data, [])


def _archive_provider(playlists, fail_item_ids=None):
    ctl, calls = _playlist_bridge_provider()
    controller = _ArchivePlaylistsController(playlists, fail_item_ids)
    ctl.mass = type("Mass", (), {"music": type("Music", (), {"playlists": controller})()})()
    return ctl, controller, calls


def test_archive_playlists_skips_dynamic_builtin_only_mismatched_and_already_archived() -> None:
    import asyncio

    playlists = [
        _Playlist("1", "Dynamic Mix", [_Mapping("spotify", "spotify1")], is_dynamic=True),
        _Playlist("2", "Local Copy", [_Mapping("builtin", "builtin")]),
        _Playlist("3", "Apple Playlist", [_Mapping("apple_music", "apple1")]),
        _Playlist("4", "Already Archived", [_Mapping("spotify", "spotify1")]),
        _Playlist("5", "Already Archived", [_Mapping("builtin", "builtin")]),
        _Playlist("6", "New Playlist", [_Mapping("spotify", "spotify1")]),
    ]
    ctl, controller, calls = _archive_provider(playlists)

    asyncio.run(ctl._archive_playlists("spotify"))

    # only playlist 6 is spotify-sourced, not dynamic, not builtin-only, and
    # its name is not already a builtin playlist (playlist 4 shares its name
    # with builtin playlist 5, so it is skipped as already archived; playlist
    # 5 itself is also builtin-only, so the builtin-only count is 2: playlists
    # 2 and 5)
    assert controller.exported == ["6"]
    assert len(controller.imported) == 1
    assert controller.imported[0][1] is False  # library_matching=False
    report = calls["report"][-1]
    assert "Archived: 1" in report
    assert "Skipped (dynamic): 1" in report
    assert "Skipped (builtin-only): 2" in report
    assert "Skipped (provider mismatch): 1" in report
    assert "Skipped (already archived): 1" in report
    assert "Failed: 0" in report


def test_archive_playlists_reports_a_failure_and_continues() -> None:
    import asyncio

    playlists = [
        _Playlist("1", "Bad Playlist", [_Mapping("spotify", "spotify1")]),
        _Playlist("2", "Good Playlist", [_Mapping("spotify", "spotify1")]),
    ]
    ctl, controller, calls = _archive_provider(playlists, fail_item_ids={"1"})

    # a single bad playlist must not abort the run: the second is still archived
    asyncio.run(ctl._archive_playlists(None))

    assert controller.exported == ["1", "2"]
    assert len(controller.imported) == 1
    assert calls["failures"] == ["Bad Playlist: item 1 failed"]
    report = calls["report"][-1]
    assert "Archived: 1" in report
    assert "Failed: 1" in report
    assert "Bad Playlist" in report


def test_archive_playlists_task_id_is_deterministic_for_source_provider() -> None:
    import asyncio

    ctl, _calls = _playlist_bridge_provider()
    captured = []

    class _Tasks:
        def run_background_task(self, **kwargs):
            captured.append(kwargs)
            return kwargs

    ctl.mass = type("Mass", (), {"tasks": _Tasks()})()

    asyncio.run(ctl.archive_playlists("spotify"))
    asyncio.run(ctl.archive_playlists("spotify"))
    asyncio.run(ctl.archive_playlists(None))

    # same source_provider -> same task id, so a double-click is deduped by
    # the tasks controller instead of starting a second overlapping run
    assert captured[0]["task_id"] == captured[1]["task_id"] == "playlist_bridge_archive_spotify"
    assert captured[2]["task_id"] == "playlist_bridge_archive_all"
    # a long bulk job must never jump ahead of interactive tasks
    assert captured[0].get("priority", False) is False


def test_playlist_bridge_main_writes_once_and_is_idempotent(tmp_path: Path) -> None:
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    assert bridge.main(["playlist_bridge.py", str(providers_dir)]) == 0
    provider_dir = providers_dir / "playlist_bridge"
    manifest_once = (provider_dir / "manifest.json").read_bytes()
    strings_once = (provider_dir / "strings.json").read_bytes()
    init_once = (provider_dir / "__init__.py").read_bytes()
    assert b"\r\n" not in manifest_once
    assert b"\r\n" not in strings_once
    assert b"\r\n" not in init_once
    assert bridge.main(["playlist_bridge.py", str(providers_dir)]) == 0
    assert (provider_dir / "manifest.json").read_bytes() == manifest_once
    assert (provider_dir / "strings.json").read_bytes() == strings_once
    assert (provider_dir / "__init__.py").read_bytes() == init_once
