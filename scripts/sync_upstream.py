#!/usr/bin/env python3
"""Track the two upstreams this app is built from.

Reads the latest stable release of music-assistant/server, the latest
release of trooperthorn/HA_int_MA-UI (the fork frontend wheel and its
sha256) and the upstream app definition in
music-assistant/home-assistant-addon, then rewrites:

- music_assistant_lm/Dockerfile: SERVER_VERSION, FRONTEND_RELEASE,
  FRONTEND_WHEEL, FRONTEND_SHA256
- music_assistant_lm/config.yaml: every key from the upstream config except
  the ones this app owns (name, version, slug, description, url) and the
  registry image key
- music_assistant_lm/apparmor.txt and translations/en.yaml: verbatim
- music_assistant_lm/CHANGELOG.md: one entry per change

The app version itself is not touched; the release automation bumps it
once the change is merged. Prints "changed=true" or "changed=false" (also
into GITHUB_OUTPUT when set).

Usage:
    python scripts/sync_upstream.py            # apply
    python scripts/sync_upstream.py --check    # report only, no writes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

import yaml

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
SERVER_REPO = "music-assistant/server"
FRONTEND_REPO = "trooperthorn/HA_int_MA-UI"
ADDON_REPO = "music-assistant/home-assistant-addon"
ADDON_DIR = "music_assistant"
APP_DIR = "music_assistant_lm"

# config.yaml keys this app owns; the rest follow upstream
OWN_KEYS = ("name", "version", "slug", "description", "url")
# upstream keys that must not be copied (the image is built locally)
DROP_KEYS = ("image",)

STABLE_TAG = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
FORK_TAG = re.compile(r"^v[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$")
WHEEL = re.compile(r"^music_assistant_frontend-.*\.whl$")


def fetch(url: str, *, binary: bool = False) -> bytes | str:
    request = urllib.request.Request(url, headers={"User-Agent": "ha_app_music_assistant sync"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(API):
        request.add_header("Authorization", f"Bearer {token}")
    # fixed https hosts only
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        data = response.read()
    return data if binary else data.decode("utf-8")


def latest_release(repo: str, tag_ok) -> dict:
    releases = json.loads(fetch(f"{API}/repos/{repo}/releases?per_page=30"))
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        if tag_ok(release["tag_name"]):
            return release
    raise RuntimeError(f"no stable release found on {repo}")


def frontend_pin(release: dict) -> dict[str, str]:
    wheels = [asset for asset in release["assets"] if WHEEL.match(asset["name"])]
    if len(wheels) != 1:
        raise RuntimeError(f"{FRONTEND_REPO} {release['tag_name']} carries {len(wheels)} wheels")
    wheel = wheels[0]
    sums = next(
        (asset for asset in release["assets"] if asset["name"] == "SHA256SUMS"),
        None,
    )
    digest = ""
    if sums:
        for line in str(fetch(sums["browser_download_url"])).splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == wheel["name"]:
                digest = parts[0]
    if not digest:
        data = fetch(wheel["browser_download_url"], binary=True)
        digest = hashlib.sha256(data).hexdigest()
    return {
        "FRONTEND_RELEASE": release["tag_name"],
        "FRONTEND_WHEEL": wheel["name"],
        "FRONTEND_SHA256": digest,
    }


def set_args(dockerfile: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        dockerfile, count = re.subn(rf"^ARG {key}=.*$", f'ARG {key}="{value}"', dockerfile, flags=re.MULTILINE)
        if count != 1:
            raise RuntimeError(f"Dockerfile has {count} ARG {key} lines, expected one")
    return dockerfile


def read_args(dockerfile: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in re.finditer(r'^ARG (\w+)="([^"]*)"', dockerfile, flags=re.MULTILINE)}


def _quote_version(body: str) -> str:
    def repl(match: re.Match[str]) -> str:
        value = match.group(1).strip("'\"")
        return f'version: "{value}"'

    return re.sub(r"^version: (\S+)$", repl, body, flags=re.MULTILINE)


def merge_config(ours: str, upstream: str) -> str:
    """Upstream keys in upstream order, with this app's own keys kept first."""
    mine = yaml.safe_load(ours)
    theirs = yaml.safe_load(upstream)
    merged: dict = {key: mine[key] for key in OWN_KEYS if key in mine}
    for key, value in theirs.items():
        if key in OWN_KEYS or key in DROP_KEYS:
            continue
        merged[key] = value
    header = "\n".join(line for line in ours.splitlines() if line.startswith("#"))
    body = yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=100)
    # the version stays a quoted string for the release scripts
    body = _quote_version(body)
    return (header + "\n" if header else "") + body


def changelog_entry(lines: list[str]) -> str:
    items = "".join(f"- {line}\n" for line in lines)
    return f"## {date.today():%Y-%m-%d}\n\n{items}\n"


def prepend_changelog(text: str, lines: list[str]) -> str:
    """Add the lines under today's heading, merging into it when it already leads."""
    head, sep, rest = text.partition("\n\n")
    heading = f"## {date.today():%Y-%m-%d}\n\n"
    if rest.startswith(heading):
        items = "".join(f"- {line}\n" for line in lines)
        return head + sep + heading + items + rest[len(heading) :]
    return head + sep + changelog_entry(lines) + rest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="report without writing")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    app = args.repository / APP_DIR
    changes: list[str] = []
    writes: dict[Path, str] = {}

    dockerfile_path = app / "Dockerfile"
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    current = read_args(dockerfile)
    wanted: dict[str, str] = {}

    server = latest_release(SERVER_REPO, STABLE_TAG.match)
    if current.get("SERVER_VERSION") != server["tag_name"]:
        wanted["SERVER_VERSION"] = server["tag_name"]
        changes.append(f"Music Assistant server {current.get('SERVER_VERSION')} -> {server['tag_name']}")

    try:
        frontend = latest_release(FRONTEND_REPO, FORK_TAG.match)
    except RuntimeError as err:
        print(f"note: {err}; the fork frontend pin is left as is", file=sys.stderr)
        frontend = None
    if frontend:
        pin = frontend_pin(frontend)
        if any(current.get(key) != value for key, value in pin.items()):
            wanted.update(pin)
            previous = current.get("FRONTEND_RELEASE") or "none"
            changes.append(f"Fork frontend {previous} -> {pin['FRONTEND_RELEASE']}")
    if wanted:
        writes[dockerfile_path] = set_args(dockerfile, wanted)

    for name in ("config.yaml", "apparmor.txt", "translations/en.yaml"):
        upstream = str(fetch(f"{RAW}/{ADDON_REPO}/main/{ADDON_DIR}/{name}"))
        target = app / name
        ours = target.read_text(encoding="utf-8")
        new = merge_config(ours, upstream) if name == "config.yaml" else upstream
        if new != ours:
            writes[target] = new
            changes.append(f"Upstream app {name} refreshed")

    changed = bool(changes)
    if changed and not args.check:
        for path, text in writes.items():
            path.write_text(text, encoding="utf-8", newline="\n")
        changelog = app / "CHANGELOG.md"
        changelog.write_text(
            prepend_changelog(changelog.read_text(encoding="utf-8"), changes),
            encoding="utf-8",
            newline="\n",
        )
    for line in changes:
        print(f"change: {line}")
    flag = "true" if changed else "false"
    print(f"changed={flag}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"changed={flag}\n")
            fh.write("summary<<SUMMARY_END\n" + "\n".join(changes) + "\nSUMMARY_END\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
