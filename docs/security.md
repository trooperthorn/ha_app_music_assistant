# Security

## Privileges

Identical to the official Music Assistant app, because `config.yaml` is
synced from it: `host_network`, `SYS_ADMIN` and `DAC_READ_SEARCH`
capabilities, audio, `media:rw`, `ssl:ro`, the Home Assistant and auth
APIs, ingress on 8094, the upstream AppArmor profile. This repository adds
no privilege.

## What is verified

- Both base images are pinned by digest as well as version tag
  (`ghcr.io/music-assistant/server:<version>@sha256:...`, and the same for
  `ghcr.io/astral-sh/uv`), so the bytes are fixed rather than merely named.
  A tag is mutable: the same version can be re-published and every rebuild
  would then ship different content with nothing to show for it. The pins
  are multi-arch index digests, so amd64 and aarch64 resolve from one value,
  and `sync_upstream.py` re-resolves them on every run — an upstream re-push
  surfaces as a digest change in the sync PR instead of a silent swap.
- The fork wheel is downloaded from a GitHub release of
  `trooperthorn/HA_int_MA-UI` and checked against the SHA-256 pinned in the
  Dockerfile before `uv pip install`. The pin is written by the sync script
  from the release's `SHA256SUMS`.
- Every workflow runs with `contents: read` unless a job needs more, and
  every action is pinned to a commit SHA.

## What is not

- Vulnerabilities inside the upstream image (Debian packages, Python
  dependencies, the server itself). `security.yml` reports them; fixing
  them is upstream's release, which this repository follows automatically.
- The provenance of what the upstream server itself depends on. Released
  server tags `2.10.2` and `2.10.3` both carry
  `aiolibdatachannel @ git+https://github.com/music-assistant/aiolibdatachannel@feat/certpem-testdrive`
  — a dependency on a mutable branch rather than a tag or commit, marked
  "TEMPORARY" in upstream's own comment. It is the WebRTC peer behind Remote
  Access, and the frontend pins its DTLS certificate fingerprint. Nothing
  here can fix that; the digest pin above is the mitigation, because it
  fixes which build of it this app ships and makes any change visible.
- The frontend fork's own supply chain (its `pnpm` lockfile and build); see
  that repository's workflows.

## Data written to the app's `/data`

The server generates a persistent WebRTC DTLS keypair
(`webrtc_certificate.pem`, `webrtc_private_key.pem`, ten-year validity,
unencrypted PKCS8) on **every** start, whether or not Remote Access is
enabled, because the Remote ID is derived from it. The server writes the key
`0600`, but file mode is not a boundary inside a backup archive, so
`webrtc_private_key.pem` is listed in `backup_exclude`. If Remote Access is
ever enabled on an instance whose backups have been shared, delete both PEMs
first so a fresh identity is minted.
