"""Pure-function tests for scripts/sync_upstream.py (no network)."""

from __future__ import annotations

import re
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


def test_prepend_changelog_merges_into_todays_heading() -> None:
    today = sync.date.today().strftime("%Y-%m-%d")
    existing = f"# Changelog\n\n## {today}\n\n- first\n\n## 2020-01-01\n\n- old\n"
    merged = sync.prepend_changelog(existing, ["second"])
    assert merged.count(f"## {today}") == 1
    assert merged.index("- second") < merged.index("- first")

    older = "# Changelog\n\n## 2020-01-01\n\n- old\n"
    fresh = sync.prepend_changelog(older, ["new"])
    assert fresh.startswith(f"# Changelog\n\n## {today}\n\n- new\n\n## 2020-01-01")


def test_merge_config_appends_this_apps_backup_exclusions() -> None:
    ours = 'name: Ours\nversion: "2026.09.13.1"\nbackup_exclude:\n  - cache.db\n  - webrtc_private_key.pem\n'
    upstream = "name: Music Assistant\nbackup_exclude:\n  - cache.db\n  - collage_images/*\n"
    merged = sync.merge_config(ours, upstream)
    excluded = __import__("yaml").safe_load(merged)["backup_exclude"]
    # upstream's list wins on content and order, this app's entry is appended
    assert excluded == ["cache.db", "collage_images/*", "webrtc_private_key.pem"]
    # and a second pass over the result does not duplicate it
    assert sync.merge_config(merged, upstream) == merged


def test_merge_config_adds_the_key_when_upstream_drops_it() -> None:
    ours = 'name: Ours\nversion: "2026.09.13.1"\n'
    upstream = "name: Music Assistant\nhost_network: true\n"
    excluded = __import__("yaml").safe_load(sync.merge_config(ours, upstream))["backup_exclude"]
    assert excluded == ["webrtc_private_key.pem"]


def test_merge_config_leaves_a_non_list_upstream_value_alone() -> None:
    ours = 'name: Ours\nversion: "2026.09.13.1"\n'
    upstream = "name: Music Assistant\nbackup_exclude: false\n"
    assert __import__("yaml").safe_load(sync.merge_config(ours, upstream))["backup_exclude"] is False


@pytest.mark.parametrize("bad", ["", "sha256:short", "nope", "SHA256:" + "a" * 64])
def test_digest_pattern_rejects_malformed_values(bad: str) -> None:
    assert not sync.DIGEST.match(bad)


def test_digest_pattern_accepts_a_real_digest() -> None:
    assert sync.DIGEST.match("sha256:" + "0123456789abcdef" * 4)


def test_dockerfile_pins_every_base_image_by_digest() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "music_assistant_lm" / "Dockerfile").read_text(encoding="utf-8")
    args = sync.read_args(dockerfile)
    for name in ("SERVER_DIGEST", "UV_DIGEST"):
        assert sync.DIGEST.match(args[name]), f"{name} is not a sha256 digest"
    # every image reference carries its digest argument, not just the tag
    assert "FROM ghcr.io/music-assistant/server:${SERVER_VERSION}@${SERVER_DIGEST}" in dockerfile
    # BuildKit refuses variable expansion in COPY --from, so uv is named as a
    # stage and the pin lives on that stage's FROM
    assert "FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_DIGEST} AS uv" in dockerfile
    assert "COPY --from=uv " in dockerfile
    # and no image is reached by a bare tag anywhere in the file
    for line in dockerfile.splitlines():
        if line.startswith("FROM ") and "@${" not in line:
            raise AssertionError(f"unpinned image reference: {line}")


def test_server_frontend_pin_extracts_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    pyproject = 'dependencies = [\n  "aiohttp==3.14.3",\n  "music-assistant-frontend==2.17.297",\n]\n'
    monkeypatch.setattr(sync, "fetch", lambda url, **kw: pyproject)
    assert sync.server_frontend_pin("2.10.3") == "2.17.297"


def test_server_frontend_pin_is_empty_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "fetch", lambda url, **kw: 'dependencies = ["aiohttp==3.14.3"]\n')
    assert sync.server_frontend_pin("2.10.3") == ""


def test_server_frontend_pin_ignores_a_non_version_specifier(monkeypatch: pytest.MonkeyPatch) -> None:
    # a git or url form carries no version to record, and must not half-match
    pyproject = 'dependencies = ["music-assistant-frontend @ git+https://example.invalid/x"]\n'
    monkeypatch.setattr(sync, "fetch", lambda url, **kw: pyproject)
    assert sync.server_frontend_pin("2.10.3") == ""


def test_dockerfile_records_what_the_server_expects() -> None:
    dockerfile = (Path(__file__).resolve().parents[1] / "music_assistant_lm" / "Dockerfile").read_text(encoding="utf-8")
    recorded = sync.read_args(dockerfile)["SERVER_EXPECTS_FRONTEND"]
    assert re.match(r"^[0-9]+\.[0-9]+\.[0-9]+$", recorded), recorded
