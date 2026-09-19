"""
Add a plugin that lets the frontend list folders under the app's music roots.

The filesystem provider asks for its path as a typed text entry and checks
nothing until it loads. No server command lists directories at all, so the
fork frontend's folder picker has nothing to call. This used to be an anchor
patch (`browse_path.py`, string-replacing exact lines in the installed
`music_assistant.controllers.config.providers` module) but the code it
injects has zero dependency on that controller: it never reads `self`, it
only needs `mass.register_api_command` and the standalone `is_safe_path`
helper. Anchoring it to a file we don't own bought nothing and cost fragility
-- any upstream reshape of that module would break the build. This plugin
ports it verbatim into a self-contained provider instead.

Music Assistant discovers providers by listing directories under its
providers path at runtime (`os.listdir(PROVIDERS_PATH)` in
`music_assistant/mass.py`), not through a central registry file. Adding a
new provider directory is therefore purely additive: it cannot conflict with
any future upstream diff to an existing file, unlike an anchor-based edit.
This script has no anchors to fail on; it always (re)writes the same three
files (`manifest.json`, `strings.json` and `__init__.py`). See
`playlist_bridge.py` for the sibling plugin this shape was copied from, and
`docs/decisions.md` for why this one moved.

The command name (`config/providers/browse_path`), its required scope
(`Scope.CONFIG_PROVIDERS_WRITE`), the browsable roots (`/music`, `/media`,
`/share`) and the return shape are a contract with the fork frontend's
folder picker and are carried over unchanged. `mass.register_api_command`
accepts any command string -- there is no namespace ownership -- so
registering the same `config/providers/...` name from a plugin needs no
frontend change at all. It does raise `RuntimeError` if the command is
already registered, which is why the old anchor patch and this plugin must
never both ship (see the Dockerfile and CI wiring).

Usage: ``python folder_browser.py [path/to/music_assistant/providers]``.
Without a path the installed providers package is located through
``importlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

DOMAIN = "folder_browser"

MANIFEST_JSON = """\
{
  "type": "plugin",
  "stage": "stable",
  "domain": "folder_browser",
  "name": "Folder Browser",
  "description": "List folders under the app's music roots, for picking a provider path.",
  "codeowners": ["@trooperthorn"],
  "requirements": [],
  "multi_instance": false,
  "builtin": true,
  "allow_disable": false
}
"""

STRINGS_JSON = """\
{
  "manifest": {
    "description": "List folders under the app's music roots, for picking a provider path."
  }
}
"""

INIT_PY = '''\
"""
List folders under the app's music roots, for the frontend's folder picker.

Added by trooperthorn/ha_app_music_assistant (see
music_assistant_lm/patches/folder_browser.py for why this is a plugin
rather than the anchor patch it replaces).

Ported essentially verbatim from the retired browse_path.py anchor patch:
the handler never touched `self` there either, so moving it into a plugin
provider changed nothing about what it does, only how it is delivered.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope

from music_assistant.helpers.security import is_safe_path
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return FolderBrowserProvider(mass, manifest, config)


class FolderBrowserProvider(PluginProvider):
    """Lists folders under the app's music roots for the provider config folder picker."""

    _unregister_handles: list[Callable[[], None]]

    async def loaded_in_mass(self) -> None:
        """Register API commands after the provider is loaded."""
        self._unregister_handles = [
            self.mass.register_api_command(
                "config/providers/browse_path",
                self.browse_path,
                required_scope=Scope.CONFIG_PROVIDERS_WRITE,
            )
        ]

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands when the provider is unloaded."""
        for unregister in getattr(self, "_unregister_handles", []):
            unregister()

    async def browse_path(self, path: str | None = None) -> dict[str, Any]:
        """
        List the folders under one of the app's music roots, for picking a provider path.

        :param path: A folder under a root; None returns the roots only.
        """
        roots: list[dict[str, Any]] = [
            {"path": "/music", "label": "Music drive"},
            {"path": "/media", "label": "Media"},
            {"path": "/share", "label": "Share"},
        ]
        for root in roots:
            root["present"] = await asyncio.to_thread(os.path.isdir, root["path"])
        if not path:
            return {"roots": roots, "path": None, "parent": None, "folders": []}
        norm = os.path.normpath(path)
        root_path = next((root["path"] for root in roots if is_safe_path(norm, root["path"])), None)
        if root_path is None or not os.path.isabs(norm):
            msg = f"{path} is outside the browsable folders"
            raise ValueError(msg)

        def _list() -> list[dict[str, str]]:
            folders: list[dict[str, str]] = []
            try:
                with os.scandir(norm) as entries:
                    for entry in entries:
                        if entry.name.startswith("."):
                            continue
                        try:
                            if not entry.is_dir():
                                continue
                        except OSError:
                            continue
                        folders.append({"name": entry.name, "path": os.path.join(norm, entry.name)})
            except OSError:
                return []
            folders.sort(key=lambda folder: folder["name"].casefold())
            return folders[:500]

        return {
            "roots": roots,
            "path": norm,
            "parent": None if norm == root_path else os.path.dirname(norm),
            "folders": await asyncio.to_thread(_list),
        }
'''


def locate() -> Path:
    """The installed providers package directory."""
    import importlib.util

    spec = importlib.util.find_spec("music_assistant.providers")
    if spec is None or spec.origin is None:
        raise SystemExit("music_assistant.providers is not installed")
    return Path(spec.origin).parent


def write_provider(providers_dir: Path) -> None:
    """Write the plugin's manifest, strings and module, creating its directory if needed."""
    import json

    provider_dir = providers_dir / DOMAIN
    provider_dir.mkdir(exist_ok=True)
    manifest_path = provider_dir / "manifest.json"
    strings_path = provider_dir / "strings.json"
    init_path = provider_dir / "__init__.py"
    compile(INIT_PY, str(init_path), "exec")
    json.loads(STRINGS_JSON)
    manifest_path.write_text(MANIFEST_JSON, encoding="utf-8", newline="\n")
    strings_path.write_text(STRINGS_JSON, encoding="utf-8", newline="\n")
    init_path.write_text(INIT_PY, encoding="utf-8", newline="\n")


def main(argv: list[str]) -> int:
    providers_dir = Path(argv[1]) if len(argv) > 1 else locate()
    write_provider(providers_dir)
    print(f"folder_browser: installed to {providers_dir / DOMAIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
