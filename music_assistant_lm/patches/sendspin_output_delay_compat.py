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
        ('from dataclasses import dataclass\n', 'from dataclasses import dataclass, field\n'),
        ('    supported_commands: list[PlayerCommand]\n    """Subset of: \'volume\', \'mute\'."""\n',
         '    supported_commands: list[PlayerCommand] = field(default_factory=list)\n'
         '    """Legacy hello commands; current-spec clients advertise in client/state."""\n'),
        ('    static_delay_ms: int | None = None\n    """Static delay in milliseconds (0-5000). Required on the initial state message;\n',
         '    output_delay_ms: int | None = None\n    """Current-spec output delay in milliseconds (0-5000)."""\n'
         '    static_delay_ms: int | None = None\n    """Static delay in milliseconds (0-5000). Required on the initial state message;\n'),
        ('        if self.static_delay_ms is not None and not 0 <= self.static_delay_ms <= 5000:\n',
         '        if self.output_delay_ms is not None and not 0 <= self.output_delay_ms <= 5000:\n'
         '            raise ValueError("output_delay_ms must be in range 0-5000")\n'
         '        if self.static_delay_ms is not None and not 0 <= self.static_delay_ms <= 5000:\n'),
        ('        VALID_STATE_COMMANDS = {PlayerCommand.SET_STATIC_DELAY}  # noqa: N806\n',
         '        VALID_STATE_COMMANDS = {PlayerCommand.VOLUME, PlayerCommand.MUTE,\n'
         '                                PlayerCommand.SET_STATIC_DELAY, PlayerCommand.SET_OUTPUT_DELAY}  # noqa: N806\n'),
        ('    supported_commands: list[PlayerCommand] | None = None\n'
         '    """Subset of: \'set_static_delay\'. Commands this player supports via client/state."""\n',
         '    supported_commands: list[PlayerCommand] | None = None\n'
         '    """Commands this player supports via client/state."""\n'
         '    format: SupportedAudioFormat | None = None\n'
         '    """Current-spec preferred audio format, if the client selects one."""\n'),
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
        ('from aiosendspin.models.player import PlayerCommandPayload, StreamStartPlayer, SupportedAudioFormat\n',
         'from aiosendspin.models.player import (\n'
         '    PlayerCommandPayload, StreamRequestFormatPlayer, StreamStartPlayer, SupportedAudioFormat,\n'
         ')\n'),
        ('        self._last_sent_format: tuple[AudioCodec, int, int, int, str | None] | None = None\n',
         '        self._last_sent_format: tuple[AudioCodec, int, int, int, str | None] | None = None\n'
         '        self._client_format_override_active = False\n'),
        ('        self._ensure_preferred_format()\n        self._ensure_audio_requirements(force=True)\n\n'
         '    def on_deactivate(self) -> None:\n',
         '        self._client_format_override_active = False\n'
         '        self._ensure_preferred_format()\n        self._ensure_audio_requirements(force=True)\n\n'
         '    def on_deactivate(self) -> None:\n'),
        ('        if not support or PlayerCommand.VOLUME not in support.supported_commands:\n            return\n',
         '        commands = set(support.supported_commands if support else []) | set(self.state_supported_commands)\n'
         '        if PlayerCommand.VOLUME not in commands:\n            return\n'),
        ('        if not support or PlayerCommand.MUTE not in support.supported_commands:\n            return\n',
         '        commands = set(support.supported_commands if support else []) | set(self.state_supported_commands)\n'
         '        if PlayerCommand.MUTE not in commands:\n            return\n'),
        ('        commands = support.supported_commands if support else []\n'
         '        if PlayerCommand.VOLUME in commands and player.volume is None:\n',
         '        commands = set(support.supported_commands if support else []) | set(player.supported_commands or [])\n'
         '        if PlayerCommand.VOLUME in commands and player.volume is None:\n'),
        ('        support = self._client.info.player_support\n'
         '        commands = support.supported_commands if support else []\n'
         '        reasons: list[str] = []\n',
         '        reasons: list[str] = []\n'),
        ('        if state.volume is not None and PlayerCommand.VOLUME not in commands:\n'
         '            reasons.append("sent volume without declaring the volume command")\n'
         '        if state.muted is not None and PlayerCommand.MUTE not in commands:\n'
         '            reasons.append("sent muted without declaring the mute command")\n',
         '        # Current-spec clients may report read-only volume and mute.\n'),
        ('        support = self._client.info.player_support\n'
         '        commands = support.supported_commands if support else []\n'
         '        changed = False\n',
         '        changed = False\n'),
        ('            and PlayerCommand.VOLUME in commands\n            and self.volume != state.volume\n',
         '            and self.volume != state.volume\n'),
        ('        if state.muted is not None and PlayerCommand.MUTE in commands and self.muted != state.muted:\n',
         '        if state.muted is not None and self.muted != state.muted:\n'),
        ('        if changed:\n'
         '            self.emit_client_event(VolumeChangedEvent(volume=self.volume, muted=self.muted))\n\n'
         '        if state.supported_commands is not None:\n'
         '            self.state_supported_commands = state.supported_commands\n',
         '        if (state.supported_commands is not None\n'
         '                and self.state_supported_commands != state.supported_commands):\n'
         '            self.state_supported_commands = state.supported_commands\n'
         '            changed = True\n'
         '        if changed:\n'
         '            self.emit_client_event(VolumeChangedEvent(volume=self.volume, muted=self.muted))\n'),
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
        ('        if state.min_buffer_ms is not None and self.min_buffer_ms != state.min_buffer_ms:\n'
         '            self.min_buffer_ms = state.min_buffer_ms\n'
         '            self.emit_client_event(MinBufferChangedEvent(min_buffer_ms=state.min_buffer_ms))\n',
         '        if state.min_buffer_ms is not None and self.min_buffer_ms != state.min_buffer_ms:\n'
         '            self.min_buffer_ms = state.min_buffer_ms\n'
         '            self.emit_client_event(MinBufferChangedEvent(min_buffer_ms=state.min_buffer_ms))\n'
         '        if state.format is not None:\n'
         '            fmt = state.format\n'
         '            self.on_stream_request_format(StreamRequestFormatPayload(player=StreamRequestFormatPlayer(\n'
         '                codec=fmt.codec, sample_rate=fmt.sample_rate, channels=fmt.channels,\n'
         '                bit_depth=fmt.bit_depth,\n'
         '            )))\n'
         '            self._client_format_override_active = True\n'
         '        elif self._client_format_override_active:\n'
         '            # A full player state without format clears the client preference.\n'
         '            # Restore the server override, if one is configured, or hello priority.\n'
         '            self._client_format_override_active = False\n'
         '            previous_format = self._effective_format()\n'
         '            self._ensure_preferred_format()\n'
         '            self._ensure_audio_requirements(force=True)\n'
         '            if (self._client.group.has_active_stream\n'
         '                    and self._effective_format() != previous_format):\n'
         '                self._begin_format_transition()\n'),
    ],
    PROVIDER: [
        ('            case VolumeChangedEvent(volume=volume, muted=muted):\n'
         '                self._attr_volume_level = volume\n'
         '                self._attr_volume_muted = muted\n'
         '                self.update_state()\n'
         '            case StaticDelayChangedEvent(static_delay_ms=delay_ms):\n',
         '            case VolumeChangedEvent(volume=volume, muted=muted):\n'
         '                self._attr_volume_level = volume\n'
         '                self._attr_volume_muted = muted\n'
         '                role = self._player_role\n'
         '                if role is not None:\n'
         '                    support = self.api.info.player_support\n'
         '                    advertised = set(role.state_supported_commands)\n'
         '                    if support is not None:\n'
         '                        advertised.update(support.supported_commands)\n'
         '                    for command, feature in ((PlayerCommand.VOLUME, PlayerFeature.VOLUME_SET),\n'
         '                                             (PlayerCommand.MUTE, PlayerFeature.VOLUME_MUTE)):\n'
         '                        if command in advertised:\n'
         '                            self._attr_supported_features.add(feature)\n'
         '                        else:\n'
         '                            self._attr_supported_features.discard(feature)\n'
         '                self.update_state()\n'
         '            case StaticDelayChangedEvent(static_delay_ms=delay_ms):\n'),
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
