# Decisions

## Build on the host, no registry image (2026-09-12)

The official app pulls `ghcr.io/music-assistant/server`; this app has no
`image:` key and lets Supervisor build the Dockerfile locally. Reason: the
image is the upstream one plus a single wheel, so publishing our own
multi-arch image would duplicate upstream's registry for no gain and add
signing and storage to maintain. Cost: a few minutes of build time on the
Home Assistant host per update.

## The image vulnerability scan reports, it does not gate (2026-09-12)

The baseline gates on high and critical findings. Here the OS layer is the
upstream Debian server image, which this repository cannot patch; a gate
would only block following the very upstream release that fixes it.
`security.yml` runs the scan with `fail-build: false`.

## The app version is CalVer, the upstream versions are arguments (2026-09-12)

Home Assistant compares app versions to decide whether an update exists.
Using the upstream server version as the app version would break when only
the frontend pin moves. CalVer via the shared release scripts keeps every
release a new version; the changelog records which server and frontend each
one carries.

## Own keys in config.yaml (2026-09-12)

`name`, `version`, `slug`, `description` and `url` are this app's; every
other key follows upstream verbatim so a change in upstream privileges or
options arrives with the next sync PR, where it is visible in the diff.

## Base images are pinned by digest, not only by tag (2026-09-13)

`FROM` and `COPY --from` carry `:<version>@sha256:...`. A tag is mutable, so
pinning only the version means an upstream re-push changes every rebuild with
no diff to review. `sync_upstream.py` resolves the multi-arch index digest for
both images on every run — not only when the version moves — so a re-publish
over the same version becomes a change in its own right and lands in a sync PR.

The tags stay alongside the digests. The daemon ignores them when a digest is
present, but they keep the file readable and the changelog meaningful.

This is also the only mitigation available for upstream's `aiolibdatachannel`
branch dependency (see security.md): the digest cannot make that pin
reproducible, but it fixes which build of it this app ships.

## backup_exclude is appended to, not owned (2026-09-13)

`webrtc_private_key.pem` has to be excluded from backups, but adding
`backup_exclude` to `OWN_KEYS` would freeze the whole list and stop upstream's
future additions arriving. Instead `EXTRA_LIST_ITEMS` appends this app's
entries to the upstream list during the merge, so upstream still owns the
contents and this app only adds. The merge is idempotent and the entry is
re-added if upstream ever drops the key.
