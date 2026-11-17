# Production web image: gunicorn serving the customer API (ADR 0028). Build context: backend/.
#
# Differs from backend/Dockerfile (development and workers) on purpose:
# - no ffmpeg: nothing in the customer cache decodes media, and a binary that is absent cannot be
#   pointed at anything;
# - no tests and no recorded crawler fixtures (web.Dockerfile.dockerignore);
# - runs as an unprivileged user, and never migrates on start (a deploy step does, runbook).

FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# pdqhash ships no Linux wheels and is compiled from source (it is imported by the matching models).
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev


FROM python:3.13-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DJANGO_SETTINGS_MODULE=marceline.settings

RUN groupadd --system marceline \
    && useradd --system --gid marceline --home-dir /app --no-create-home marceline

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=root:root . .
RUN python -m compileall -q /app

USER marceline
EXPOSE 8000

# Two workers with two threads each fit a 2 vCPU / 2-4 GB machine next to Postgres; recycle
# workers now and then so a slow leak never becomes an outage. Access logs stay off: Caddy is the
# edge, and client addresses are personal data we do not need. No control socket: nothing uses it,
# and its default location is not writable for the unprivileged user.
CMD ["gunicorn", "marceline.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "2", "--threads", "2", \
     "--timeout", "30", "--graceful-timeout", "20", \
     "--max-requests", "2000", "--max-requests-jitter", "200", \
     "--worker-tmp-dir", "/dev/shm", \
     "--no-control-socket", \
     "--error-logfile", "-"]
