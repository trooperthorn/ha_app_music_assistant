"""
Teach the Home Assistant player provider to select an input on the wrapped entity.

The upstream provider imports every Home Assistant media player as a Music
Assistant player with power, volume and mute, but its source list only carries
a passive "External Source" entry: the entity's own inputs (AUDIO2 on a Yamaha
zone, Source 2 on a Monoprice zone) are not mirrored and there is no
select_source command. The routing view in the fork frontend needs both, so
this script edits the installed provider in place at image build time:

- ``source_list`` on the entity becomes one selectable PlayerSource per input,
  which makes the server add PlayerFeature.SELECT_SOURCE by itself (it does so
  for any player with two or more selectable sources).
- ``source`` on the entity is mirrored as ``extra_attributes["hass_source"]``
  so a frontend can tell which input a zone is on without touching
  ``active_source``, whose meaning (Music Assistant queue or external session)
  the provider already owns.
- ``select_source`` calls ``media_player.select_source`` on the entity.

Every edit is anchored on an exact upstream line and the script fails loudly
when an anchor is gone, so a server release that reshapes the provider breaks
the image build (and the sync pull request) instead of silently shipping
without the feature. Running it twice is a no-op.

Usage: ``python hass_source_select.py [path/to/hass_players/player.py]``.
Without a path the installed module is located through ``importlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: hass_source_select"

EDITS: list[tuple[str, str]] = [
    # the errors module for the refusal in select_source
    (
        "from music_assistant_models.media_items import MediaItemImage\n",
        "from music_assistant_models.errors import PlayerCommandFailed\n"
        "from music_assistant_models.media_items import MediaItemImage\n",
    ),
    # the entity's inputs and its current input, kept in step with its attributes
    (
        '            elif key == "is_volume_muted":\n'
        "                self._attr_volume_muted = value\n",
        '            elif key == "is_volume_muted":\n'
        "                self._attr_volume_muted = value\n"
        f'            elif key == "source_list":  {MARKER}\n'
        "                self._set_hass_source_list(value)\n"
        '            elif key == "source":\n'
        '                self._extra_attributes["hass_source"] = (\n'
        "                    value if isinstance(value, str) and value else None\n"
        "                )\n",
    ),
    # the command itself, plus the helper that rebuilds the source list
    (
        "    def update_from_compressed_state(self, state: CompressedState) -> None:\n",
        f"    def _set_hass_source_list(self, value: Any) -> None:  {MARKER}\n"
        '        """Mirror the entity\'s selectable inputs as player sources."""\n'
        "        names = [x for x in value if isinstance(x, str)] if isinstance(value, list) else []\n"
        '        external = [x for x in self._attr_source_list if x.id == "External"]\n'
        "        self._attr_source_list = [\n"
        "            PlayerSource(id=name, name=name, passive=False) for name in names\n"
        "        ] + external\n"
        "\n"
        "    async def select_source(self, source: str) -> None:\n"
        '        """Handle SELECT SOURCE command on the player: pick one of the entity\'s inputs."""\n'
        "        if not any(x.id == source and not x.passive for x in self._attr_source_list):\n"
        '            raise PlayerCommandFailed(f"{source} is not an input of {self.display_name}")\n'
        '        self._extra_attributes["hass_source"] = source\n'
        "        await self.hass.call_service(\n"
        '            domain="media_player",\n'
        '            service="select_source",\n'
        '            target={"entity_id": self.player_id},\n'
        '            service_data={"source": source},\n'
        "        )\n"
        "\n"
        "    def update_from_compressed_state(self, state: CompressedState) -> None:\n",
    ),
]


def locate() -> Path:
    """The installed provider module."""
    import importlib.util

    spec = importlib.util.find_spec("music_assistant.providers.hass_players.player")
    if spec is None or spec.origin is None:
        raise SystemExit("music_assistant.providers.hass_players.player is not installed")
    return Path(spec.origin)


def apply(text: str) -> str:
    """Return the patched module text; the input text when it is already patched."""
    if MARKER in text:
        return text
    for anchor, replacement in EDITS:
        count = text.count(anchor)
        if count != 1:
            raise SystemExit(
                f"anchor found {count} times, expected once; the provider changed upstream:\n{anchor}"
            )
        text = text.replace(anchor, replacement, 1)
    return text


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else locate()
    original = path.read_text(encoding="utf-8")
    patched = apply(original)
    if patched == original:
        print(f"hass_source_select: already applied to {path}")
        return 0
    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8", newline="\n")
    print(f"hass_source_select: applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
