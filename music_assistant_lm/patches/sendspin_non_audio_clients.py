"""Keep non-audio Sendspin roles out of the audio-player path."""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: classify non-audio Sendspin clients"
MODULE = "music_assistant.providers.sendspin.provider"

ANCHOR = (
    '        has_player_role = "player" in negotiated_families\n'
    '        has_metadata_role = "metadata" in negotiated_families\n'
    '        has_visualizer_role = "visualizer" in negotiated_families\n'
    "\n"
    "        if has_player_role:\n"
)
REPLACEMENT = (
    '        has_player_role = "player" in negotiated_families\n'
    '        has_metadata_role = "metadata" in negotiated_families\n'
    '        has_visualizer_role = "visualizer" in negotiated_families\n'
    f"        {MARKER}\n"
    '        has_artwork_role = "artwork" in negotiated_families\n'
    '        has_color_role = "color" in negotiated_families\n'
    "\n"
    "        if has_player_role:\n"
)

BRANCH_ANCHOR = (
    "        elif has_metadata_role or has_visualizer_role:\n"
    "            default_type = PlayerType.DISPLAY if has_metadata_role else PlayerType.VISUALIZER\n"
    "            viz_player = SendspinVisualizerPlayer(self, client_id, initial_hello=initial_hello)\n"
    "            viz_player._attr_type = bridge_player_type or default_type\n"
    "            player = viz_player\n"
)
BRANCH_REPLACEMENT = (
    "        elif has_metadata_role or has_visualizer_role or has_artwork_role or has_color_role:\n"
    "            default_type = (\n"
    "                PlayerType.DISPLAY if has_metadata_role or has_artwork_role else PlayerType.VISUALIZER\n"
    "            )\n"
    "            viz_player = SendspinVisualizerPlayer(self, client_id, initial_hello=initial_hello)\n"
    "            client_info = initial_hello or sendspin_client.info\n"
    "            device_info = client_info.device_info\n"
    "            # The account-bound web pairing endpoint also serves browser displays.\n"
    "            viz_player.is_web_player = bool(\n"
    '                device_info and device_info.product_name == "Music Assistant Display"\n'
    "            )\n"
    "            if viz_player.is_web_player:\n"
    "                viz_player._attr_private = True\n"
    "            viz_player._attr_type = bridge_player_type or default_type\n"
    "            player = viz_player\n"
)

FALLBACK_ANCHOR = (
    "        else:\n"
    "            audio_player = SendspinPlayer(self, client_id, initial_hello=initial_hello)\n"
    "            if isinstance(existing_player, SendspinPlayer):\n"
    "                audio_player.preserve_control_features_from(existing_player)\n"
    "            player = audio_player\n"
    "\n"
    "        if extra_ids:\n"
)
FALLBACK_REPLACEMENT = (
    "        else:\n"
    "            # Controller-only or unknown roles must never create an audio destination.\n"
    "            controller = SendspinVisualizerPlayer(self, client_id, initial_hello=initial_hello)\n"
    "            controller._attr_type = bridge_player_type or PlayerType.DISPLAY\n"
    "            controller._attr_supported_features = set()\n"
    "            player = controller\n"
    "\n"
    "        if extra_ids:\n"
)


def apply(source: str) -> str:
    """Patch the pinned provider once, failing closed on upstream drift."""
    if MARKER in source:
        return source
    for anchor, replacement in (
        (ANCHOR, REPLACEMENT),
        (BRANCH_ANCHOR, BRANCH_REPLACEMENT),
        (FALLBACK_ANCHOR, FALLBACK_REPLACEMENT),
    ):
        count = source.count(anchor)
        if count != 1:
            raise SystemExit(f"Sendspin non-audio anchor found {count} times, expected once")
        source = source.replace(anchor, replacement, 1)
    compile(source, MODULE, "exec")
    return source


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
