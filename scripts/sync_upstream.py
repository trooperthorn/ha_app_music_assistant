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
REGISTRY = "https://ghcr.io"
SERVER_REPO = "music-assistant/server"
# registry paths, which happen to match the GitHub repo for the server but are
# looked up on ghcr.io rather than the API and need not stay identical
SERVER_IMAGE = "music-assistant/server"
UV_IMAGE = "astral-sh/uv"
FRONTEND_REPO = "trooperthorn/HA_int_MA-UI"
ADDON_REPO = "music-assistant/home-assistant-addon"
ADDON_DIR = "music_assistant"
APP_DIR = "music_assistant_lm"

# config.yaml keys this app owns; the rest follow upstream
OWN_KEYS = ("name", "version", "slug", "description", "url")
# upstream keys that must not be copied (the image is built locally)
DROP_KEYS = ("image",)
# Entries this app appends to an upstream list, instead of owning the key.
# backup_exclude is upstream's to grow, but the WebRTC DTLS private key is
# written into the data directory whether or not remote access is enabled
# (the server derives the Remote ID from it at startup either way) and an
# unencrypted ten-year key does not belong in a Home Assistant backup.
EXTRA_LIST_ITEMS: dict[str, tuple[str, ...]] = {
    "backup_exclude": ("webrtc_private_key.pem",),
    # the backup task writes under /share
    "map": ("share:rw",),
}
# Entries this app adds to an upstream mapping (options and their schema), so
# upstream's own additions keep arriving while ours stay: the music drive the
# entrypoint wrapper mounts and its one-off tasks (rootfs/usr/local/bin).
EXTRA_DICT_ITEMS: dict[str, dict[str, object]] = {
    "options": {"music_drive_task": "none"},
    "schema": {
        "music_drive": "device(subsystem=block)?",
        "music_drive_task": "list(none|backup|verify|restore|repair)",
    },
}
# Keys this app sets when upstream has no opinion; upstream's value wins when
# it appears. udev lets the wrapper read the drive's label and type, and
# kernel_modules loads its filesystem driver by name.
EXTRA_KEYS: dict[str, object] = {"udev": True, "kernel_modules": True}

# Accept header that asks a registry for the multi-arch index rather than one
# platform's manifest, so the digest pinned below covers amd64 and aarch64.
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)

STABLE_TAG = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
FORK_TAG = re.compile(r"^v[0-9]{4}\.[0-9]{2}\.[0-9]{2}\.[0-9]+$")
WHEEL = re.compile(r"^music_assistant_frontend-.*\.whl$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# the server's own pin of the package this app replaces
FRONTEND_PIN = re.compile(r'"music-assistant-frontend==([0-9][^"]*)"')


def fetch(url: str, *, binary: bool = False) -> bytes | str:
    request = urllib.request.Request(url, headers={"User-Agent": "ha_app_music_assistant sync"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(API):
        request.add_header("Authorization", f"Bearer {token}")
    # fixed https hosts only
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        data = response.read()
    return data if binary else data.decode("utf-8")


def image_digest(image: str, tag: str) -> str:
    """
    Resolve an image tag to the digest of its multi-arch index.

    A tag is mutable: the same `server:2.10.3` can be re-pushed and every
    rebuild then silently ships different bytes. Pinning `tag@sha256:...` in
    the Dockerfile fixes the bytes and turns an upstream re-push into a
    visible digest change in the next sync PR.

    :param image: Registry path, e.g. "music-assistant/server".
    :param tag: Tag to resolve.
    :return: The "sha256:..." digest of the manifest the tag points at.
    """
    token_url = f"{REGISTRY}/token?service=ghcr.io&scope=repository:{image}:pull"
    # fixed https hosts only
    with urllib.request.urlopen(token_url, timeout=60) as response:  # noqa: S310
        token = json.load(response)["token"]
    request = urllib.request.Request(
        f"{REGISTRY}/v2/{image}/manifests/{tag}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": MANIFEST_ACCEPT,
            "User-Agent": "ha_app_music_assistant sync",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        digest = response.headers.get("Docker-Content-Digest", "")
    if not DIGEST.match(digest):
        raise RuntimeError(f"{image}:{tag} returned no usable digest ({digest!r})")
    return digest


def server_frontend_pin(version: str) -> str:
    """
    Read the frontend version the server release pins for itself.

    The fork wheel is force-installed over `music-assistant-frontend` with
    --no-deps, so whatever the server declared is gone by the time the image
    runs and nothing would otherwise notice the two drifting apart. The fork
    uses CalVer, so its own version says nothing about which upstream frontend
    it was built from; recording the server's expectation is what makes the
    pair visible in the build log and in a sync diff.

    :param version: Server release tag, e.g. "2.10.3".
    :return: The pinned frontend version, e.g. "2.17.297", or "" if absent.
    """
    pyproject = str(fetch(f"{RAW}/{SERVER_REPO}/{version}/pyproject.toml"))
    found = FRONTEND_PIN.search(pyproject)
    return found.group(1) if found else ""


def latest_release(repo: str, tag_ok) -> dict:
    releases = json.loads(fetch(f"{API}/repos/{repo}/releases?per_page=30"))
    stable = [
        release
        for release in releases
        if not release.get("draft")
        and not release.get("prerelease")
        and tag_ok(release["tag_name"])
    ]
    if stable:
        # GitHub orders releases by creation time, not version or publication
        # time. A release drafted early and published later can otherwise be
        # hidden below an older tag (for example .11 below .9).
        return max(
            stable,
            key=lambda release: tuple(int(part) for part in re.findall(r"\d+", release["tag_name"])),
        )
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
        extra = EXTRA_LIST_ITEMS.get(key)
        if extra and isinstance(value, list):
            # append rather than own the key, so upstream's own additions to
            # the list keep arriving with the next sync
            value = list(value) + [item for item in extra if item not in value]
        extra_items = EXTRA_DICT_ITEMS.get(key)
        if extra_items and isinstance(value, dict):
            value = {**value, **{k: v for k, v in extra_items.items() if k not in value}}
        merged[key] = value
    for key, extra in EXTRA_LIST_ITEMS.items():
        # upstream dropped the key entirely; this app's entries still apply
        if key not in merged:
            merged[key] = list(extra)
    for key, extra_items in EXTRA_DICT_ITEMS.items():
        if key not in merged:
            merged[key] = dict(extra_items)
    for key, default in EXTRA_KEYS.items():
        merged.setdefault(key, default)
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

    # Digests are resolved every run, not only when the version moves: a tag
    # re-pushed over the same version is exactly the case a digest pin exists
    # to catch, and it has to surface as a change of its own.
    for arg, image, version, version_arg in (
        ("SERVER_DIGEST", SERVER_IMAGE, wanted.get("SERVER_VERSION") or current.get("SERVER_VERSION", ""), "SERVER_VERSION"),
        ("UV_DIGEST", UV_IMAGE, current.get("UV_VERSION", ""), "UV_VERSION"),
    ):
        if not version:
            raise RuntimeError(f"Dockerfile has no {version_arg} to resolve {arg} against")
        digest = image_digest(image, version)
        if current.get(arg) == digest:
            continue
        wanted[arg] = digest
        if version_arg in wanted:
            # the version moved this run, and its changelog line already says so
            continue
        previous = current.get(arg) or "unpinned"
        changes.append(f"{image}:{version} re-published: digest {previous} -> {digest}")

    # What the server expects of the package this app replaces. Recorded, not
    # enforced: the fork is CalVer and does not say which upstream frontend it
    # was built from, so this cannot prove compatibility -- it makes the pair
    # visible in the build log and moves in a sync diff when the server's
    # expectation changes, which is the point at which the fork needs a rebase.
    server_version = wanted.get("SERVER_VERSION") or current.get("SERVER_VERSION", "")
    expects = server_frontend_pin(server_version)
    if expects and current.get("SERVER_EXPECTS_FRONTEND") != expects:
        wanted["SERVER_EXPECTS_FRONTEND"] = expects
        previous = current.get("SERVER_EXPECTS_FRONTEND") or "unrecorded"
        changes.append(
            f"Server {server_version} expects frontend {previous} -> {expects}; "
            "rebase the fork on that upstream release if the UI misbehaves"
        )

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
