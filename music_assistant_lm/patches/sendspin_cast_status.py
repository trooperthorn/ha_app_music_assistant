"""Publish bounded Cast receiver state on its Sendspin player.

The pinned bridge receives periodic receiver status but only logs errors.
Expose state transitions through the existing player API so HTTP/ingress
clients can distinguish idle, connecting, ready, playing, and failed.
Receiver log text and arbitrary status messages are not forwarded.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_cast_status"
MODULE = "music_assistant.providers.chromecast.sendspin_bridge"

EDITS: list[tuple[str, str]] = [
    (
        "        on_cast_connected: Callable[[], None] | None = None,\n    ) -> None:\n",
        "        on_cast_connected: Callable[[], None] | None = None,\n"
        "        on_cast_status: Callable[[str], None] | None = None,\n"
        "    ) -> None:\n",
    ),
    (
        "        self._on_cast_connected = on_cast_connected\n",
        "        self._on_cast_connected = on_cast_connected\n        self._on_cast_status = on_cast_status\n",
    ),
    (
        '        state = data.get("state")\n        message = data.get("message", "")\n        if state == "error":\n',
        '        state = data.get("state")\n'
        '        message = data.get("message", "")\n'
        f'        if state in ("connecting", "connected", "playing", "stopped", "error") and self._on_cast_status:  {MARKER}\n'
        "            self._on_cast_status(state)\n"
        '        if state == "error":\n',
    ),
    (
        "            on_cast_connected=self._on_cast_connected,\n        )\n",
        "            on_cast_connected=self._on_cast_connected,\n            on_cast_status=self._on_cast_receiver_status,\n        )\n",
    ),
    (
        "        # Recorded on the Cast player, not on the bridge: the re-evaluation below takes\n",
        '        self._publish_cast_receiver_status("error", "audio_unsupported")\n'
        "        # Recorded on the Cast player, not on the bridge: the re-evaluation below takes\n",
    ),
    (
        '    def _on_cast_connected(self) -> None:\n        """Handle Cast app "connected" status (called from socket thread)."""\n',
        "    def _on_cast_receiver_status(self, state: str) -> None:\n"
        '        """Transfer a validated receiver state from the Cast socket thread."""\n'
        "        self.mass.loop.call_soon_threadsafe(self._publish_cast_receiver_status, state)\n"
        "\n"
        "    def _publish_cast_receiver_status(self, state: str, reason: str | None = None) -> None:\n"
        '        """Send one state transition through the normal player event stream."""\n'
        "        player = self.mass.players.get_player(self._bridge_client_id)\n"
        "        if player is None:\n"
        "            return\n"
        '        if state == "error":\n'
        "            reason = reason if reason in (\n"
        '                "device_unavailable", "launch_timeout", "launch_failed",\n'
        '                "receiver_error", "audio_unsupported",\n'
        '            ) else "receiver_error"\n'
        "        else:\n"
        "            reason = None\n"
        "        if (\n"
        '            player.extra_attributes.get("sendspin_cast_state") == state\n'
        '            and player.extra_attributes.get("sendspin_cast_failure") == reason\n'
        "        ):\n"
        "            return\n"
        '        player.extra_attributes["sendspin_cast_state"] = state\n'
        "        if reason is None:\n"
        '            player.extra_attributes.pop("sendspin_cast_failure", None)\n'
        "        else:\n"
        '            player.extra_attributes["sendspin_cast_failure"] = reason\n'
        "        player.update_state()\n"
        "\n"
        "    def _on_cast_connected(self) -> None:\n"
        '        """Handle Cast app "connected" status (called from socket thread)."""\n',
    ),
    (
        "        self._cast_app_was_active = False\n        self._cast_app_connected = False\n        self.logger.info(\n",
        "        self._cast_app_was_active = False\n"
        "        self._cast_app_connected = False\n"
        '        self._publish_cast_receiver_status("disconnected")\n'
        "        self.logger.info(\n",
    ),
    (
        "            self.logger.warning(\n"
        '                "Cannot start Sendspin stream for %s: player not available",\n'
        "                self.cast_player.display_name,\n"
        "            )\n"
        "            return\n",
        "            self.logger.warning(\n"
        '                "Cannot start Sendspin stream for %s: player not available",\n'
        "                self.cast_player.display_name,\n"
        "            )\n"
        '            self._publish_cast_receiver_status("error", "device_unavailable")\n'
        '            self._resolve_cast_app_ready(PlayerCommandFailed(f"{self.cast_player.display_name} is unavailable."))\n'
        "            return\n",
    ),
    (
        "        self.cast_player.cancel_pending_app_quit()\n        try:\n",
        '        self._publish_cast_receiver_status("connecting")\n        self.cast_player.cancel_pending_app_quit()\n        try:\n',
    ),
    (
        '        except TimeoutError:\n            self.logger.warning(\n                "Timed out launching Sendspin Cast App on %s",\n',
        "        except TimeoutError:\n"
        '            self._publish_cast_receiver_status("error", "launch_timeout")\n'
        "            self._resolve_cast_app_ready(\n"
        '                PlayerCommandFailed(f"Timed out launching Sendspin on {self.cast_player.display_name}.")\n'
        "            )\n"
        "            self.logger.warning(\n"
        '                "Timed out launching Sendspin Cast App on %s",\n',
    ),
    (
        "        except Exception as err:\n"
        "            self.logger.error(\n"
        '                "Failed to launch Sendspin Cast App on %s: %s",\n',
        "        except Exception as err:\n"
        '            self._publish_cast_receiver_status("error", "launch_failed")\n'
        '            self._resolve_cast_app_ready(PlayerCommandFailed(f"Failed to launch Sendspin on {self.cast_player.display_name}."))\n'
        "            self.logger.error(\n"
        '                "Failed to launch Sendspin Cast App on %s: %s",\n',
    ),
]


def apply(source: str) -> str:
    """Patch exact pinned-server anchors and fail on upstream drift."""
    if MARKER in source:
        return source
    for anchor, replacement in EDITS:
        count = source.count(anchor)
        if count != 1:
            raise SystemExit(f"Cast status anchor found {count} times, expected once:\n{anchor}")
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
