"""Accept current-spec output-delay frames alongside pinned legacy Sendspin frames.

aiosendspin 9.1.1 calls this value static_delay_ms. Current Sendspin calls it
output_delay_ms and advertises set_output_delay. Preserve the legacy wire path
for existing clients; select the command the connected client advertises.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_output_delay_compat"
TYPES = "aiosendspin.models.types"
MODEL = "aiosendspin.models.player"
ROLE = "aiosendspin.server.roles.player.v1"
PROVIDER = "music_assistant.providers.sendspin.player"

EDITS: dict[str, list[tuple[str, str]]] = {
    TYPES: [
        ('    SET_STATIC_DELAY = "set_static_delay"\n',
         '    SET_STATIC_DELAY = "set_static_delay"\n    SET_OUTPUT_DELAY = "set_output_delay"\n'),
    ],
    MODEL: [
        ('    static_delay_ms: int | None = None\n    """Static delay in milliseconds (0-5000). Required on the initial state message;\n',
         '    output_delay_ms: int | None = None\n    """Current-spec output delay in milliseconds (0-5000)."""\n'
         '    static_delay_ms: int | None = None\n    """Static delay in milliseconds (0-5000). Required on the initial state message;\n'),
        ('        if self.static_delay_ms is not None and not 0 <= self.static_delay_ms <= 5000:\n',
         '        if self.output_delay_ms is not None and not 0 <= self.output_delay_ms <= 5000:\n'
         '            raise ValueError("output_delay_ms must be in range 0-5000")\n'
         '        if self.static_delay_ms is not None and not 0 <= self.static_delay_ms <= 5000:\n'),
        ('        VALID_STATE_COMMANDS = {PlayerCommand.SET_STATIC_DELAY}  # noqa: N806\n',
         '        VALID_STATE_COMMANDS = {PlayerCommand.SET_STATIC_DELAY, PlayerCommand.SET_OUTPUT_DELAY}  # noqa: N806\n'),
        ('    static_delay_ms: int | None = None\n    """Delay in milliseconds (0-5000), only set if command is set_static_delay."""\n',
         '    output_delay_ms: int | None = None\n    """Current-spec delay, only set if command is set_output_delay."""\n'
         '    static_delay_ms: int | None = None\n    """Delay in milliseconds (0-5000), only set if command is set_static_delay."""\n'),
        ('        if self.command == PlayerCommand.SET_STATIC_DELAY:\n',
         '        if self.command == PlayerCommand.SET_OUTPUT_DELAY:\n'
         '            if self.output_delay_ms is None or not 0 <= self.output_delay_ms <= 5000:\n'
         '                raise ValueError("output_delay_ms must be in range 0-5000 for set_output_delay")\n'
         '        elif self.output_delay_ms is not None:\n'
         '            raise ValueError("output_delay_ms is only valid for set_output_delay")\n'
         '        if self.command == PlayerCommand.SET_STATIC_DELAY:\n'),
    ],
    ROLE: [
        ('        if PlayerCommand.SET_STATIC_DELAY not in self.state_supported_commands:\n            return\n\n'
         '        self._client.send_message(\n',
         '        commands = self.state_supported_commands\n'
         '        if PlayerCommand.SET_OUTPUT_DELAY in commands:\n'
         '            command = PlayerCommand.SET_OUTPUT_DELAY\n'
         '            fields = {"output_delay_ms": delay_ms}\n'
         '        elif PlayerCommand.SET_STATIC_DELAY in commands:\n'
         '            command = PlayerCommand.SET_STATIC_DELAY\n'
         '            fields = {"static_delay_ms": delay_ms}\n'
         '        else:\n'
         '            return\n\n'
         '        self._client.send_message(\n'),
        ('                        command=PlayerCommand.SET_STATIC_DELAY,\n                        static_delay_ms=delay_ms,\n',
         '                        command=command,\n                        **fields,\n'),
        ('            player.static_delay_ms is None\n            or player.required_lead_time_ms is None\n',
         '            (player.static_delay_ms is None and player.output_delay_ms is None)\n'
         '            or player.required_lead_time_ms is None\n'),
        ('        if state.static_delay_ms is not None and self.static_delay_ms != state.static_delay_ms:\n'
         '            self.static_delay_ms = state.static_delay_ms\n'
         '            self.emit_client_event(StaticDelayChangedEvent(static_delay_ms=state.static_delay_ms))\n',
         '        reported_delay = (state.output_delay_ms if state.output_delay_ms is not None\n'
         '                          else state.static_delay_ms)\n'
         '        if reported_delay is not None and self.static_delay_ms != reported_delay:\n'
         '            self.static_delay_ms = reported_delay\n'
         '            self.emit_client_event(StaticDelayChangedEvent(static_delay_ms=reported_delay))\n'),
    ],
    PROVIDER: [
        ('                and PlayerCommand.SET_STATIC_DELAY in player_role.state_supported_commands\n',
         '                and ({PlayerCommand.SET_STATIC_DELAY, PlayerCommand.SET_OUTPUT_DELAY}\n'
         '                     & set(player_role.state_supported_commands))\n'),
    ],
}


def apply(source: str, module: str) -> str:
    """Patch exact pinned anchors or fail the image build on upstream drift."""
    if module not in EDITS:
        raise ValueError(f"Unsupported module: {module}")
    if MARKER in source:
        return source
    for anchor, replacement in EDITS[module]:
        count = source.count(anchor)
        if count != 1:
            raise SystemExit(f"Sendspin output delay anchor found {count} times in {module}, expected once")
        source = source.replace(anchor, replacement, 1)
    source += f"\n{MARKER}\n"
    compile(source, module, "exec")
    return source


def main(argv: list[str]) -> int:
    modules = (TYPES, MODEL, ROLE, PROVIDER)
    if len(argv) not in (1, len(modules) + 1):
        raise SystemExit("expected zero or four source paths")
    if len(argv) == 1:
        specs = [importlib.util.find_spec(module) for module in modules]
        if any(spec is None or spec.origin is None for spec in specs):
            raise SystemExit("Pinned Sendspin modules are not installed")
        paths = [Path(spec.origin) for spec in specs]  # type: ignore[union-attr]
    else:
        paths = [Path(arg) for arg in argv[1:]]
    for module, path in zip(modules, paths, strict=True):
        source = path.read_text(encoding="utf-8")
        result = apply(source, module)
        if result != source:
            path.write_text(result, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
