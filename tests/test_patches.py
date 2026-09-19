"""The build-time edits of the server (no network)."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "music_assistant_lm" / "patches"))

import browse_path as browse  # noqa: E402
import hass_source_select as patch  # noqa: E402
import play_source_steer as steer  # noqa: E402
import sendspin_opus_bitrate as opus  # noqa: E402

# the modules exactly as the pinned server release ships them
UPSTREAM = ROOT / "tests" / "fixtures" / "hass_player_2_10_4.py"
QUEUE_LOADER = ROOT / "tests" / "fixtures" / "queue_loader_2_10_4.py"
STREAMS_AUDIO = ROOT / "tests" / "fixtures" / "streams_audio_2_10_4.py"
# aiosendspin as server 2.10.4 pins it (aiosendspin[server]==9.1.1)
AIOSENDSPIN_CODECS = ROOT / "tests" / "fixtures" / "aiosendspin_codecs_9_1_1.py"
AIOSENDSPIN_PLAYER_V1 = ROOT / "tests" / "fixtures" / "aiosendspin_player_v1_9_1_1.py"
SENDSPIN_PLAYER = ROOT / "tests" / "fixtures" / "sendspin_player_2_10_4.py"
CONFIG_PROVIDERS = ROOT / "tests" / "fixtures" / "config_providers_2_10_4.py"


def test_the_fixture_matches_the_pinned_server_release() -> None:
    dockerfile = (ROOT / "music_assistant_lm" / "Dockerfile").read_text(encoding="utf-8")
    assert 'ARG SERVER_VERSION="2.10.4"' in dockerfile, (
        "the server moved; refresh the tests/fixtures/*_<version>.py copies from the new "
        "release and re-check the anchors in music_assistant_lm/patches/*.py"
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


def test_steer_marks_queue_items_and_prefers_their_provider() -> None:
    loader = steer.apply(QUEUE_LOADER.read_text(encoding="utf-8"), steer.EDITS[steer.QUEUE_LOADER])
    audio = steer.apply(STREAMS_AUDIO.read_text(encoding="utf-8"), steer.EDITS[steer.STREAMS_AUDIO])

    for text in (loader, audio):
        ast.parse(text)
    loader_tree = ast.parse(loader)
    names = {node.name for node in ast.walk(loader_tree) if isinstance(node, ast.FunctionDef)}
    assert "_play_source_steer" in names
    # the request's uri is read before it resolves to the library item
    assert loader.index("steer_uri = item if isinstance(item, str) else None") < loader.index(
        "media_item = await self.mass.music.get_item_by_uri(item)"
    )
    assert 'queue_item.extra_attributes["preferred_provider"] = steer_provider' in loader
    # the streams side puts it ahead of the quality order, before the user's filter
    assert "preferred_providers = [steer, *preferred_providers]" in audio
    assert audio.index("preferred_providers = [steer, *preferred_providers]") < audio.index(
        "candidates = self._get_streamdetail_candidates("
    )


def test_steer_is_idempotent_and_refuses_a_moved_module() -> None:
    once = steer.apply(QUEUE_LOADER.read_text(encoding="utf-8"), steer.EDITS[steer.QUEUE_LOADER])
    assert steer.apply(once, steer.EDITS[steer.QUEUE_LOADER]) == once
    with pytest.raises(SystemExit, match="anchor found 0 times"):
        steer.apply("class PlayerQueuesController:\n    pass\n", steer.EDITS[steer.QUEUE_LOADER])


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


def test_browse_path_adds_a_scoped_command_inside_the_roots() -> None:
    patched = browse.apply(CONFIG_PROVIDERS.read_text(encoding="utf-8"))

    tree = ast.parse(patched)
    methods = {
        node.name
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert "get_provider_config_entries" in methods
    assert "browse_path" in methods
    assert 'api_command("config/providers/browse_path", required_scope=Scope.CONFIG_PROVIDERS_WRITE)' in patched
    assert "from music_assistant.helpers.security import is_safe_path" in patched
    assert 'entry.name.startswith(".")' in patched
    assert "folders[:500]" in patched
    assert browse.apply(patched) == patched
    with pytest.raises(SystemExit, match="anchor found 0 times"):
        browse.apply("class ProvidersController:\n    pass\n")


def test_browse_path_main_patches_the_module_once(tmp_path: Path) -> None:
    target = tmp_path / "providers.py"
    target.write_text(CONFIG_PROVIDERS.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    assert browse.main(["browse_path.py", str(target)]) == 0
    once = target.read_bytes()
    assert b"\r\n" not in once
    assert browse.main(["browse_path.py", str(target)]) == 0
    assert target.read_bytes() == once


def test_main_writes_once_and_keeps_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "player.py"
    target.write_bytes(UPSTREAM.read_bytes())
    assert patch.main(["x", str(target)]) == 0
    first = target.read_bytes()
    assert b"\r\n" not in first
    assert patch.MARKER.encode() in first
    assert patch.main(["x", str(target)]) == 0
    assert target.read_bytes() == first


# music_trash: the methods are exercised for real by lifting the patched
# class body onto a stub with the two things the code touches
MUSIC_CONTROLLER = ROOT / "tests" / "fixtures" / "music_controller_2_10_4.py"


def _trash_controller(base: Path):
    """A MusicController stand-in carrying only the trash methods from the patch."""
    import asyncio
    import logging
    import os
    import shutil

    from music_trash import EDITS

    body = EDITS[2][1].split('    @api_command("music/match_providers"')[0]
    body = body.replace("    @api_command(", "    @_command(")

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

    class Scope:
        LIBRARY_MANAGE = "library.manage"

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
        "InvalidDataError": InvalidDataError,
        "MediaNotFoundError": MediaNotFoundError,
        "is_safe_path": is_safe_path,
        "Scope": Scope,
        "_command": lambda *_a, **_k: (lambda fn: fn),
    }
    exec("class Ctl:\n" + body, namespace)  # noqa: S102
    ctl = namespace["Ctl"]()
    ctl.mass = Mass()
    ctl.logger = logging.getLogger("test")
    ctl.errors = (InvalidDataError, MediaNotFoundError)
    return ctl


def test_music_trash_adds_four_scoped_commands() -> None:
    import music_trash as trash

    patched = trash.apply(MUSIC_CONTROLLER.read_text(encoding="utf-8"))
    tree = ast.parse(patched)
    methods = {
        node.name
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert {"trash_move", "trash_list", "trash_restore", "trash_empty", "remove_provider_mapping"} <= methods
    for name in ("move", "list", "restore", "empty"):
        assert f'api_command("music/trash/{name}", required_scope=Scope.LIBRARY_MANAGE)' in patched
    assert "from music_assistant.helpers.security import is_safe_path" in patched
    assert trash.apply(patched) == patched
    with pytest.raises(SystemExit, match="anchor found 0 times"):
        trash.apply("class MusicController:\n    pass\n")


def test_music_trash_main_patches_the_module_once(tmp_path: Path) -> None:
    import music_trash as trash

    target = tmp_path / "controller.py"
    target.write_text(MUSIC_CONTROLLER.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    assert trash.main(["music_trash.py", str(target)]) == 0
    once = target.read_bytes()
    assert b"\r\n" not in once
    assert trash.main(["music_trash.py", str(target)]) == 0
    assert target.read_bytes() == once


def test_music_trash_moves_lists_restores_and_empties(tmp_path: Path) -> None:
    import asyncio

    base = tmp_path / "music"
    (base / "Artist" / "Album").mkdir(parents=True)
    song = base / "Artist" / "Album" / "01.mp3"
    song.write_bytes(b"mp3")
    sheet = base / "Artist" / "Album" / "Album.cue"
    sheet.write_bytes(b"cue")
    ctl = _trash_controller(base)
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
