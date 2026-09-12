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
