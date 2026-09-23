"""Expose the Cast receiver's persisted Sendspin delay before it connects.

The pinned server sends the bridge player's saved sendspin_static_delay to the
Cast receiver as syncDelay, including after config changes. Its provisional
bridge role advertises no SET_STATIC_DELAY command, so the normal settings
entry is otherwise absent while the Cast receiver is idle. The receiver's
documented range is 0..5000 ms; this patch does not claim that other bridges
support the same control.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_cast_delay"
MODULE = "music_assistant.providers.sendspin.player"

ANCHOR = (
    "        if (\n"
    "            player_role is not None\n"
    "            and PlayerCommand.SET_STATIC_DELAY in player_role.state_supported_commands\n"
    "        ):\n"
    "            entries.append(\n"
)
REPLACEMENT = (
    f"        # Cast's receiver consumes syncDelay from bridge config even while idle.  {MARKER}\n"
    "        underlying = (\n"
    "            self.mass.players.get_player(self.underlying_player_id)\n"
    "            if self.underlying_player_id else None\n"
    "        )\n"
    '        cast_bridge = underlying is not None and underlying.provider.domain == "chromecast"\n'
    "        if (\n"
    "            cast_bridge\n"
    "            or (\n"
    "                player_role is not None\n"
    "                and PlayerCommand.SET_STATIC_DELAY in player_role.state_supported_commands\n"
    "            )\n"
    "        ):\n"
    "            entries.append(\n"
)


def apply(source: str) -> str:
    """Patch exactly the pinned config-entry gate, idempotently."""
    if MARKER in source:
        return source
    count = source.count(ANCHOR)
    if count != 1:
        raise SystemExit(f"Cast delay anchor found {count} times, expected once")
    result = source.replace(ANCHOR, REPLACEMENT, 1)
    compile(result, MODULE, "exec")
    return result


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        raise SystemExit("expected zero or one source path")
    if len(argv) == 2:
        path = Path(argv[1])
    else:
        import importlib.util

        spec = importlib.util.find_spec(MODULE)
        if spec is None or spec.origin is None:
            raise SystemExit(f"{MODULE} is not installed")
        path = Path(spec.origin)
    source = path.read_text(encoding="utf-8")
    result = apply(source)
    if result != source:
        path.write_text(result, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
