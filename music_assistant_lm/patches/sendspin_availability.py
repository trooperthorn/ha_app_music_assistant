"""Reflect occupied Sendspin clients in Music Assistant player availability.

aiosendspin handles the protocol transition to a solo stopped group, but it
does not publish a client update when ``available`` changes. The MA provider
therefore keeps the player selectable. Both edits are pinned and fail closed.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_availability"
CLIENT_MODULE = "aiosendspin.server.client"
PLAYER_MODULE = "music_assistant.providers.sendspin.player"

CLIENT_ANCHOR = (
    "        if not available:\n"
    "            await self._handle_external_source_transition()\n"
    "\n"
    "    async def _handle_external_source_transition(self) -> None:\n"
)
CLIENT_REPLACEMENT = (
    "        if not available:\n"
    "            await self._handle_external_source_transition()\n"
    "        if old_available != available:\n"
    f"            self._server._signal_client_updated(self._client_id)  {MARKER}\n"
    "\n"
    "    async def _handle_external_source_transition(self) -> None:\n"
)
PLAYER_ANCHOR = "        self._attr_available = True\n"
PLAYER_REPLACEMENT = f"        self._attr_available = sendspin_client.available  {MARKER}\n"


def apply(source: str, module: str) -> str:
    """Patch one exact pinned module, or reject upstream drift."""
    if module == CLIENT_MODULE:
        anchor, replacement = CLIENT_ANCHOR, CLIENT_REPLACEMENT
    elif module == PLAYER_MODULE:
        anchor, replacement = PLAYER_ANCHOR, PLAYER_REPLACEMENT
    else:
        raise ValueError(f"Unsupported module: {module}")
    if MARKER in source:
        return source
    count = source.count(anchor)
    if count != 1:
        raise SystemExit(f"Sendspin availability anchor found {count} times in {module}, expected once")
    result = source.replace(anchor, replacement, 1)
    compile(result, module, "exec")
    return result


def main(argv: list[str]) -> int:
    if len(argv) not in (1, 3):
        raise SystemExit("expected zero or two source paths")
    if len(argv) == 3:
        paths = (Path(argv[1]), Path(argv[2]))
    else:
        import importlib.util

        specs = (importlib.util.find_spec(CLIENT_MODULE), importlib.util.find_spec(PLAYER_MODULE))
        if any(spec is None or spec.origin is None for spec in specs):
            raise SystemExit("Sendspin client or MA player module is not installed")
        paths = tuple(Path(spec.origin) for spec in specs)  # type: ignore[union-attr]
    for path, module in zip(paths, (CLIENT_MODULE, PLAYER_MODULE), strict=True):
        source = path.read_text(encoding="utf-8")
        result = apply(source, module)
        if result != source:
            path.write_text(result, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
