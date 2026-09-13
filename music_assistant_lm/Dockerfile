# syntax=docker/dockerfile:1
#
# The upstream Music Assistant server image with this fork's frontend
# installed over the upstream music-assistant-frontend package. The three
# FRONTEND_* arguments and SERVER_VERSION are maintained by
# scripts/sync_upstream.py; the app version comes from config.yaml.
#
# hadolint ignore=DL3006
ARG SERVER_VERSION="2.10.3"
FROM ghcr.io/music-assistant/server:${SERVER_VERSION}

# release tag on trooperthorn/HA_int_MA-UI, the wheel it carries and its
# sha256; empty FRONTEND_WHEEL keeps the upstream frontend (bootstrap only)
ARG FRONTEND_RELEASE="v2026.09.13.2"
ARG FRONTEND_WHEEL="music_assistant_frontend-2026.9.13.2-py3-none-any.whl"
ARG FRONTEND_SHA256="3be2f14227dec4acb4f09542c36ff79bafaf5756085a3457690bbd348cd854c7"

# the final server image ships a venv without pip; uv installs into it
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /bin/uv

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
        && "$VIRTUAL_ENV/bin/python" -c "import music_assistant_frontend as f; import importlib.metadata as m; p = f.where(); assert (p / 'index.html').is_file(); print('frontend', m.version('music-assistant-frontend'), p)"; \
    else \
        echo "No fork frontend pinned; the upstream frontend stays installed"; \
    fi \
    && chmod -R a+rX "$VIRTUAL_ENV/lib"

ARG BUILD_VERSION
ARG BUILD_ARCH
LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.name="Music Assistant (Library Manager)" \
    io.hass.description="Music Assistant server with the trooperthorn library-manager frontend" \
    org.opencontainers.image.source="https://github.com/trooperthorn/ha_app_music_assistant"

# the server image's entrypoint (jemalloc preload, --data-dir /data) is inherited
