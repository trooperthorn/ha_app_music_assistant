"""Pure-function tests for scripts/sync_upstream.py (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import sync_upstream as sync  # noqa: E402

DOCKERFILE = 'ARG SERVER_VERSION="2.10.3"\nFROM x\nARG FRONTEND_RELEASE=""\nARG FRONTEND_WHEEL=""\nARG FRONTEND_SHA256=""\n'


def test_set_and_read_args_round_trip() -> None:
    updated = sync.set_args(DOCKERFILE, {"SERVER_VERSION": "2.11.0", "FRONTEND_WHEEL": "a.whl"})
    assert sync.read_args(updated) == {
        "SERVER_VERSION": "2.11.0",
        "FRONTEND_RELEASE": "",
        "FRONTEND_WHEEL": "a.whl",
        "FRONTEND_SHA256": "",
    }


def test_set_args_refuses_a_missing_argument() -> None:
    with pytest.raises(RuntimeError):
        sync.set_args(DOCKERFILE, {"NOPE": "1"})


def test_merge_config_keeps_own_keys_and_follows_upstream() -> None:
    ours = (
        "# header\n"
        "name: Ours\n"
        'version: "2026.09.12.1"\n'
        "slug: music_assistant_lm\n"
        "description: d\n"
        "url: u\n"
        "arch:\n  - amd64\n"
        "host_network: true\n"
    )
    upstream = (
        "name: Music Assistant\n"
        "version: 2.10.3\n"
        "slug: music_assistant\n"
        "image: ghcr.io/x\n"
        "arch:\n  - amd64\n  - aarch64\n"
        "host_network: true\n"
        "ingress_port: 8094\n"
    )
    merged = sync.merge_config(ours, upstream)
    assert merged.startswith("# header\n")
    assert 'version: "2026.09.12.1"' in merged
    assert "slug: music_assistant_lm" in merged
    assert "image:" not in merged
    assert "ingress_port: 8094" in merged
    assert "- aarch64" in merged
    assert sync.merge_config(merged, upstream) == merged


def test_changelog_entry_lists_every_change() -> None:
    entry = sync.changelog_entry(["a", "b"])
    assert entry.count("\n- ") == 2
