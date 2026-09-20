"""Bounded raw playlist reads through Spotify's pinned 2.10.4 provider.

No MA item hydration, independent HTTP session, or remote writes. Credentials stay
inside the existing provider, whose normal refresh may persist rotated tokens.
Private hooks are intentionally limited to those verified in server e30a4974.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


class SpotifyCaptureError(ValueError):
    """Source cannot be captured completely and consistently."""


def _validate(provider: Any, playlist_id: str, max_items: int) -> None:
    if not isinstance(playlist_id, str) or not re.fullmatch(r"[A-Za-z0-9]{22}", playlist_id):
        raise SpotifyCaptureError("A concrete Spotify playlist ID is required (Liked Songs is unsupported)")
    if type(max_items) is not int or not 1 <= max_items <= 10000:
        raise SpotifyCaptureError("Capture limit must be between 1 and 10000")
    for hook in ("_get_data", "_get_auth_info", "_playlist_requires_global_token", "_set_playlist_requires_global_token"):
        if not callable(getattr(provider, hook, None)):
            raise SpotifyCaptureError("Spotify provider is incompatible with the pinned capture adapter")


class _Session:
    def __init__(self, provider: Any, use_global: bool) -> None:
        self.provider = provider
        self.use_global = use_global
        self.dev_active = bool(provider.dev_session_active)
        self.account_id: str | None = None
        self.checked_token: str | None = None

    async def request(self, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        if bool(self.provider.dev_session_active) != self.dev_active:
            raise SpotifyCaptureError("Spotify session selection changed during capture")
        auth = await self.provider._get_auth_info(use_global_session=self.use_global)
        if self.account_id is not None and auth.get("access_token") != self.checked_token and endpoint != "me":
            identity = await self.provider._get_data("me", use_global_session=self.use_global, auth_info=auth)
            if identity.get("id") != self.account_id:
                raise SpotifyCaptureError("Spotify account changed during capture")
            self.checked_token = auth.get("access_token")
        result = await self.provider._get_data(
            endpoint, use_global_session=self.use_global, auth_info=auth, **kwargs
        )
        if not isinstance(result, dict):
            raise SpotifyCaptureError("Malformed Spotify response")
        if endpoint == "me":
            if self.account_id is not None and result.get("id") != self.account_id:
                raise SpotifyCaptureError("Spotify account changed during capture")
            self.account_id = result.get("id")
            self.checked_token = auth.get("access_token")
        return result

    async def account(self) -> str:
        # Query the authenticated session, never the playlist owner or a cached global user.
        account = (await self.request("me")).get("id")
        if not isinstance(account, str) or not account:
            raise SpotifyCaptureError("Authenticated Spotify account is unavailable")
        return account


def _metadata(raw: dict[str, Any], max_items: int) -> dict[str, Any]:
    snapshot = raw.get("snapshot_id")
    container = raw.get("items") if isinstance(raw.get("items"), dict) else raw.get("tracks")
    total = container.get("total") if isinstance(container, dict) else None
    if not isinstance(snapshot, str) or not snapshot or type(total) is not int or total < 0:
        raise SpotifyCaptureError("Playlist snapshot or complete total is unavailable")
    if total > max_items:
        raise SpotifyCaptureError(f"Playlist exceeds capture limit of {max_items}")
    return {"snapshot_id": snapshot, "total": total, "name": str(raw.get("name") or "")}


def _occurrence(position: int, raw: Any) -> dict[str, Any]:
    item = (raw.get("item") or raw.get("track")) if isinstance(raw, dict) else None
    item = item if isinstance(item, dict) else {}
    if isinstance(raw, dict) and (raw.get("is_local") or item.get("is_local")):
        state = "local"
    elif item.get("type") == "episode":
        state = "episode"
    elif not item:
        state = "null"
    elif not item.get("id") or item.get("is_playable") is False:
        state = "unavailable"
    elif item.get("type") not in (None, "track"):
        state = "unsupported"
    else:
        state = "track"
    return {"position": position, "source_item_id": item.get("id"), "state": state, "source_payload": deepcopy(raw)}


async def _read(provider: Any, playlist_id: str, max_items: int, capture: bool, on_page: Any) -> dict[str, Any]:
    _validate(provider, playlist_id, max_items)
    use_global = bool(await provider._playlist_requires_global_token(playlist_id))
    for attempt in range(2):
        try:
            return await _read_session(provider, playlist_id, max_items, capture, on_page, use_global)
        except Exception as exc:
            # Exactly the pinned provider's restricted-playlist fallback; restart the whole
            # traversal so observations from two authorization contexts cannot be combined.
            if exc.__class__.__name__ != "MediaNotFoundError" or use_global or not provider.dev_session_active or attempt:
                raise
            use_global = True
            if capture:
                await provider._set_playlist_requires_global_token(playlist_id)
    raise SpotifyCaptureError("Playlist is inaccessible")


async def _read_session(provider: Any, playlist_id: str, max_items: int, capture: bool, on_page: Any, use_global: bool) -> dict[str, Any]:
    session = _Session(provider, use_global)
    account = await session.account()
    endpoint = f"playlists/{playlist_id}"
    before = _metadata(await session.request(endpoint), max_items)
    result = {**before, "account_id": account, "provider_instance_id": provider.instance_id, "source_playlist_id": playlist_id}
    if not capture:
        if await session.account() != account:
            raise SpotifyCaptureError("Spotify account changed during preview")
        return result
    occurrences: list[Any] = []
    while len(occurrences) < before["total"]:
        offset = len(occurrences)
        limit = min(50, before["total"] - offset)
        page = await session.request(f"{endpoint}/items", offset=offset, limit=limit)
        rows = page.get("items")
        if (
            page.get("total") != before["total"]
            or page.get("offset") != offset
            or not isinstance(rows, list)
            or len(rows) != limit
        ):
            raise SpotifyCaptureError("Playlist pagination was incomplete or changed")
        # Preserve every wrapper verbatim, including nulls, repeated tracks, episodes,
        # local entries and unavailable placeholders; list index is source occurrence order.
        occurrences.extend(_occurrence(offset + index, row) for index, row in enumerate(rows))
        if on_page is not None:
            await on_page(len(occurrences))
    after = _metadata(await session.request(endpoint), max_items)
    if await session.account() != account:
        raise SpotifyCaptureError("Spotify account changed during capture")
    if before["snapshot_id"] != after["snapshot_id"] or before["total"] != after["total"]:
        raise SpotifyCaptureError("Playlist snapshot changed during capture")
    return {**result, "snapshot_before": before["snapshot_id"], "snapshot_after": after["snapshot_id"], "occurrences": occurrences}


async def preview_playlist(provider: Any, source_playlist_id: str, *, max_items: int = 10000) -> dict[str, Any]:
    """Read fresh metadata/eligibility without track hydration or library updates."""
    return await _read(provider, source_playlist_id, max_items, False, None)


async def capture_playlist(provider: Any, source_playlist_id: str, *, max_items: int = 10000, on_page: Any = None) -> dict[str, Any]:
    """Capture a bounded, snapshot-checked ordered sequence or raise without a result."""
    return await _read(provider, source_playlist_id, max_items, True, on_page)
