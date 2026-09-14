# Design

## The problem

The Music Assistant server installs its frontend as the PyPI package
`music-assistant-frontend`, pinned to an exact version in the server's
`pyproject.toml`, and serves it from that package's install directory
(`music_assistant_frontend.where()`). A frontend fork therefore cannot reach
a running Music Assistant by any route except replacing that package inside
the server's environment. The official app pulls a prebuilt registry image,
so there is no hook to do that either.

## The shape

One app, `music_assistant_lm`, whose Dockerfile is:

1. `FROM ghcr.io/music-assistant/server:<SERVER_VERSION>`, the exact image the
   official app runs.
2. `uv` copied in (the final server image ships a venv without pip).
3. The fork's wheel downloaded from a GitHub release of
   `trooperthorn/HA_int_MA-UI`, checked against a pinned SHA-256, and
   installed with `--no-deps --force-reinstall` over the upstream package.
4. The server's own entrypoint, unchanged.

The build also records `SERVER_EXPECTS_FRONTEND`, the frontend version the
server release pins for itself, and prints it beside the installed fork
version. Because the install uses `--no-deps`, that expectation is otherwise
invisible once the image is built; see decisions.md for why it is recorded
rather than enforced.

Supervisor builds this image on the Home Assistant host when the app is
installed or updated (there is no `image:` key), so no registry, image
signing or multi-arch publishing is needed on this side. The cost is a few
minutes of build time on the host per update and the pull of the upstream
image.

Everything else in `music_assistant_lm/` is the upstream app definition:
`config.yaml` (with this app's own `name`, `slug`, `description`, `url` and
`version`), `apparmor.txt` and `translations/en.yaml`.

## Build-time edits of the server

`music_assistant_lm/patches/` holds small Python scripts that edit the
installed server in place while the image builds, one script per change,
each documenting what it changes and why. The Dockerfile runs every script
after the frontend wheel is installed. A script is a list of exact upstream
lines and their replacements; it refuses to run when an anchor is missing or
ambiguous, so a server release that reshapes the edited module fails the
image build and the sync pull request instead of shipping without the change.
Each script is a no-op on a module it already edited.

Today there is one:

- `hass_source_select.py`: the Home Assistant player provider mirrors the
  wrapped entity's `source_list` as selectable player sources (which gives
  the player `select_source` on the server side), mirrors its `source` as
  `extra_attributes.hass_source`, and implements `select_source` as
  `media_player.select_source`. The fork frontend's routing view (`/flow`)
  switches receiver and amplifier zones to the Chromecast feed input with it.

`tests/test_patches.py` applies each script to a copy of the upstream module
kept under `tests/fixtures/` and pins the copy to the server version in the
Dockerfile, so a server bump is the moment the fixture and the anchors are
re-checked.

## Two upstreams and one fork

`scripts/sync_upstream.py` reads three things and writes the pins:

- `music-assistant/server`: latest non-prerelease `X.Y.Z` release tag.
- `trooperthorn/HA_int_MA-UI`: latest `vYYYY.MM.DD.N` release, its wheel and
  its `SHA256SUMS` line (the digest is computed from the wheel if the sums
  file is absent).
- `music-assistant/home-assistant-addon` `music_assistant/`: config,
  AppArmor and translations, merged so this app's own keys win.

The fork publishes that wheel with its `publish-fork.yml` workflow on every
merge to its `main`.

## Versioning

The app version is CalVer `YYYY.MM.DD.N` in `config.yaml`, written only by
`scripts/set_version.py` and validated by `build_release_artifacts.py`
(the shared release scripts). The upstream server version and the frontend
release are Dockerfile arguments and appear in the app changelog, not in the
app version.
