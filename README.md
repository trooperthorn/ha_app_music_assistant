# Music Assistant (Library Manager) for Home Assistant

A Home Assistant app repository with one app: the upstream
[Music Assistant server](https://github.com/music-assistant/server) with the
[trooperthorn library-manager frontend](https://github.com/trooperthorn/HA_int_MA-UI)
installed over the stock one. It tracks the upstream server release, the
fork's frontend release and the upstream app definition automatically.

[![Open your Home Assistant instance and show the Add App repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Ftrooperthorn%2Fha_app_music_assistant)

Or add `https://github.com/trooperthorn/ha_app_music_assistant` under
Settings, Apps, App store, Repositories.

## What you get

- The current stable Music Assistant server, unchanged.
- The fork frontend: a desktop library manager at `/library` (source tree,
  genre / artist / album browser, dense sortable track grid, queue and
  selected-item panes, keyboard commands, settings tree). Guide:
  [LIBRARY-MANAGER.md](https://github.com/trooperthorn/HA_int_MA-UI/blob/main/docs/LIBRARY-MANAGER.md).
- The same options, ports, ingress, discovery and AppArmor profile as the
  official app.

Install it instead of the official Music Assistant app: both bind port 8095
on the host network and announce the same discovery service.

## How it stays current

| Input | Source | Followed by |
| --- | --- | --- |
| Server | latest stable release of `music-assistant/server` | `SERVER_VERSION` in the Dockerfile |
| Frontend | latest release of `trooperthorn/HA_int_MA-UI` (wheel + SHA256SUMS) | `FRONTEND_*` in the Dockerfile |
| App definition | `music_assistant/` in `music-assistant/home-assistant-addon` | `config.yaml`, `apparmor.txt`, `translations/` |

`sync-upstream.yml` runs daily, applies `scripts/sync_upstream.py`, and opens
an auto-merging pull request when anything moved. A merge to `main` releases
(`release.yml`), and `prepare-release.yml` bumps the CalVer app version so
Home Assistant offers the update. Details in [docs/operations.md](docs/operations.md).

## Repository layout

- `music_assistant_lm/`: the app (config, Dockerfile, AppArmor, translations, changelog).
- `scripts/`: the upstream sync and the shared release scripts.
- `docs/`: design, operations, security, decisions.
