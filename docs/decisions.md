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

## The server's frontend pin is recorded, not enforced (2026-09-13)

The fork wheel is installed over `music-assistant-frontend` with `--no-deps`,
so the version the server pinned for itself is gone by the time the image runs
and nothing notices the two drifting apart. `sync_upstream.py` now resolves
that pin from the server release and writes it as `SERVER_EXPECTS_FRONTEND`.

It is deliberately informational. Enforcing it is not possible from here: the
fork uses CalVer and does not record which upstream frontend release it was
built from, so there is no version to compare against. What this buys is
visibility — the pair appears in the build log, and the server's expectation
moving shows up as a line in the sync PR, which is the point at which the fork
may need a rebase. Making it enforceable means the fork publishing its upstream
base alongside the wheel; that is work for `HA_int_MA-UI`, not here.

## Server changes ship as anchored build-time edits, not a fork (2026-09-14)

The routing view needs the Home Assistant player provider to select inputs
on receiver zones, which upstream does not do. Contributing it upstream is out
(the Open Home Foundation AI policy and this repository's own rule), and
forking the server would mean owning its release train. Instead the change is
a script under `music_assistant_lm/patches/` that rewrites exact upstream
lines in the installed module during the image build. Anchoring on exact
lines is the point: when the server release changes the module, the anchor
is gone, the build fails, and the sync pull request shows it. A `patch(1)`
file would need the tool in the image and give worse errors; a sed would
apply blindly. The unit test applies the script to a vendored copy of the
upstream module pinned to the Dockerfile's server version, so the fixture
and the script move together with every server bump.

## Playback steers to the browsed source through the same mechanism (2026-09-14)

Playing from the Filesystem listing still streamed from Spotify: the server
resolves every uri to the library item and orders the stream candidates by
quality, with the playback user's provider filter as the only preference.
That filter is an account-wide access restriction, so setting it from a
listing toggle was out, and a per-play hint has no place in the API. The
`play_source_steer.py` edit reads the provider from the uri a play request
names, keeps it on the queue items as `extra_attributes.preferred_provider`
(the field the server already uses for playback speed, so it persists with
the queue), and puts it ahead of the quality order at stream time. The
frontend only has to name the source's own item in the uri it sends.

