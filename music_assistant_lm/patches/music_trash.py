"""
Let the frontend move a file of a Filesystem provider into a trash folder.

Nothing in the server touches files on disk: removing a track drops the
database row and the file stays. The fork frontend's Duplicates page needs
one reversible step past that, so this script adds four commands to the
music controller at image build time, all behind the scope that already
guards provider mappings:

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
path is checked with the server's own ``is_safe_path`` against that base,
so nothing outside the provider's folder can be moved or listed.

The edit is anchored on exact upstream lines and the script fails loudly
when an anchor is gone, so a server release that reshapes the module breaks
the image build (and the sync pull request) instead of silently shipping
without the commands. Running it twice is a no-op.

Usage: ``python music_trash.py [controller.py]``. Without a path the
installed module is located through ``importlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: music_trash"
MODULE = "music_assistant.controllers.music.controller"
TRASH_DIR = ".music-assistant-trash"

EDITS: list[tuple[str, str]] = [
    (
        "import asyncio\nimport logging\n",
        f"import asyncio\nimport logging\nimport os  {MARKER}\nimport shutil  {MARKER}\n",
    ),
    (
        "from music_assistant.helpers.api import api_command\n",
        "from music_assistant.helpers.api import api_command\n"
        f"from music_assistant.helpers.security import is_safe_path  {MARKER}\n",
    ),
    (
        '    @api_command("music/match_providers", required_scope=Scope.LIBRARY_MANAGE)\n',
        f'    TRASH_DIR = "{TRASH_DIR}"  {MARKER}\n'
        "\n"
        "    def _trash_base(self, provider_instance: str) -> str:\n"
        '        """The folder of a Filesystem provider; raises for any other provider."""\n'
        "        prov = self.mass.get_provider(provider_instance)\n"
        '        base = getattr(prov, "base_path", None)\n'
        "        if not isinstance(base, str) or not base:\n"
        '            msg = f"{provider_instance} is not a Filesystem provider"\n'
        "            raise InvalidDataError(msg)\n"
        "        return os.path.normpath(base)\n"
        "\n"
        "    def _trash_resolve(self, base: str, path: str) -> str:\n"
        '        """Absolute path of a file under base (path may be relative or absolute)."""\n'
        "        absolute = path if os.path.isabs(path) else os.path.join(base, path)\n"
        "        absolute = os.path.normpath(absolute)\n"
        "        if not is_safe_path(absolute, base) or absolute == base:\n"
        '            msg = f"{path} is outside the provider folder"\n'
        "            raise InvalidDataError(msg)\n"
        "        return absolute\n"
        "\n"
        "    @staticmethod\n"
        "    def _trash_move(source: str, target: str) -> str:\n"
        '        """Rename source to target, never over an existing file; returns the final target."""\n'
        "        if not os.path.isfile(source):\n"
        '            msg = f"{source} is not a file"\n'
        "            raise MediaNotFoundError(msg)\n"
        "        final = target\n"
        "        stem, ext = os.path.splitext(target)\n"
        "        counter = 1\n"
        "        while os.path.lexists(final):\n"
        '            final = f"{stem} ({counter}){ext}"\n'
        "            counter += 1\n"
        "        os.makedirs(os.path.dirname(final), exist_ok=True)\n"
        "        os.replace(source, final)\n"
        "        return final\n"
        "\n"
        '    @api_command("music/trash/move", required_scope=Scope.LIBRARY_MANAGE)\n'
        "    async def trash_move(self, provider_instance: str, path: str) -> dict[str, str]:\n"
        '        """\n'
        "        Move one file of a Filesystem provider into its trash folder (a rename).\n"
        "\n"
        "        :param provider_instance: The Filesystem provider instance id.\n"
        "        :param path: The file, relative to the provider folder or absolute inside it.\n"
        '        """\n'
        "        base = self._trash_base(provider_instance)\n"
        "        trash = os.path.join(base, self.TRASH_DIR)\n"
        "        source = self._trash_resolve(base, path)\n"
        "        if is_safe_path(source, trash):\n"
        '            msg = f"{path} is already in the trash"\n'
        "            raise InvalidDataError(msg)\n"
        "        relative = os.path.relpath(source, base)\n"
        "        target = await asyncio.to_thread(self._trash_move, source, os.path.join(trash, relative))\n"
        '        self.logger.info("Moved %s to the trash of %s", relative, provider_instance)\n'
        '        return {"path": relative, "trashed_path": os.path.relpath(target, trash)}\n'
        "\n"
        '    @api_command("music/trash/list", required_scope=Scope.LIBRARY_MANAGE)\n'
        "    async def trash_list(self, provider_instance: str) -> list[dict[str, Any]]:\n"
        '        """List the files in the trash folder of a Filesystem provider."""\n'
        "        base = self._trash_base(provider_instance)\n"
        "        trash = os.path.join(base, self.TRASH_DIR)\n"
        "\n"
        "        def _walk() -> list[dict[str, Any]]:\n"
        "            found: list[dict[str, Any]] = []\n"
        "            for root, _dirs, files in os.walk(trash):\n"
        "                for name in files:\n"
        "                    full = os.path.join(root, name)\n"
        "                    try:\n"
        "                        info = os.stat(full)\n"
        "                    except OSError:\n"
        "                        continue\n"
        "                    found.append(\n"
        "                        {\n"
        '                            "path": os.path.relpath(full, trash),\n'
        '                            "size": info.st_size,\n'
        '                            "trashed_at": info.st_mtime,\n'
        "                        }\n"
        "                    )\n"
        '            found.sort(key=lambda item: item["path"].casefold())\n'
        "            return found\n"
        "\n"
        "        return await asyncio.to_thread(_walk)\n"
        "\n"
        '    @api_command("music/trash/restore", required_scope=Scope.LIBRARY_MANAGE)\n'
        "    async def trash_restore(self, provider_instance: str, path: str) -> dict[str, str]:\n"
        '        """\n'
        "        Move one trashed file back to where it came from.\n"
        "\n"
        "        :param provider_instance: The Filesystem provider instance id.\n"
        "        :param path: The file, relative to the trash folder (as trash/list returns it).\n"
        '        """\n'
        "        base = self._trash_base(provider_instance)\n"
        "        trash = os.path.join(base, self.TRASH_DIR)\n"
        "        source = self._trash_resolve(trash, path)\n"
        "        target = os.path.join(base, os.path.relpath(source, trash))\n"
        "        if os.path.lexists(target):\n"
        '            msg = f"{target} exists; the trashed copy stays where it is"\n'
        "            raise InvalidDataError(msg)\n"
        "        await asyncio.to_thread(self._trash_move, source, target)\n"
        '        self.logger.info("Restored %s from the trash of %s", path, provider_instance)\n'
        '        return {"path": os.path.relpath(target, base), "trashed_path": path}\n'
        "\n"
        '    @api_command("music/trash/empty", required_scope=Scope.LIBRARY_MANAGE)\n'
        "    async def trash_empty(self, provider_instance: str) -> dict[str, int]:\n"
        '        """Delete everything in the trash folder of a Filesystem provider; returns the file count."""\n'
        "        base = self._trash_base(provider_instance)\n"
        "        trash = os.path.join(base, self.TRASH_DIR)\n"
        "\n"
        "        def _empty() -> int:\n"
        "            count = sum(len(files) for _root, _dirs, files in os.walk(trash))\n"
        "            shutil.rmtree(trash, ignore_errors=True)\n"
        "            return count\n"
        "\n"
        "        count = await asyncio.to_thread(_empty)\n"
        '        self.logger.info("Emptied the trash of %s (%s files)", provider_instance, count)\n'
        '        return {"deleted": count}\n'
        "\n"
        '    @api_command("music/match_providers", required_scope=Scope.LIBRARY_MANAGE)\n',
    ),
]


def locate() -> Path:
    """The installed module."""
    import importlib.util

    spec = importlib.util.find_spec(MODULE)
    if spec is None or spec.origin is None:
        raise SystemExit(f"{MODULE} is not installed")
    return Path(spec.origin)


def apply(text: str) -> str:
    """Return the patched module text; the input text when it is already patched."""
    if MARKER in text:
        return text
    for anchor, replacement in EDITS:
        count = text.count(anchor)
        if count != 1:
            raise SystemExit(f"anchor found {count} times, expected once; the module changed upstream:\n{anchor}")
        text = text.replace(anchor, replacement, 1)
    return text


def main(argv: list[str]) -> int:
    path = Path(argv[1]) if len(argv) > 1 else locate()
    original = path.read_text(encoding="utf-8")
    patched = apply(original)
    if patched == original:
        print(f"music_trash: already applied to {path}")
        return 0
    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8", newline="\n")
    print(f"music_trash: applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
