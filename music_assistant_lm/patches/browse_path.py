"""
Let the frontend list folders under the app's music roots.

The filesystem provider asks for its path as a typed text entry and checks
nothing until it loads. No server command lists directories at all, so the
fork frontend's folder picker has nothing to call. This script adds one
command to the providers config controller at image build time:

``config/providers/browse_path(path=None)`` returns the browsable roots
(``/music`` for the drive this app mounts, ``/media``, ``/share``), whether
each is present, and the sub-folders of ``path``. Every path is checked with
the server's own ``is_safe_path`` against those roots, so the command can
never enumerate anything else in the container. It carries the same scope
that saves a provider config.

The edit is anchored on exact upstream lines and the script fails loudly
when an anchor is gone, so a server release that reshapes the module breaks
the image build (and the sync pull request) instead of silently shipping
without the command. Running it twice is a no-op.

Usage: ``python browse_path.py [providers.py]``. Without a path the installed
module is located through ``importlib``.
"""

from __future__ import annotations

import sys
from pathlib import Path

MARKER = "# trooperthorn: browse_path"
MODULE = "music_assistant.controllers.config.providers"

EDITS: list[tuple[str, str]] = [
    (
        "import asyncio\n",
        f"import asyncio\nimport os  {MARKER}\n",
    ),
    (
        "from music_assistant_models.errors import ActionUnavailable\n",
        "from music_assistant_models.errors import ActionUnavailable\n"
        f"from music_assistant.helpers.security import is_safe_path  {MARKER}\n",
    ),
    (
        '    @api_command("config/providers/invoke_action", required_scope=Scope.CONFIG_PROVIDERS_WRITE)\n',
        f'    @api_command("config/providers/browse_path", required_scope=Scope.CONFIG_PROVIDERS_WRITE)  {MARKER}\n'
        "    async def browse_path(self, path: str | None = None) -> dict[str, Any]:\n"
        '        """\n'
        "        List the folders under one of the app's music roots, for picking a provider path.\n"
        "\n"
        "        :param path: A folder under a root; None returns the roots only.\n"
        '        """\n'
        "        roots: list[dict[str, Any]] = [\n"
        '            {"path": "/music", "label": "Music drive"},\n'
        '            {"path": "/media", "label": "Media"},\n'
        '            {"path": "/share", "label": "Share"},\n'
        "        ]\n"
        "        for root in roots:\n"
        '            root["present"] = await asyncio.to_thread(os.path.isdir, root["path"])\n'
        "        if not path:\n"
        '            return {"roots": roots, "path": None, "parent": None, "folders": []}\n'
        "        norm = os.path.normpath(path)\n"
        '        root_path = next((root["path"] for root in roots if is_safe_path(norm, root["path"])), None)\n'
        "        if root_path is None or not os.path.isabs(norm):\n"
        '            msg = f"{path} is outside the browsable folders"\n'
        "            raise ValueError(msg)\n"
        "\n"
        "        def _list() -> list[dict[str, str]]:\n"
        "            folders: list[dict[str, str]] = []\n"
        "            try:\n"
        "                with os.scandir(norm) as entries:\n"
        "                    for entry in entries:\n"
        '                        if entry.name.startswith("."):\n'
        "                            continue\n"
        "                        try:\n"
        "                            if not entry.is_dir():\n"
        "                                continue\n"
        "                        except OSError:\n"
        "                            continue\n"
        '                        folders.append({"name": entry.name, "path": os.path.join(norm, entry.name)})\n'
        "            except OSError:\n"
        "                return []\n"
        '            folders.sort(key=lambda folder: folder["name"].casefold())\n'
        "            return folders[:500]\n"
        "\n"
        "        return {\n"
        '            "roots": roots,\n'
        '            "path": norm,\n'
        '            "parent": None if norm == root_path else os.path.dirname(norm),\n'
        '            "folders": await asyncio.to_thread(_list),\n'
        "        }\n"
        "\n"
        '    @api_command("config/providers/invoke_action", required_scope=Scope.CONFIG_PROVIDERS_WRITE)\n',
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
        print(f"browse_path: already applied to {path}")
        return 0
    compile(patched, str(path), "exec")
    path.write_text(patched, encoding="utf-8", newline="\n")
    print(f"browse_path: applied to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
