# syntax=docker/dockerfile:1.7
# Cold Chain Control Tower: 3-stage build
#   1. node:22-alpine         -> Vite production build of the React SPA
#   2. astral-sh/uv (py3.12)  -> locked, non-editable install of the Python package
#   3. python:3.12-slim       -> minimal non-root runtime serving API + SPA on :8000

# ---- Stage 1: frontend ----------------------------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python dependencies and package -----------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- Stage 3: runtime -----------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:${PATH}" \
    APP_ENV=production \
    LOG_FORMAT=json \
    HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/app/data \
    FRONTEND_DIST=/app/frontend/dist

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=frontend --chown=app:app /build/frontend/dist /app/frontend/dist
COPY --chown=app:app data/ /app/data/

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).status == 200 else 1)"]

CMD ["fde-copilot", "serve"]
