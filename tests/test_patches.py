"""The build-time edit of the Home Assistant player provider (no network)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "music_assistant_lm" / "patches"))

import hass_source_select as patch  # noqa: E402

# the provider module exactly as the pinned server release ships it
UPSTREAM = ROOT / "tests" / "fixtures" / "hass_player_2_10_3.py"


def test_the_fixture_matches_the_pinned_server_release() -> None:
    dockerfile = (ROOT / "music_assistant_lm" / "Dockerfile").read_text(encoding="utf-8")
    assert 'ARG SERVER_VERSION="2.10.3"' in dockerfile, (
        "the server moved; refresh tests/fixtures/hass_player_<version>.py from the new "
        "release and re-check the anchors in music_assistant_lm/patches/hass_source_select.py"
    )


def test_apply_adds_source_mirroring_and_select_source() -> None:
    patched = patch.apply(UPSTREAM.read_text(encoding="utf-8"))

    tree = ast.parse(patched)
    player = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HomeAssistantPlayer"
    )
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


def test_main_writes_once_and_keeps_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "player.py"
    target.write_bytes(UPSTREAM.read_bytes())
    assert patch.main(["x", str(target)]) == 0
    first = target.read_bytes()
    assert b"\r\n" not in first
    assert patch.MARKER.encode() in first
    assert patch.main(["x", str(target)]) == 0
    assert target.read_bytes() == first
