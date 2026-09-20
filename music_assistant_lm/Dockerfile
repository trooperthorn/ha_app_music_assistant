# syntax=docker/dockerfile:1
#
# The upstream Music Assistant server image with this fork's frontend
# installed over the upstream music-assistant-frontend package. The three
# FRONTEND_* arguments, the two image versions and their digests are all
# maintained by scripts/sync_upstream.py; the app version comes from
# config.yaml.
#
# Every base image is pinned by digest as well as tag. A tag is mutable: the
# same server:2.10.3 can be re-published and every rebuild would then ship
# different bytes with nothing to show for it. The digest fixes the bytes, and
# because sync_upstream.py re-resolves it on every run, an upstream re-push
# surfaces as a digest change in the sync PR instead of a silent swap. Both
# are multi-arch index digests, so amd64 and aarch64 resolve from one pin.
#
# All four arguments are declared here, before the first FROM, so they are in
# global scope: BuildKit refuses variable expansion in COPY --from, and the
# supported way to keep uv's reference maintainable is to name it as a stage.
ARG SERVER_VERSION="2.10.4"
ARG SERVER_DIGEST="sha256:37a9a2776e838a754c9f5b38c432567389952304e7cb8f6b44b6cd28043de6de"
ARG UV_VERSION="0.12.13"
ARG UV_DIGEST="sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6"

# the final server image ships a venv without pip; uv installs into it
# hadolint ignore=DL3006
FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_DIGEST} AS uv

# hadolint ignore=DL3006
FROM ghcr.io/music-assistant/server:${SERVER_VERSION}@${SERVER_DIGEST}

# release tag on trooperthorn/HA_int_MA-UI, the wheel it carries and its
# sha256; empty FRONTEND_WHEEL keeps the upstream frontend (bootstrap only)
ARG FRONTEND_RELEASE="v2026.09.20.3"
ARG FRONTEND_WHEEL="music_assistant_frontend-2026.9.20.3-py3-none-any.whl"
ARG FRONTEND_SHA256="4e40a997de4ce7da1dbaa81b244f6974be7e4c06fc3c08e390e9c0102672bacd"

# The frontend version this server release pins for itself. The fork wheel is
# installed over that package with --no-deps, so the server's own expectation
# is gone by the time the image runs and nothing would otherwise notice the
# two drifting apart. Informational, not enforced: the fork is CalVer and does
# not carry the upstream release it was built from, so this cannot prove
# compatibility -- it keeps the pair in the build log and makes the server's
# expectation move visibly in a sync diff.
ARG SERVER_EXPECTS_FRONTEND="2.17.297"

COPY --from=uv /uv /bin/uv
# build-time edits of the upstream server: see patches/*.py for what each one
# changes and why; every one fails the build when its anchors are gone
COPY patches/ /tmp/patches/
COPY providers/ /tmp/providers/
# the entrypoint wrapper that mounts the music drive (docs/design.md)
COPY rootfs/ /

# exfatprogs: the read-only check (and the explicit repair task) for an exFAT
# music drive; rsync: the backup task. Debian's versions move with the base
# image, which is pinned by digest above.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends exfatprogs rsync \
    && rm -rf /var/lib/apt/lists/* \
    && chmod 0755 /usr/local/bin/mass_with_drive.py

ENV VIRTUAL_ENV=/app/venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# hadolint ignore=DL3008,SC2015
RUN set -eu \
    && if [ -n "$FRONTEND_WHEEL" ]; then \
        URL="https://github.com/trooperthorn/HA_int_MA-UI/releases/download/${FRONTEND_RELEASE}/${FRONTEND_WHEEL}" \
        && wget -q "$URL" -O "/tmp/${FRONTEND_WHEEL}" \
        && echo "${FRONTEND_SHA256}  /tmp/${FRONTEND_WHEEL}" | sha256sum --check - \
        && uv pip install --no-cache --no-deps --force-reinstall --link-mode=copy "/tmp/${FRONTEND_WHEEL}" \
        && rm -f "/tmp/${FRONTEND_WHEEL}" \
        && "$VIRTUAL_ENV/bin/python" -c "import music_assistant_frontend as f; import importlib.metadata as m; p = f.where(); assert (p / 'index.html').is_file(); print('frontend', m.version('music-assistant-frontend'), p)" \
        && echo "frontend: fork wheel installed over the ${SERVER_EXPECTS_FRONTEND} this server release pins"; \
    else \
        echo "No fork frontend pinned; the upstream frontend stays installed"; \
    fi \
    && "$VIRTUAL_ENV/bin/python" /tmp/patches/hass_source_select.py     && "$VIRTUAL_ENV/bin/python" /tmp/patches/play_source_steer.py     && "$VIRTUAL_ENV/bin/python" /tmp/patches/sendspin_opus_bitrate.py     && "$VIRTUAL_ENV/bin/python" /tmp/patches/playlist_bridge.py     && "$VIRTUAL_ENV/bin/python" /tmp/patches/folder_browser.py     && "$VIRTUAL_ENV/bin/python" /tmp/patches/library_trash.py     && "$VIRTUAL_ENV/bin/python" /tmp/patches/library_enrichment.py     && rm -rf /tmp/patches /tmp/providers     && chmod -R a+rX "$VIRTUAL_ENV/lib"

ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.name="Music Assistant (Library Manager)" \
    io.hass.description="Music Assistant server with the trooperthorn library-manager frontend" \
    org.opencontainers.image.source="https://github.com/trooperthorn/ha_app_music_assistant"

# the server image's entrypoint (jemalloc preload, --data-dir /data) runs as the
# child of the wrapper, which mounts the music drive first and unmounts it after
ENTRYPOINT ["/app/venv/bin/python", "/usr/local/bin/mass_with_drive.py", "--data-dir", "/data", "--cache-dir", "/data/.cache"]
