"""Advertise the group-switch command implemented by pinned aiosendspin.

aiosendspin 9.1.1 handles controller `switch` at the client role and cycles
through playing groups. MA's controller command list omits the capability,
so spec-compliant clients must refuse to send it until this is advertised.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_controller_switch"
MODULE = "music_assistant.providers.sendspin.player"
ANCHOR = "    MediaCommand.SEEK,\n    MediaCommand.SEEK_RELATIVE,\n]\n\n# Config constants for Sendspin audio format\n"
REPLACEMENT = (
    "    MediaCommand.SEEK,\n"
    "    MediaCommand.SEEK_RELATIVE,\n"
    f"    MediaCommand.SWITCH,  {MARKER}\n"
    "]\n\n# Config constants for Sendspin audio format\n"
)


def apply(source: str) -> str:
    """Patch the exact pinned command list and fail on upstream drift."""
    if MARKER in source:
        return source
    count = source.count(ANCHOR)
    if count != 1:
        raise SystemExit(f"Sendspin switch anchor found {count} times, expected once")
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
