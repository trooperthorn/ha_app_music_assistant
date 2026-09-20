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

An explicit local-only request prefixes that same uri with ``local-only:``.
For a builtin M3U, which reconstructs media items instead of returning its raw
path, the equivalent entry directive is
``#EXTPROV:local_only||<provider-instance>``. The marker is consumed before
queue resolution. The queue item then also carries
``extra_attributes["strict_provider"]``. The provider must be loaded,
available, and non-streaming; selection and capacity retries may not widen
outside that provider instance or domain.

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
            f"        steered: dict[int, tuple[str, bool]] = {{}}  {MARKER}\n"
            "        # the subset of media_items the user explicitly picked to play next\n",
        ),
        # Parse the optional local-only prefix outside the skip-on-error block: an invalid
        # strict request must fail closed rather than be turned into a skipped item.
        (
            "        for item in media_list:\n"
            "            try:\n",
            "        for item in media_list:\n"
            f"            steer_uri, strict_provider = self._play_source_steer(item)  {MARKER}\n"
            "            try:\n",
        ),
        # the uri as the caller wrote it, before it resolves to the library item
        (
            "                media_item: MediaItemType | ItemMapping | BrowseFolder\n"
            "                if isinstance(item, str):\n"
            "                    media_item = await self.mass.music.get_item_by_uri(item)\n",
            "                media_item: MediaItemType | ItemMapping | BrowseFolder\n"
            "                if isinstance(item, str):\n"
            "                    media_item = await self.mass.music.get_item_by_uri(steer_uri)\n",
        ),
        (
            "                if isinstance(media_item, ItemMapping):\n"
            "                    # Resolve any ItemMapping to its full media item, exactly as the str-uri\n",
            f"                if steer_uri is None:  {MARKER}\n"
            '                    steer_uri = getattr(media_item, "uri", None)\n'
            "                steer_provider = self._play_source_provider(\n"
            "                    steer_uri, strict_provider\n"
            "                )\n"
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
            "                        steer_value = (steer_provider, strict_provider is not None)\n"
            "                        steered.update((id(x), steer_value) for x in resolved_items)\n"
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
            "            if steer := steered.get(id(x)):\n"
            "                steer_provider, strict_steer = steer\n"
            '                queue_item.extra_attributes["preferred_provider"] = steer_provider\n'
            "                if strict_steer:\n"
            '                    queue_item.extra_attributes["strict_provider"] = steer_provider\n'
            "            queue_items.append(queue_item)\n",
        ),
        (
            "    async def _enter_dynamic_mode(self, queue_id: str, option: QueueOption | None) -> None:\n",
            f"    def _play_source_steer(self, item: object) -> tuple[str | None, str | None]:  {MARKER}\n"
            '        """Consume the direct-uri prefix or a builtin-M3U provider marker."""\n'
            "        if not isinstance(item, str):\n"
            '            mappings = getattr(item, "provider_mappings", set())\n'
            "            markers = [\n"
            "                mapping for mapping in mappings\n"
            '                if mapping.provider_domain == "local_only"\n'
            "            ]\n"
            "            if not markers:\n"
            '                return getattr(item, "uri", None), None\n'
            "            if len(markers) != 1 or not markers[0].item_id:\n"
            '                raise InvalidDataError("Invalid local-only provider marker in playlist")\n'
            "            mappings.discard(markers[0])\n"
            '            return getattr(item, "uri", None), markers[0].item_id\n'
            '        if not item.startswith("local-only:"):\n'
            "            return item, None\n"
            '        uri = item.removeprefix("local-only:")\n'
            '        if not uri or "://" not in uri:\n'
            '            raise InvalidDataError("Invalid local-only uri: expected local-only:<provider-uri>")\n'
            '        return uri, uri.split("://", 1)[0]\n'
            "\n"
            "    def _play_source_provider(\n"
            "        self, uri: str | None, strict_provider: str | None\n"
            "    ) -> str | None:\n"
            '        """Return and, for local-only, validate the provider named by a uri."""\n'
            '        if not uri or "://" not in uri:\n'
            "            return None\n"
            '        provider_id = uri.split("://", 1)[0]\n'
            "        provider_id = strict_provider or provider_id\n"
            '        if provider_id in ("library", "builtin"):\n'
            "            if strict_provider:\n"
            '                raise InvalidDataError(f"Local-only uri must name a provider: {uri}")\n'
            "            return None\n"
            "        provider = self.mass.get_provider(provider_id, return_unavailable=True)\n"
            "        if provider and provider.available and not provider.is_streaming_provider:\n"
            "            return strict_provider or provider.instance_id\n"
            "        if strict_provider:\n"
            '            raise InvalidDataError(\n'
            '                f"Local-only provider {provider_id!r} is unavailable or is a streaming provider"\n'
            "            )\n"
            "        return provider.instance_id if provider and provider.available else None\n"
            "\n"
            "    async def _enter_dynamic_mode(self, queue_id: str, option: QueueOption | None) -> None:\n",
        ),
    ],
    STREAMS_AUDIO: [
        (
            "        time_start = time.time()\n"
            '        self.logger.debug("Getting streamdetails for %s", queue_item.uri)\n',
            "        time_start = time.time()\n"
            f'        strict_provider = queue_item.extra_attributes.get("strict_provider")  {MARKER}\n'
            '        self.logger.debug("Getting streamdetails for %s", queue_item.uri)\n',
        ),
        (
            "            queue_item.streamdetails\n"
            "            # cached details of an excluded instance are exactly what we select away from\n",
            "            queue_item.streamdetails\n"
            f"            # strict plays may never reuse details from another provider  {MARKER}\n"
            "            and (\n"
            "                not strict_provider\n"
            "                or (strict_stream_provider := mass.get_provider(\n"
            "                    queue_item.streamdetails.provider, return_unavailable=True\n"
            "                ))\n"
            "                and not strict_stream_provider.is_streaming_provider\n"
            "                and strict_provider in (\n"
            "                    strict_stream_provider.instance_id, strict_stream_provider.domain\n"
            "                )\n"
            "            )\n"
            "            # cached details of an excluded instance are exactly what we select away from\n",
        ),
        (
            "            candidates = self._get_streamdetail_candidates(\n"
            "                media_item.provider_mappings,\n"
            "                preferred_providers,\n"
            "                excluded_provider_instances,\n"
            "            )\n",
            f'            if steer := queue_item.extra_attributes.get("preferred_provider"):  {MARKER}\n'
            "                preferred_providers = [steer, *preferred_providers]\n"
            "            if strict_provider:\n"
            "                preferred_providers = [strict_provider]\n"
            "            candidates = self._get_streamdetail_candidates(\n"
            "                media_item.provider_mappings,\n"
            "                preferred_providers,\n"
            "                excluded_provider_instances,\n"
            "            )\n"
            "            if strict_provider:\n"
            "                candidates = [\n"
            "                    (mapping, provider)\n"
            "                    for mapping, provider in candidates\n"
            "                    if not provider.is_streaming_provider\n"
            "                    and strict_provider in (provider.instance_id, provider.domain)\n"
            "                ]\n"
            "                if not candidates:\n"
            "                    raise MediaNotFoundError(\n"
            '                        f"Local-only provider {strict_provider!r} cannot serve "\n'
            '                        f"{queue_item.name} ({queue_item.uri})"\n'
            "                    )\n",
        ),
        (
            "        all_candidate_instances = {\n"
            "            provider.instance_id\n"
            "            for mapping in (\n"
            "                queue_item.media_item.provider_mappings if queue_item.media_item else ()\n"
            "            )\n"
            "            if mapping.available\n"
            "            for provider in self._get_mapping_providers(mapping)\n"
            "        }\n",
            f'        strict_provider = queue_item.extra_attributes.get("strict_provider")  {MARKER}\n'
            "        all_candidate_instances = {\n"
            "            provider.instance_id\n"
            "            for mapping in (\n"
            "                queue_item.media_item.provider_mappings if queue_item.media_item else ()\n"
            "            )\n"
            "            if mapping.available\n"
            "            for provider in self._get_mapping_providers(mapping)\n"
            "            if not strict_provider\n"
            "            or (\n"
            "                not provider.is_streaming_provider\n"
            "                and strict_provider in (provider.instance_id, provider.domain)\n"
            "            )\n"
            "        }\n",
        ),
        (
            "        match_pending = (\n"
            "            allow_provider_match\n",
            "        match_pending = (\n"
            f"            not strict_provider  {MARKER}\n"
            "            and allow_provider_match\n",
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
