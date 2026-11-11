# Camera Viewer: the Relay (MediaMTX) and the Python backend in one image,
# under a small supervisor (ADR 0002).

# MediaMTX is pinned to exactly 1.21.2, the same as the Device Kit.
FROM bluenviron/mediamtx:1.21.2 AS relay
FROM ghcr.io/astral-sh/uv:0.11.21 AS uv

# The Device Kit this image was built with, served at /device-kit.tar.gz so a
# student can build a Device without the repo (#99). .dockerignore keeps every
# secret a Device generates out of the context, so none can reach the archive.
FROM python:3.13-slim AS kit
COPY device-kit /kit/device-kit
RUN tar --sort=name --owner=0 --group=0 --numeric-owner -C /kit -czf /kit/device-kit.tar.gz device-kit

FROM python:3.13-slim

COPY --from=uv /uv /usr/local/bin/uv
COPY --from=relay /mediamtx /usr/local/bin/mediamtx

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend ./backend
RUN uv sync --frozen --no-dev

COPY frontend ./frontend
COPY --from=kit /kit/device-kit.tar.gz ./frontend/device-kit.tar.gz
COPY relay/relay.yml /etc/camera-viewer/relay.yml

ENV PATH=/app/.venv/bin:$PATH \
    CV_FRONTEND_DIR=/app/frontend \
    CV_RELAY_CONFIG=/etc/camera-viewer/relay.yml \
    CV_CONFIG_DIR=/config \
    CV_CAPTURES_DIR=/captures

EXPOSE 8080/tcp 8189/udp

ENTRYPOINT ["python", "-m", "camera_viewer.supervisor"]
