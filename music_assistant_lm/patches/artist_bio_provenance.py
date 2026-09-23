"""Record the provider and observation time of the selected artist biography.

The pinned metadata merge discards which provider supplied the selected bio.
These fields describe where and when Music Assistant observed that text, not
when the provider originally wrote it. Existing biographies remain unattributed
until the next metadata refresh.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MARKER = "# trooperthorn: artist_bio_provenance"
MODEL = "music_assistant_models.media_items.metadata"
ENRICHMENT = "music_assistant.controllers.metadata.enrichment"
ARTISTS = "music_assistant.controllers.music.media.artists"


def _replace_once(source: str, old: str, new: str, module: str) -> str:
    count = source.count(old)
    if count != 1:
        raise SystemExit(f"Artist bio provenance anchor found {count} times in {module}, expected once")
    return source.replace(old, new, 1)


def apply(source: str, module: str) -> str:
    """Patch one pinned module exactly once, failing on upstream drift."""
    if module not in (MODEL, ENRICHMENT, ARTISTS):
        raise ValueError(f"Unsupported module: {module}")
    if MARKER in source:
        return source
    if module == MODEL:
        source = _replace_once(
            source,
            "    description_language: str | None = None\n    # Artist-specific metadata",
            "    description_language: str | None = None\n"
            "    description_source: str | None = None  " + MARKER + "\n"
            "    description_observed_at: int | None = None\n"
            "    # Artist-specific metadata",
            module,
        )
        source = _replace_once(
            source,
            'if fld.name in ("description", "description_language"):',
            'if fld.name in ("description", "description_language", '
            '"description_source", "description_observed_at"):',
            module,
        )
    elif module == ENRICHMENT:
        source = _replace_once(
            source,
            "        prev_description_language = artist.metadata.description_language\n"
            "        description_candidates: list[tuple[str | None, str]] = []",
            "        prev_description_language = artist.metadata.description_language\n"
            "        prev_description_source = artist.metadata.description_source\n"
            "        prev_description_observed_at = artist.metadata.description_observed_at\n"
            "        description_candidates: list[tuple[str | None, str, str]] = []  " + MARKER,
            module,
        )
        source = _replace_once(
            source,
            "(prov_item.metadata.description_language, prov_item.metadata.description)\n",
            "(prov_item.metadata.description_language, prov_item.metadata.description, prov.instance_id)\n",
            module,
        )
        source = _replace_once(
            source,
            "(metadata.description_language, metadata.description)\n",
            "(metadata.description_language, metadata.description, provider.instance_id)\n",
            module,
        )
        source = _replace_once(
            source,
            "        artist.metadata.description, artist.metadata.description_language = (\n"
            "            self._select_description(\n"
            "                description_candidates, prev_description, prev_description_language\n"
            "            )\n"
            "        )\n",
            "        (artist.metadata.description, artist.metadata.description_language,\n"
            "         artist.metadata.description_source, observed) = self._select_description(\n"
            "            description_candidates, prev_description, prev_description_language,\n"
            "            prev_description_source,\n"
            "        )\n"
            "        artist.metadata.description_observed_at = (\n"
            "            int(time()) if observed else prev_description_observed_at\n"
            "        )\n",
            module,
        )
        start = source.index("    def _select_description(\n")
        end = source.index("    async def _update_album_metadata(", start)
        old_method = source[start:end]
        if (
            "candidates: Sequence[tuple[str | None, str]]" not in old_method
            or "return prev_description, prev_description_language" not in old_method
        ):
            raise SystemExit("Artist bio selector anchor changed")
        new_method = '''    def _select_description(
        self,
        candidates: Sequence[tuple[str | None, str, str]],
        prev_description: str | None,
        prev_description_language: str | None,
        prev_description_source: str | None,
    ) -> tuple[str | None, str | None, str | None, bool]:
        """Choose the bio and retain provenance if a stored bio wins."""
        if prev_description_source == "manual" and prev_description is not None:
            return prev_description, prev_description_language, "manual", False
        pref = self.preferred_language
        for lang, text, source in candidates:
            if lang == pref:
                return text, lang, source, True
        if prev_description is not None and prev_description_language == pref:
            return prev_description, prev_description_language, prev_description_source, False
        for lang, text, source in candidates:
            if lang == "en":
                return text, lang, source, True
        if candidates:
            lang, text, source = candidates[0]
            return text, lang, source, True
        return prev_description, prev_description_language, prev_description_source, False

'''
        source = source[:start] + new_method + source[end:]
    else:
        source = _replace_once(
            source,
            "import contextlib\n",
            "import contextlib\nfrom time import time  " + MARKER + "\n",
            module,
        )
        source = _replace_once(
            source,
            "            metadata = metadata_for_update(cur_item.metadata, update.metadata, overwrite)\n",
            "            metadata = metadata_for_update(cur_item.metadata, update.metadata, overwrite)\n"
            "            if overwrite and update.metadata.description != cur_item.metadata.description:\n"
            "                # An explicit library edit owns its biography across metadata refreshes.\n"
            "                metadata.description_source = (\"manual\" if metadata.description else None)\n"
            "                metadata.description_observed_at = int(time()) if metadata.description else None\n",
            module,
        )
    compile(source, module, "exec")
    return source


def main(argv: list[str]) -> int:
    """Patch installed model and server sources in the pinned app image."""
    if len(argv) not in (1, 4):
        raise SystemExit("expected zero or three source paths")
    if len(argv) == 4:
        paths = (Path(argv[1]), Path(argv[2]), Path(argv[3]))
    else:
        specs = tuple(importlib.util.find_spec(module) for module in (MODEL, ENRICHMENT, ARTISTS))
        if any(spec is None or spec.origin is None for spec in specs):
            raise SystemExit("Music Assistant model or metadata module is not installed")
        paths = tuple(Path(spec.origin) for spec in specs)  # type: ignore[union-attr]
    for path, module in zip(paths, (MODEL, ENRICHMENT, ARTISTS), strict=True):
        source = path.read_text(encoding="utf-8")
        result = apply(source, module)
        if result != source:
            path.write_text(result, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
