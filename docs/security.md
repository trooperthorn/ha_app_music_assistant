# Security

## Privileges

Identical to the official Music Assistant app, because `config.yaml` is
synced from it: `host_network`, `SYS_ADMIN` and `DAC_READ_SEARCH`
capabilities, audio, `media:rw`, `ssl:ro`, the Home Assistant and auth
APIs, ingress on 8094, the upstream AppArmor profile. This repository adds
no privilege.

## What is verified

- The upstream server image is pulled by an exact version tag from
  `ghcr.io/music-assistant/server`.
- The fork wheel is downloaded from a GitHub release of
  `trooperthorn/HA_int_MA-UI` and checked against the SHA-256 pinned in the
  Dockerfile before `uv pip install`. The pin is written by the sync script
  from the release's `SHA256SUMS`.
- `uv` is copied from a pinned tag of `ghcr.io/astral-sh/uv`.
- Every workflow runs with `contents: read` unless a job needs more, and
  every action is pinned to a commit SHA.

## What is not

- Vulnerabilities inside the upstream image (Debian packages, Python
  dependencies, the server itself). `security.yml` reports them; fixing
  them is upstream's release, which this repository follows automatically.
- The frontend fork's own supply chain (its `pnpm` lockfile and build); see
  that repository's workflows.
