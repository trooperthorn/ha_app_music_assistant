"""Expose timing values reported by Sendspin clients through player state.

The pinned aiosendspin role emits changes only after a client/state report.
These are observed values, separate from Music Assistant's saved config.
Missing reports remain unknown instead of being inferred from config or zeros.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_timing_status"
MODULE = "music_assistant.providers.sendspin.player"

EDITS: list[tuple[str, str]] = [
    (
        "from aiosendspin.server.roles.player.events import StaticDelayChangedEvent\n",
        "from aiosendspin.server.roles.player.events import (\n"
        "    MinBufferChangedEvent,\n"
        "    RequiredLeadTimeChangedEvent,\n"
        "    StaticDelayChangedEvent,\n"
        ")\n",
    ),
    (
        "        super()._refresh_client_info(sendspin_client, hello_payload=hello_payload)\n"
        "        client_info = hello_payload or sendspin_client.info\n",
        "        super()._refresh_client_info(sendspin_client, hello_payload=hello_payload)\n"
        f"        # A replacement transport must report its own timing.  {MARKER}\n"
        '        for key in ("sendspin_output_delay_ms", "sendspin_startup_lead_ms",\n'
        '                    "sendspin_min_buffer_ms", "sendspin_timing_reported_at"):\n'
        "            self.extra_attributes.pop(key, None)\n"
        "        client_info = hello_payload or sendspin_client.info\n",
    ),
    (
        "            case StaticDelayChangedEvent(static_delay_ms=delay_ms):\n"
        '                self.logger.debug("Static delay changed to %d ms", delay_ms)\n',
        "            case StaticDelayChangedEvent(static_delay_ms=delay_ms):\n"
        '                self.extra_attributes["sendspin_output_delay_ms"] = delay_ms\n'
        '                self.extra_attributes["sendspin_timing_reported_at"] = time.time()\n'
        "                self.update_state()\n"
        '                self.logger.debug("Static delay changed to %d ms", delay_ms)\n',
    ),
    (
        "            case _:\n                super().event_cb(client, event)\n\n    def group_event_cb(",
        "            case RequiredLeadTimeChangedEvent(required_lead_time_ms=lead_ms):\n"
        '                self.extra_attributes["sendspin_startup_lead_ms"] = lead_ms\n'
        '                self.extra_attributes["sendspin_timing_reported_at"] = time.time()\n'
        "                self.update_state()\n"
        "            case MinBufferChangedEvent(min_buffer_ms=buffer_ms):\n"
        '                self.extra_attributes["sendspin_min_buffer_ms"] = buffer_ms\n'
        '                self.extra_attributes["sendspin_timing_reported_at"] = time.time()\n'
        "                self.update_state()\n"
        "            case _:\n                super().event_cb(client, event)\n\n    def group_event_cb(",
    ),
]


def apply(source: str) -> str:
    """Patch exact pinned-server anchors and fail on upstream drift."""
    if MARKER in source:
        return source
    for anchor, replacement in EDITS:
        count = source.count(anchor)
        if count != 1:
            raise SystemExit(f"Sendspin timing anchor found {count} times, expected once:\n{anchor}")
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
