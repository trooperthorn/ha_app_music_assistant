"""Expose Sendspin source health and guarded stop alongside native routing."""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_source_status"
MODULE = "music_assistant.providers.sendspin_source.provider"

EDITS: list[tuple[str, str]] = [
    (
        "from music_assistant_models.enums import (\n",
        "from music_assistant_models.auth import Scope\nfrom music_assistant_models.enums import (\n",
    ),
    (
        "        self._server_unsubscribe: Callable[[], None] | None = None\n",
        "        self._server_unsubscribe: Callable[[], None] | None = None\n"
        "        self._status_unsubscribe: Callable[[], None] | None = None\n"
        "        self._stop_unsubscribe: Callable[[], None] | None = None\n",
    ),
    (
        "        self._unloading = False\n        if (sendspin := self._sendspin_provider) is None:\n",
        "        self._unloading = False\n"
        f"        {MARKER}\n"
        "        self._status_unsubscribe = self.mass.register_api_command(\n"
        '            "sendspin_source/status",\n'
        "            self.source_status,\n"
        "            required_scope=Scope.CONFIG_PROVIDERS_READ,\n"
        "        )\n"
        "        self._stop_unsubscribe = self.mass.register_api_command(\n"
        '            "sendspin_source/stop",\n'
        "            self.stop_source,\n"
        "            required_scope=Scope.PLAYERS_CONTROL,\n"
        "        )\n"
        "        if (sendspin := self._sendspin_provider) is None:\n",
    ),
    (
        "        self._unloading = True\n        if self._server_unsubscribe is not None:\n",
        "        self._unloading = True\n"
        "        if self._status_unsubscribe is not None:\n"
        "            self._status_unsubscribe()\n"
        "            self._status_unsubscribe = None\n"
        "        if self._stop_unsubscribe is not None:\n"
        "            self._stop_unsubscribe()\n"
        "            self._stop_unsubscribe = None\n"
        "        if self._server_unsubscribe is not None:\n",
    ),
    (
        "    async def get_audio_sources(self) -> list[AudioSource]:\n",
        "    async def source_status(self) -> dict[str, object]:\n"
        '        """Report connected source signal and stream state for diagnostics."""\n'
        "        sendspin = self._sendspin_provider\n"
        "        target_latency_ms = (\n"
        '            cast("int | None", self.config.get_value(CONF_TARGET_LATENCY))\n'
        "            or DEFAULT_TARGET_LATENCY_MS\n"
        "        )\n"
        "        sources = []\n"
        "        if sendspin is not None:\n"
        "            for client in sendspin.server_api.connected_clients:\n"
        "                if self._get_source_role(client) is None:\n"
        "                    continue\n"
        "                state = self._clients.get(client.client_id)\n"
        "                session = state.session if state else None\n"
        "                last_pcm_age_ms = (\n"
        "                    round(max(0, time.monotonic() - session.last_pcm_monotonic) * 1000)\n"
        "                    if session and session.pcm_received.is_set()\n"
        "                    else None\n"
        "                )\n"
        "                receiving = bool(\n"
        "                    session is not None\n"
        "                    and session.bridge is not None\n"
        "                    and session.ingest_task is not None\n"
        "                    and not session.ingest_task.done()\n"
        "                    and last_pcm_age_ms is not None\n"
        "                    and last_pcm_age_ms <= 2000\n"
        "                )\n"
        "                info = client.info_or_none\n"
        "                sources.append(\n"
        "                    {\n"
        '                        "client_id": client.client_id,\n'
        '                        "name": info.name if info else client.client_id,\n'
        '                        "source_uri": create_uri(MediaType.AUDIO_SOURCE, self.instance_id, client.client_id),\n'
        '                        "signal": state.signal.value if state and state.signal else None,\n'
        '                        "selected_player_id": session.player_id if session else None,\n'
        '                        "owner_player_id": session.owner_player_id if session else None,\n'
        '                        "playback_session_id": session.playback_session_id if session else None,\n'
        '                        "receiving_pcm": receiving,\n'
        '                        "last_pcm_age_ms": last_pcm_age_ms,\n'
        '                        "bridge_buffer_ms": (\n'
        '                            round(session.bridge.occupancy_us / 1000)\n'
        '                            if session and session.bridge and receiving else None\n'
        '                        ),\n'
        "                    }\n"
        "                )\n"
        '        return {"target_latency_ms": target_latency_ms, "sources": sources}\n'
        "\n"
        "    async def stop_source(self, client_id: str, playback_session_id: str) -> None:\n"
        '        """Stop only the exact Sendspin source selection the caller observed."""\n'
        "        state = self._clients.get(client_id)\n"
        "        session = state.session if state else None\n"
        "        if session is None or session.playback_session_id != playback_session_id:\n"
        '            raise PlayerCommandFailed("Sendspin source selection changed; refresh before stopping")\n'
        "        self.mass.player_queues._check_player_permission(session.owner_player_id)\n"
        "        await self.mass.players.deselect_source(\n"
        "            session.owner_player_id,\n"
        "            provider_instance_id=self.instance_id,\n"
        "            source_id=client_id,\n"
        "            playback_session_id=playback_session_id,\n"
        "        )\n"
        "\n"
        "    async def get_audio_sources(self) -> list[AudioSource]:\n",
    ),
]


def apply(source: str) -> str:
    """Patch exact pinned-server anchors and fail if the module changes."""
    if MARKER in source:
        return source
    for anchor, replacement in EDITS:
        count = source.count(anchor)
        if count != 1:
            raise SystemExit(f"Sendspin source status anchor found {count} times, expected once")
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
