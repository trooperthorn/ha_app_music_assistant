"""
Give Sendspin players an Opus bitrate setting.

aiosendspin encodes Opus with libopus defaults and ignores the codec options
its transformer pool already carries, so a phone on a poor link has no rung
below "Opus at whatever the encoder picks". This script edits three
installed modules at image build time:

- ``aiosendspin/audio/codecs.py``: ``OpusEncoder`` keeps its ``options`` and
  applies ``options["bit_rate"]`` (bits per second) to the encoder context.
- ``aiosendspin/server/roles/player/v1.py``: the player role gets
  ``set_opus_bit_rate(bit_rate)``, passes the bitrate as a codec option to the
  transformer pool and the stream's audio requirements (so encoders with
  different bitrates never share a pipeline), and re-announces the stream
  when the bitrate changes mid-stream, the same way a format change does.
- ``music_assistant/providers/sendspin/player.py``: a per-player config entry
  ``sendspin_opus_bitrate`` (0 is the encoder default) applied on every config
  update next to the preferred format.

The fork frontend's web player drives it from its adaptive mode and the phone
layout's menu. Every edit is anchored on exact upstream lines and the script
fails loudly when an anchor is gone, so a release that reshapes any of the
three modules breaks the image build (and the sync pull request) instead of
silently shipping without the setting. Running it twice is a no-op.

Usage: ``python sendspin_opus_bitrate.py [codecs.py v1.py player.py]``.
Without paths the installed modules are located through ``importlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_opus_bitrate"

CODECS = "aiosendspin.audio.codecs"
PLAYER_ROLE = "aiosendspin.server.roles.player.v1"
PROVIDER = "music_assistant.providers.sendspin.player"

EDITS: dict[str, list[tuple[str, str]]] = {
    CODECS: [
        (
            "        options: Mapping[str, str] | None = None,  # noqa: ARG002 - uses libopus defaults\n"
            "    ) -> None:\n"
            '        """Initialize Opus encoder with audio format parameters."""\n',
            "        options: Mapping[str, str] | None = None,\n"
            "    ) -> None:\n"
            '        """Initialize Opus encoder with audio format parameters."""\n'
            f"        self._options = options or {{}}  {MARKER}\n",
        ),
        (
            '        self._encoder.format = "s16"\n',
            '        self._encoder.format = "s16"\n'
            f'        if bit_rate := self._options.get("bit_rate"):  {MARKER}\n'
            "            self._encoder.bit_rate = int(bit_rate)\n",
        ),
    ],
    PLAYER_ROLE: [
        (
            "        self._audio_requirements = audio_requirements\n",
            f"        self._audio_requirements = audio_requirements\n        self._opus_bit_rate: int | None = None  {MARKER}\n",
        ),
        (
            "    def set_volume(self, volume: int) -> None:\n",
            f"    def set_opus_bit_rate(self, bit_rate: int | None) -> None:  {MARKER}\n"
            '        """Set or clear the Opus bitrate (bits per second); the stream follows at once."""\n'
            "        bit_rate = bit_rate if bit_rate and bit_rate > 0 else None\n"
            "        if bit_rate == self._opus_bit_rate:\n"
            "            return\n"
            "        self._opus_bit_rate = bit_rate\n"
            "        if self._preferred_codec != AudioCodec.OPUS:\n"
            "            return\n"
            "        self._ensure_audio_requirements(force=True)\n"
            "        if self._client.group.has_active_stream:\n"
            "            self._begin_format_transition()\n"
            "\n"
            "    def _opus_options(self) -> dict[str, str] | None:\n"
            '        """The codec options for this player\'s Opus encoder, None for the defaults."""\n'
            '        return {"bit_rate": str(self._opus_bit_rate)} if self._opus_bit_rate else None\n'
            "\n"
            "    def set_volume(self, volume: int) -> None:\n",
        ),
        (
            "                OpusEncoder,\n"
            "                channel_id=channel_id_int,\n"
            "                sample_rate=audio_format.sample_rate,\n"
            "                bit_depth=audio_format.bit_depth,\n"
            "                channels=audio_format.channels,\n"
            "                frame_duration_us=frame_duration_us,\n"
            "            )\n",
            "                OpusEncoder,\n"
            "                channel_id=channel_id_int,\n"
            "                sample_rate=audio_format.sample_rate,\n"
            "                bit_depth=audio_format.bit_depth,\n"
            "                channels=audio_format.channels,\n"
            "                frame_duration_us=frame_duration_us,\n"
            f"                options=self._opus_options(),  {MARKER}\n"
            "            )\n",
        ),
        (
            "        self._audio_requirements = AudioRequirements(\n"
            "            sample_rate=audio_format.sample_rate,\n"
            "            bit_depth=audio_format.bit_depth,\n"
            "            channels=audio_format.channels,\n"
            "            transformer=transformer,\n"
            "            channel_id=channel_id,\n"
            "            frame_duration_us=frame_duration_us,\n"
            "        )\n",
            "        self._audio_requirements = AudioRequirements(\n"
            "            sample_rate=audio_format.sample_rate,\n"
            "            bit_depth=audio_format.bit_depth,\n"
            "            channels=audio_format.channels,\n"
            "            transformer=transformer,\n"
            "            channel_id=channel_id,\n"
            "            frame_duration_us=frame_duration_us,\n"
            f"            transform_options=(  {MARKER}\n"
            "                self._opus_options() if audio_codec == AudioCodec.OPUS else None\n"
            "            ),\n"
            "        )\n",
        ),
    ],
    PROVIDER: [
        (
            'SENDSPIN_FORMAT_AUTOMATIC = "automatic"\n',
            'SENDSPIN_FORMAT_AUTOMATIC = "automatic"\n'
            f'CONF_SENDSPIN_OPUS_BITRATE = "sendspin_opus_bitrate"  {MARKER}\n'
            "# bits per second; 0 leaves the encoder's own default\n"
            "SENDSPIN_OPUS_BITRATES = (0, 48000, 64000, 96000, 128000, 160000, 192000, 256000)\n",
        ),
        (
            "                        advanced=True,\n"
            "                    )\n"
            "                )\n"
            "\n"
            "        if (\n"
            "            player_role is not None\n"
            "            and PlayerCommand.SET_STATIC_DELAY in player_role.state_supported_commands\n"
            "        ):\n",
            "                        advanced=True,\n"
            "                    )\n"
            "                )\n"
            f"                entries.append(  {MARKER}\n"
            "                    ConfigEntry(\n"
            "                        key=CONF_SENDSPIN_OPUS_BITRATE,\n"
            "                        type=ConfigEntryType.INTEGER,\n"
            '                        category="protocol_generic",\n'
            "                        default_value=0,\n"
            "                        options=[\n"
            "                            ConfigValueOption(\n"
            "                                rate,\n"
            '                                title="Encoder default" if rate == 0 else f"{rate // 1000} kb/s",\n'
            "                            )\n"
            "                            for rate in SENDSPIN_OPUS_BITRATES\n"
            "                        ],\n"
            '                        description="Opus bitrate for this player; only used when the '
            'stream is Opus.",\n'
            "                        advanced=True,\n"
            "                    )\n"
            "                )\n"
            "\n"
            "        if (\n"
            "            player_role is not None\n"
            "            and PlayerCommand.SET_STATIC_DELAY in player_role.state_supported_commands\n"
            "        ):\n",
        ),
        (
            "    async def on_config_updated(self) -> None:\n"
            '        """Handle logic when the PlayerConfig is first loaded or updated."""\n'
            "        await self._apply_preferred_format()\n",
            "    async def on_config_updated(self) -> None:\n"
            '        """Handle logic when the PlayerConfig is first loaded or updated."""\n'
            f"        self._apply_opus_bitrate()  {MARKER}\n"
            "        await self._apply_preferred_format()\n",
        ),
        (
            "    async def _apply_preferred_format(self) -> None:\n",
            f"    def _apply_opus_bitrate(self) -> None:  {MARKER}\n"
            '        """Read config and hand the player role its Opus bitrate."""\n'
            "        player_role = self._player_role\n"
            "        if player_role is None:\n"
            "            return\n"
            "        value = self.config.get_value(CONF_SENDSPIN_OPUS_BITRATE, 0)\n"
            "        bit_rate = int(value) if isinstance(value, int | float | str) and str(value).isdigit() else 0\n"
            '        setter = getattr(player_role, "set_opus_bit_rate", None)\n'
            "        if setter is not None:\n"
            "            setter(bit_rate or None)\n"
            "\n"
            "    async def _apply_preferred_format(self) -> None:\n",
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
            raise SystemExit(f"anchor found {count} times, expected once; the module changed upstream:\n{anchor}")
        text = text.replace(anchor, replacement, 1)
    return text


def patch_file(path: Path, edits: list[tuple[str, str]]) -> None:
    original = path.read_text(encoding="utf-8")
    patched = apply(original, edits)
    if patched == original:
        print(f"sendspin_opus_bitrate: already applied to {path}")
        return
    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8", newline="\n")
    print(f"sendspin_opus_bitrate: applied to {path}")


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
