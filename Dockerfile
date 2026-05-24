# ── Stage 1: Build React/Vite frontend ──────────────────────────
FROM node:22-slim AS frontend-build

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --include=dev

COPY frontend/ ./
RUN npm run build

# ── Stage 2: Python runtime + serve ──────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Install uv (fast Python package manager)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

# System deps: curl (healthcheck), Node.js 22 + npm (OpenClaw gateway)
# NodeSource provides Node.js 22 (openclaw requires >=22.19.0; use Latest LTS)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install OpenClaw CLI (agent runtime)
RUN npm install -g openclaw@latest

# Copy WorldSeed plugin for OpenClaw (installed at runtime by entrypoint)
COPY openclaw-plugin/ /app/openclaw-plugin/
RUN cd /app/openclaw-plugin && npm install

# ── Layer 1: Dependencies (cached when pyproject / lock unchanged)
COPY pyproject.toml uv.lock ./
RUN uv sync --extra dm --frozen --no-dev --no-install-project

# ── Layer 2: Project source (lightweight layer, changes often)
COPY src/ ./src/
COPY configs/ ./configs/
COPY shared/ ./shared/
RUN uv sync --extra dm --frozen --no-dev

# ── Layer 3: Built frontend (from Stage 1)
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

# ── Entrypoint
COPY scripts/docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

# Railway injects PORT; default 8000 for local testing
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]