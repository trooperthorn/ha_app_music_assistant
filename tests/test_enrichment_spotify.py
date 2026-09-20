"""Raw archive capture contract against a fake pinned Spotify provider, no network."""

import asyncio
import importlib.util
from copy import deepcopy
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "enrichment_spotify", Path(__file__).resolve().parents[1] / "music_assistant_lm/providers/library_enrichment/spotify.py"
)
spotify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(spotify)
PLAYLIST = "abcdefghijklmnopqrstuv"


class MediaNotFoundError(Exception):
    pass


class Provider:
    instance_id = "spotify-instance"
    dev_session_active = True

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [{"item": {"id": "one", "type": "track"}}]
        self.global_required = False
        self.calls = []
        self.metadata_reads = 0
        self.account = "authenticated-listener"
        self.token = "first-token"  # noqa: S105 - fake provider credential
        self.mutate = None

    async def _playlist_requires_global_token(self, playlist_id):
        assert playlist_id == PLAYLIST
        return self.global_required

    async def _set_playlist_requires_global_token(self, playlist_id):
        self.global_required = True

    async def _get_auth_info(self, *, use_global_session):
        return {"access_token": self.token, "global": use_global_session}

    async def _get_data(self, endpoint, **kwargs):
        assert kwargs["auth_info"]["global"] == kwargs["use_global_session"]
        self.calls.append((endpoint, kwargs))
        if self.mutate:
            self.mutate(self, endpoint, kwargs)
        if endpoint == "me":
            return {"id": self.account}
        if endpoint.endswith("/items"):
            offset, limit = kwargs["offset"], kwargs["limit"]
            return {"items": deepcopy(self.rows[offset:offset + limit]), "offset": offset, "total": len(self.rows)}
        self.metadata_reads += 1
        return {"name": "Source", "snapshot_id": "snapshot-A", "tracks": {"total": len(self.rows)}, "owner": {"id": "different-owner"}}


def test_preview_is_fresh_metadata_only_and_uses_authenticated_account():
    provider = Provider()
    result = asyncio.run(spotify.preview_playlist(provider, PLAYLIST))
    assert result["account_id"] == "authenticated-listener"
    assert result["snapshot_id"] == "snapshot-A"
    assert result["total"] == 1
    assert [call[0] for call in provider.calls] == ["me", f"playlists/{PLAYLIST}", "me"]


def test_capture_preserves_all_ordered_raw_occurrences_and_progress():
    repeated = {"track": {"id": "same", "type": "track"}}
    rows = [repeated, None, {"item": None}, {"item": {"type": "episode", "id": "ep"}},
            {"is_local": True, "track": {"id": None}}, {"track": {"id": "unavailable", "is_playable": False}}, repeated] * 9
    provider = Provider(rows)
    progress = []

    async def on_page(count):
        progress.append(count)

    result = asyncio.run(spotify.capture_playlist(provider, PLAYLIST, on_page=on_page))
    assert [row["source_payload"] for row in result["occurrences"]] == rows
    assert [row["position"] for row in result["occurrences"]] == list(range(len(rows)))
    assert [row["state"] for row in result["occurrences"][:7]] == ["track", "null", "null", "episode", "local", "unavailable", "track"]
    assert result["snapshot_before"] == result["snapshot_after"] == "snapshot-A"
    assert progress == [50, 63]
    assert len([call for call in provider.calls if call[0] == "me"]) == 2


@pytest.mark.parametrize("fault", ["gap", "total", "snapshot"])
def test_rejects_incomplete_or_mixed_version(fault):
    provider = Provider([None] * 51)
    original = provider._get_data

    async def broken(endpoint, **kwargs):
        result = await original(endpoint, **kwargs)
        if endpoint.endswith("/items"):
            if fault == "gap":
                result["offset"] += 1
            if fault == "total":
                result["total"] += 1
        elif fault == "snapshot" and provider.metadata_reads == 2:
            result["snapshot_id"] = "snapshot-B"
        return result

    provider._get_data = broken
    with pytest.raises(spotify.SpotifyCaptureError):
        asyncio.run(spotify.capture_playlist(provider, PLAYLIST))


@pytest.mark.parametrize("fault", ["account", "session", "token-account"])
def test_rejects_account_or_session_drift(fault):
    provider = Provider([None] * 51)

    def mutate(instance, endpoint, kwargs):
        if endpoint.endswith("/items") and kwargs["offset"] == 0:
            if fault == "session":
                instance.dev_session_active = False
            else:
                instance.account = "another-account"
                if fault == "token-account":
                    instance.token = "second-token"  # noqa: S105 - fake provider credential

    provider.mutate = mutate
    with pytest.raises(spotify.SpotifyCaptureError):
        asyncio.run(spotify.capture_playlist(provider, PLAYLIST))


def test_item_access_fallback_restarts_entire_capture_in_global_session():
    provider = Provider()

    def mutate(instance, endpoint, kwargs):
        if endpoint.endswith("/items") and not kwargs["use_global_session"]:
            raise MediaNotFoundError()

    provider.mutate = mutate
    result = asyncio.run(spotify.capture_playlist(provider, PLAYLIST))
    assert [row["source_payload"] for row in result["occurrences"]] == provider.rows
    assert provider.global_required
    assert provider.metadata_reads == 3  # dev before; global before and after


def test_preview_fallback_does_not_write_provider_playlist_cache():
    provider = Provider()

    def mutate(instance, endpoint, kwargs):
        if endpoint.startswith("playlists/") and not kwargs["use_global_session"]:
            raise MediaNotFoundError()

    provider.mutate = mutate
    result = asyncio.run(spotify.preview_playlist(provider, PLAYLIST))
    assert result["snapshot_id"] == "snapshot-A"
    assert not provider.global_required
    assert not any(call[0].endswith("/items") for call in provider.calls)


def test_access_denied_does_not_produce_an_archive():
    provider = Provider()

    def mutate(instance, endpoint, kwargs):
        if endpoint.startswith("playlists/"):
            raise MediaNotFoundError()

    provider.mutate = mutate
    with pytest.raises(MediaNotFoundError):
        asyncio.run(spotify.capture_playlist(provider, PLAYLIST))


def test_limit_rejects_before_paging_and_liked_songs_is_unsupported():
    provider = Provider([None] * 2)
    with pytest.raises(spotify.SpotifyCaptureError, match="exceeds"):
        asyncio.run(spotify.capture_playlist(provider, PLAYLIST, max_items=1))
    assert not any(call[0].endswith("/items") for call in provider.calls)
    with pytest.raises(spotify.SpotifyCaptureError, match="concrete"):
        asyncio.run(spotify.preview_playlist(provider, "liked-songs-spotify-instance"))
