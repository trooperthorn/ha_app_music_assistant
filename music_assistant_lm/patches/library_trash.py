"""
Add a plugin that lets the frontend move a Filesystem provider's files into a trash folder.

Nothing in the server touches files on disk: removing a track drops the
database row and the file stays. The fork frontend's Duplicates page needs
one reversible step past that. This used to be an anchor patch
(`music_trash.py`, string-replacing exact lines in the installed
`music_assistant.controllers.music.controller` module) but the code it
injects has zero dependency on that controller: it never reads anything of
`MusicController`'s beyond `self.logger` and `self.mass.get_provider`, both
of which exist on the `Provider` base class every plugin already subclasses.
Anchoring it to a file we don't own bought nothing and cost fragility -- any
upstream reshape of that module would break the build. This plugin ports it
verbatim into a self-contained provider instead, following the same shape
`folder_browser.py` was converted to (see `docs/decisions.md`).

- ``music/trash/move(provider_instance, path)`` renames the file into
  ``.music-assistant-trash/`` at the root of that provider's folder, keeping
  its relative path. Same filesystem, so it is a rename: instant, no copy,
  and the sync skips the dot folder.
- ``music/trash/list(provider_instance)`` lists what the folder holds.
- ``music/trash/restore(provider_instance, path)`` renames one back; it
  refuses when the original path is taken again.
- ``music/trash/empty(provider_instance)`` deletes the folder's contents.
  This is the only command that destroys anything.

The provider must be a Filesystem provider (it has a ``base_path``); every
path is checked with the server's own ``is_safe_path`` against that base, so
nothing outside the provider's folder can be moved or listed.

Music Assistant discovers providers by listing directories under its
providers path at runtime (`os.listdir(PROVIDERS_PATH)` in
`music_assistant/mass.py`), not through a central registry file. Adding a
new provider directory is therefore purely additive: it cannot conflict with
any future upstream diff to an existing file, unlike an anchor-based edit.
This script has no anchors to fail on; it always (re)writes the same three
files (`manifest.json`, `strings.json` and `__init__.py`).

The command names (`music/trash/move`, `music/trash/list`,
`music/trash/restore`, `music/trash/empty`), their required scope
(`Scope.LIBRARY_MANAGE`, the same scope that already guards provider
mappings) and the return shapes are a contract with the fork frontend's
Duplicates page and are carried over unchanged. `mass.register_api_command`
accepts any command string -- there is no namespace ownership -- so
registering the same `music/trash/...` names from a plugin needs no
frontend change at all. It does raise `RuntimeError` if the command is
already registered, which is why the old anchor patch and this plugin must
never both ship (see the Dockerfile and CI wiring).

Usage: ``python library_trash.py [path/to/music_assistant/providers]``.
Without a path the installed providers package is located through
``importlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

DOMAIN = "library_trash"

MANIFEST_JSON = """\
{
  "type": "plugin",
  "stage": "stable",
  "domain": "library_trash",
  "name": "Library Trash",
  "description": "Move a Filesystem provider's files to a reversible trash folder, for the Duplicates page.",
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
    "description": "Move a Filesystem provider's files to a reversible trash folder, for the Duplicates page."
  }
}
"""

INIT_PY = '''\
"""
Move a Filesystem provider's files into a reversible trash folder.

Added by trooperthorn/ha_app_music_assistant (see
music_assistant_lm/patches/library_trash.py for why this is a plugin rather
than the anchor patch it replaces).

Ported essentially verbatim from the retired music_trash.py anchor patch:
the handlers never touched `MusicController` internals either, only
`self.logger` and `self.mass.get_provider`, both of which exist on the
`Provider` base class, so moving them into a plugin provider changed
nothing about what they do, only how they are delivered.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import TYPE_CHECKING, Any

from music_assistant_models.auth import Scope
from music_assistant_models.errors import InvalidDataError, MediaNotFoundError

from music_assistant.helpers.security import is_safe_path
from music_assistant.models.plugin import PluginProvider

if TYPE_CHECKING:
    from collections.abc import Callable

    from music_assistant.mass import MusicAssistant
    from music_assistant.models import ProviderInstanceType
    from music_assistant_models.config_entries import ProviderConfig
    from music_assistant_models.provider import ProviderManifest

TRASH_DIR = ".music-assistant-trash"


async def setup(
    mass: MusicAssistant, manifest: ProviderManifest, config: ProviderConfig
) -> ProviderInstanceType:
    """Initialize provider(instance) with given configuration."""
    return LibraryTrashProvider(mass, manifest, config)


class LibraryTrashProvider(PluginProvider):
    """Gives a Filesystem provider's files a reversible trash folder for the Duplicates page."""

    TRASH_DIR = TRASH_DIR

    _unregister_handles: list[Callable[[], None]]

    async def loaded_in_mass(self) -> None:
        """Register API commands after the provider is loaded."""
        self._unregister_handles = [
            self.mass.register_api_command(
                "music/trash/move", self.trash_move, required_scope=Scope.LIBRARY_MANAGE
            ),
            self.mass.register_api_command(
                "music/trash/list", self.trash_list, required_scope=Scope.LIBRARY_MANAGE
            ),
            self.mass.register_api_command(
                "music/trash/restore", self.trash_restore, required_scope=Scope.LIBRARY_MANAGE
            ),
            self.mass.register_api_command(
                "music/trash/empty", self.trash_empty, required_scope=Scope.LIBRARY_MANAGE
            ),
        ]

    async def unload(self, is_removed: bool = False) -> None:
        """Unregister API commands when the provider is unloaded."""
        for unregister in getattr(self, "_unregister_handles", []):
            unregister()

    def _trash_base(self, provider_instance: str) -> str:
        """The folder of a Filesystem provider; raises for any other provider."""
        prov = self.mass.get_provider(provider_instance)
        base = getattr(prov, "base_path", None)
        if not isinstance(base, str) or not base:
            msg = f"{provider_instance} is not a Filesystem provider"
            raise InvalidDataError(msg)
        return os.path.normpath(base)

    def _trash_resolve(self, base: str, path: str) -> str:
        """Absolute path of a file under base (path may be relative or absolute)."""
        absolute = path if os.path.isabs(path) else os.path.join(base, path)
        absolute = os.path.normpath(absolute)
        if not is_safe_path(absolute, base) or absolute == base:
            msg = f"{path} is outside the provider folder"
            raise InvalidDataError(msg)
        return absolute

    @staticmethod
    def _trash_move(source: str, target: str) -> str:
        """Rename source to target, never over an existing file; returns the final target."""
        if not os.path.isfile(source):
            msg = f"{source} is not a file"
            raise MediaNotFoundError(msg)
        final = target
        stem, ext = os.path.splitext(target)
        counter = 1
        while os.path.lexists(final):
            final = f"{stem} ({counter}){ext}"
            counter += 1
        os.makedirs(os.path.dirname(final), exist_ok=True)
        os.replace(source, final)
        return final

    async def trash_move(self, provider_instance: str, path: str) -> dict[str, str]:
        """
        Move one file of a Filesystem provider into its trash folder (a rename).

        :param provider_instance: The Filesystem provider instance id.
        :param path: The file, relative to the provider folder or absolute inside it.
        """
        base = self._trash_base(provider_instance)
        trash = os.path.join(base, self.TRASH_DIR)
        source = self._trash_resolve(base, path)
        if is_safe_path(source, trash):
            msg = f"{path} is already in the trash"
            raise InvalidDataError(msg)
        relative = os.path.relpath(source, base)
        target = await asyncio.to_thread(self._trash_move, source, os.path.join(trash, relative))
        self.logger.info("Moved %s to the trash of %s", relative, provider_instance)
        return {"path": relative, "trashed_path": os.path.relpath(target, trash)}

    async def trash_list(self, provider_instance: str) -> list[dict[str, Any]]:
        """List the files in the trash folder of a Filesystem provider."""
        base = self._trash_base(provider_instance)
        trash = os.path.join(base, self.TRASH_DIR)

        def _walk() -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            for root, _dirs, files in os.walk(trash):
                for name in files:
                    full = os.path.join(root, name)
                    try:
                        info = os.stat(full)
                    except OSError:
                        continue
                    found.append(
                        {
                            "path": os.path.relpath(full, trash),
                            "size": info.st_size,
                            "trashed_at": info.st_mtime,
                        }
                    )
            found.sort(key=lambda item: item["path"].casefold())
            return found

        return await asyncio.to_thread(_walk)

    async def trash_restore(self, provider_instance: str, path: str) -> dict[str, str]:
        """
        Move one trashed file back to where it came from.

        :param provider_instance: The Filesystem provider instance id.
        :param path: The file, relative to the trash folder (as trash/list returns it).
        """
        base = self._trash_base(provider_instance)
        trash = os.path.join(base, self.TRASH_DIR)
        source = self._trash_resolve(trash, path)
        target = os.path.join(base, os.path.relpath(source, trash))
        if os.path.lexists(target):
            msg = f"{target} exists; the trashed copy stays where it is"
            raise InvalidDataError(msg)
        await asyncio.to_thread(self._trash_move, source, target)
        self.logger.info("Restored %s from the trash of %s", path, provider_instance)
        return {"path": os.path.relpath(target, base), "trashed_path": path}

    async def trash_empty(self, provider_instance: str) -> dict[str, int]:
        """Delete everything in the trash folder of a Filesystem provider; returns the file count."""
        base = self._trash_base(provider_instance)
        trash = os.path.join(base, self.TRASH_DIR)

        def _empty() -> int:
            count = sum(len(files) for _root, _dirs, files in os.walk(trash))
            shutil.rmtree(trash, ignore_errors=True)
            return count

        count = await asyncio.to_thread(_empty)
        self.logger.info("Emptied the trash of %s (%s files)", provider_instance, count)
        return {"deleted": count}
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
    print(f"library_trash: installed to {providers_dir / DOMAIN}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
