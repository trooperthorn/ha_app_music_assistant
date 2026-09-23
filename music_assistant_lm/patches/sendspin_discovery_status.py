"""Expose read-only Sendspin discovery health to authorized HTTP clients.

The pinned provider starts both mDNS directions and manual outbound dials but
only logs their state. This command reports bounded status without credentials,
pairing material, or connection side effects.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: sendspin_discovery_status"
MODULE = "music_assistant.providers.sendspin.provider"

EDITS: list[tuple[str, str]] = [
    (
        '    async def loaded_in_mass(self) -> None:\n        """Call after the provider has been loaded."""\n',
        "    async def display_capabilities(self) -> dict[str, object]:\n"
        '        """Advertise support for paired, non-audio browser displays."""\n'
        '        return {"api_version": 1, "browser_display_pairing": True}\n'
        "\n"
        "    async def discovery_status(self) -> dict[str, Any]:\n"
        '        """Report listener, both mDNS directions, manual addresses and client counts."""\n'
        "        def safe_endpoint(address: str) -> str:\n"
        '            """Expose only host and port from URLs; paths and user info may be secrets."""\n'
        "            try:\n"
        '                parsed = urlsplit(address if "://" in address else f"//{address}")\n'
        "                host = parsed.hostname\n"
        "                port = parsed.port\n"
        "            except ValueError:\n"
        '                return "[redacted address]"\n'
        "            if host is None:\n"
        '                return "[redacted address]"\n'
        '            host = f"[{host}]" if ":" in host else host\n'
        '            scheme = f"{parsed.scheme}://" if parsed.scheme in ("ws", "wss") else ""\n'
        '            port_text = f":{port}" if port is not None else ""\n'
        '            return f"{scheme}{host}{port_text}"\n'
        "\n"
        "        server = self.server_api\n"
        "        manual = []\n"
        "        for address in self._manual_ip_config:\n"
        "            try:\n"
        "                url = _manual_client_url(address)\n"
        "            except ValueError:\n"
        "                valid = False\n"
        '                display_address = "[invalid address]"\n'
        "            else:\n"
        "                valid = True\n"
        "                display_address = safe_endpoint(url)\n"
        '            manual.append({"address": display_address, "valid": valid})\n'
        "        return {\n"
        '            "api_version": 1,\n'
        '            "listener_active": server._tcp_site is not None,\n'
        '            "listen_address": self.mass.streams.bind_ip,\n'
        '            "port": SENDSPIN_SERVER_PORT,\n'
        '            "advertising_active": server._mdns_service is not None,\n'
        '            "advertise_address": self.mass.streams.publish_ip,\n'
        '            "client_discovery_active": server._mdns_browser is not None,\n'
        '            "discovered_services": [\n'
        '                {"name": name, "url": safe_endpoint(url)}\n'
        "                for name, url in sorted(server._mdns_client_urls.items())\n"
        "            ],\n"
        '            "manual_addresses": manual,\n'
        '            "known_clients": len(server.clients),\n'
        '            "connected_clients": len(server.connected_clients),\n'
        '            "legacy_clients_allowed": bool(self.config.get_value(CONF_ALLOW_LEGACY_CLIENTS, True)),\n'
        "        }\n"
        "\n"
        "    async def loaded_in_mass(self) -> None:\n"
        '        """Call after the provider has been loaded."""\n',
    ),
    (
        "        await super().loaded_in_mass()\n"
        "        self.unregister_cbs.append(\n"
        "            self.mass.register_api_command(\n"
        '                "sendspin/pair_web_player",\n',
        "        await super().loaded_in_mass()\n"
        f"        # Admin-only read access: addresses describe the local network.  {MARKER}\n"
        "        self.unregister_cbs.append(\n"
        "            self.mass.register_api_command(\n"
        '                "sendspin/discovery_status",\n'
        "                self.discovery_status,\n"
        "                required_scope=Scope.CONFIG_PROVIDERS_READ,\n"
        "            )\n"
        "        )\n"
        "        self.unregister_cbs.append(\n"
        "            self.mass.register_api_command(\n"
        '                "sendspin/display_capabilities",\n'
        "                self.display_capabilities,\n"
        "                required_scope=Scope.PLAYERS_CONTROL,\n"
        "            )\n"
        "        )\n"
        "        self.unregister_cbs.append(\n"
        "            self.mass.register_api_command(\n"
        '                "sendspin/pair_web_player",\n',
    ),
]


def apply(source: str) -> str:
    """Patch exact pinned-server anchors and fail on upstream drift."""
    if MARKER in source:
        return source
    for anchor, replacement in EDITS:
        count = source.count(anchor)
        if count != 1:
            raise SystemExit(f"Sendspin discovery anchor found {count} times, expected once")
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
