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

Supervisor builds this image on the Home Assistant host when the app is
installed or updated (there is no `image:` key), so no registry, image
signing or multi-arch publishing is needed on this side. The cost is a few
minutes of build time on the host per update and the pull of the upstream
image.

Everything else in `music_assistant_lm/` is the upstream app definition:
`config.yaml` (with this app's own `name`, `slug`, `description`, `url` and
`version`), `apparmor.txt` and `translations/en.yaml`.

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
