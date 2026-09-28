# Security

## Privileges

Most keys are synced from the official Music Assistant app's `config.yaml`,
so `host_network`, `homeassistant_api`, `auth_api`, the `SYS_ADMIN` and
`DAC_READ_SEARCH` capabilities, audio, `media:rw`, `ssl:ro` and ingress on
8094 are inherited from upstream rather than chosen here. This repository
adds `share:rw` (the drive backup task writes there), `udev` (read-only
access to the host's device database, for the drive's label and type) and
an optional `music_drive` device option through which Supervisor grants one
partition to the container. Protection mode stays on; `full_access` is not
used. See "The music drive" in design.md for what the wrapper does and does
not do with the drive, in particular that it never writes without an
explicit one-off task.

`kernel_modules` was dropped. Nothing in the image calls `modprobe` or
`insmod` (the wrapper runs only `blkid`, `fsck.exfat`, `mount`, `umount`
and `rsync`), and a `mount -t exfat|ntfs3|...` of a filesystem type the
host has not loaded yet is resolved by the host kernel's own module
request, not by the container, so the module mapping and `SYS_MODULE`
bought nothing.

### AppArmor

`apparmor.txt` is this app's own profile, `music_assistant_lm`, and
`sync_upstream.py` no longer overwrites it. It replaces upstream's blanket
`file`, `mount` and `/dev/* mrwkl` rules with explicit paths: read and
execute on the image (`/usr`, `/bin`, `/lib`, `/app`, ...), read-write on
`/data`, `/media`, `/share`, `/music` and scratch space, read-only on
`/ssl`, the audio nodes under `/dev/snd`, and the block device nodes a
granted partition can appear as (`/dev/sd*`, `/dev/nvme*n*p*`,
`/dev/mmcblk*p*`). Mounting is scoped to local filesystem types onto
`/music/**` (the drive) and `cifs`/`nfs` onto `/tmp/**` (upstream's network
share providers). If the server logs a permission error after an update,
add `complain` to the profile flags, reproduce, and read
`journalctl _TRANSPORT="audit" -g 'apparmor="ALLOWED"'` for the missing rule.

### Rating

Supervisor starts every app at 5 (docs/apps/security.md):

| Factor | Change | Running |
| --- | --- | --- |
| Base | | 5 |
| `ingress: true` (supersedes `auth_api`) | +2 | 7 |
| Custom `apparmor.txt` | +1 | 8 |
| `privileged: SYS_ADMIN, DAC_READ_SEARCH` (counted once) | -1 | 7 |
| `host_network: true` (inherited) | -1 | 6 |

Clamped to the 1 to 6 scale, the rating is 6. `kernel_modules` falls in the
same once-only bucket as the privileged capabilities, so dropping it does
not move the number; it removes `SYS_MODULE` and the host module mapping.
`homeassistant_api` carries no rating factor.

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
