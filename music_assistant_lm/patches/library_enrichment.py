"""Install the separately testable Library Enrichment plugin sources."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

DOMAIN = "library_enrichment"


def write_provider(providers_dir: Path, source: Path | None = None) -> None:
    source = source or Path(__file__).resolve().parents[1] / "providers" / DOMAIN
    files = {path.name: path.read_text(encoding="utf-8") for path in source.iterdir() if path.suffix in (".py", ".json")}
    if not {"__init__.py", "store.py", "spotify.py", "manifest.json"} <= files.keys():
        raise ValueError("Incomplete Library Enrichment source bundle")
    for name, content in files.items():
        if name.endswith(".py"):
            compile(content, name, "exec")
        else:
            json.loads(content)
    destination = providers_dir / DOMAIN
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (destination / name).write_text(content, encoding="utf-8")


def main() -> None:
    if len(sys.argv) > 1:
        providers_dir = Path(sys.argv[1])
    else:
        spec = importlib.util.find_spec("music_assistant.providers")
        if spec is None or not spec.submodule_search_locations:
            raise SystemExit("music_assistant.providers is not installed")
        providers_dir = Path(next(iter(spec.submodule_search_locations)))
    write_provider(providers_dir)


if __name__ == "__main__":
    main()
