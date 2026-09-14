"""
Let a play request name the source its items should stream from.

Every uri the server is asked to play resolves to the full library item, and
the stream is then taken from whichever of the item's providers reports the
best quality (the playback user's provider filter being the only steer). A
track that is both on Spotify and on disk streams from Spotify however the
user found it, including from the fork frontend's Filesystem listing, which
is what put Spotify over its request budget. This script edits two installed
modules at image build time:

- ``player_queues/queue_loader.py``: when an item handed to ``play_media``
  carries a uri that names a loaded provider instance other than the library
  (``filesystem_local--xyz://track/...``), every queue item that request
  produces remembers it as ``extra_attributes["preferred_provider"]``.
- ``streams/audio.py``: ``get_stream_details`` puts that provider in front of
  the quality order, the same way the user's provider filter already is, so
  the stream comes from it when it can serve the item and falls back to the
  others otherwise.

The fork frontend's library manager sends such uris for a listing narrowed to
one source. Every edit is anchored on exact upstream lines and the script
fails loudly when an anchor is gone, so a server release that reshapes either
module breaks the image build (and the sync pull request) instead of silently
shipping without the feature. Running it twice is a no-op.

Usage: ``python play_source_steer.py [queue_loader.py streams/audio.py]``.
Without paths the installed modules are located through ``importlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: play_source_steer"

QUEUE_LOADER = "music_assistant.controllers.player_queues.queue_loader"
STREAMS_AUDIO = "music_assistant.controllers.streams.audio"

EDITS: dict[str, list[tuple[str, str]]] = {
    QUEUE_LOADER: [
        # a place to remember which resolved items asked for a source
        (
            "        media_items: list[MediaItemType] = []\n        # the subset of media_items the user explicitly picked to play next\n",
            "        media_items: list[MediaItemType] = []\n"
            f"        steered: dict[int, str] = {{}}  {MARKER}\n"
            "        # the subset of media_items the user explicitly picked to play next\n",
        ),
        # the uri as the caller wrote it, before it resolves to the library item
        (
            "                media_item: MediaItemType | ItemMapping | BrowseFolder\n"
            "                if isinstance(item, str):\n"
            "                    media_item = await self.mass.music.get_item_by_uri(item)\n",
            "                media_item: MediaItemType | ItemMapping | BrowseFolder\n"
            f"                steer_uri = item if isinstance(item, str) else None  {MARKER}\n"
            "                if isinstance(item, str):\n"
            "                    media_item = await self.mass.music.get_item_by_uri(item)\n",
        ),
        (
            "                if isinstance(media_item, ItemMapping):\n"
            "                    # Resolve any ItemMapping to its full media item, exactly as the str-uri\n",
            f"                if steer_uri is None:  {MARKER}\n"
            '                    steer_uri = getattr(media_item, "uri", None)\n'
            "                steer_provider = self._play_source_steer(steer_uri)\n"
            "                if isinstance(media_item, ItemMapping):\n"
            "                    # Resolve any ItemMapping to its full media item, exactly as the str-uri\n",
        ),
        # every item the request produced carries its source
        (
            "                    media_items += resolved_items\n"
            "                    if plays_next_track:\n"
            "                        play_next_items += resolved_items\n",
            "                    media_items += resolved_items\n"
            f"                    if steer_provider:  {MARKER}\n"
            "                        steered.update((id(x), steer_provider) for x in resolved_items)\n"
            "                    if plays_next_track:\n"
            "                        play_next_items += resolved_items\n",
        ),
        (
            "        queue_items: list[QueueItem] = [\n"
            '            build_queue_item(queue_id, cast("PlayableMediaItemType", x))\n'
            "            for x in media_items\n"
            "            if x and x.available\n"
            "        ]\n",
            f"        queue_items: list[QueueItem] = []  {MARKER}\n"
            "        for x in media_items:\n"
            "            if not (x and x.available):\n"
            "                continue\n"
            '            queue_item = build_queue_item(queue_id, cast("PlayableMediaItemType", x))\n'
            "            if steer_provider := steered.get(id(x)):\n"
            '                queue_item.extra_attributes["preferred_provider"] = steer_provider\n'
            "            queue_items.append(queue_item)\n",
        ),
        (
            "    async def _enter_dynamic_mode(self, queue_id: str, option: QueueOption | None) -> None:\n",
            f"    def _play_source_steer(self, uri: str | None) -> str | None:  {MARKER}\n"
            '        """The provider instance a uri asks to stream from, if it names a loaded one."""\n'
            '        if not uri or "://" not in uri:\n'
            "            return None\n"
            '        provider_id = uri.split("://", 1)[0]\n'
            '        if provider_id in ("library", "builtin"):\n'
            "            return None\n"
            "        provider = self.mass.get_provider(provider_id)\n"
            "        return provider.instance_id if provider else None\n"
            "\n"
            "    async def _enter_dynamic_mode(self, queue_id: str, option: QueueOption | None) -> None:\n",
        ),
    ],
    STREAMS_AUDIO: [
        (
            "            candidates = self._get_streamdetail_candidates(\n"
            "                media_item.provider_mappings,\n"
            "                preferred_providers,\n"
            "                excluded_provider_instances,\n"
            "            )\n",
            f'            if steer := queue_item.extra_attributes.get("preferred_provider"):  {MARKER}\n'
            "                preferred_providers = [steer, *preferred_providers]\n"
            "            candidates = self._get_streamdetail_candidates(\n"
            "                media_item.provider_mappings,\n"
            "                preferred_providers,\n"
            "                excluded_provider_instances,\n"
            "            )\n",
        ),
    ],
}


def locate(module: str) -> Path:
    """The installed module."""
    import importlib.util

    spec = importlib.util.find_spec(module)
    if spec is None or spec.origin is None:
        raise SystemExit(f"{module} is not installed")
    return Path(spec.origin)


def apply(text: str, edits: list[tuple[str, str]]) -> str:
    """Return the patched module text; the input text when it is already patched."""
    if MARKER in text:
        return text
    for anchor, replacement in edits:
        count = text.count(anchor)
        if count != 1:
            raise SystemExit(f"anchor found {count} times, expected once; the server changed upstream:\n{anchor}")
        text = text.replace(anchor, replacement, 1)
    return text


def patch_file(path: Path, edits: list[tuple[str, str]]) -> None:
    original = path.read_text(encoding="utf-8")
    patched = apply(original, edits)
    if patched == original:
        print(f"play_source_steer: already applied to {path}")
        return
    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8", newline="\n")
    print(f"play_source_steer: applied to {path}")


def main(argv: list[str]) -> int:
    modules = list(EDITS)
    if len(argv) > 1:
        paths = [Path(arg) for arg in argv[1:]]
        if len(paths) != len(modules):
            raise SystemExit(f"expected {len(modules)} paths, in the order {', '.join(modules)}")
    else:
        paths = [locate(module) for module in modules]
    for module, path in zip(modules, paths, strict=True):
        patch_file(path, EDITS[module])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
