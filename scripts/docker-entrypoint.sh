#!/bin/bash
set -euo pipefail

# ── WorldSeed Railway entrypoint ─────────────────────────────────
# Railway injects PORT (e.g. 8080). Default 8000 for local testing.
LISTEN_PORT="${PORT:-8000}"

echo "=== WorldSeed starting on 0.0.0.0:${LISTEN_PORT} ==="

# lobby mode — dashboard-first, users configure via UI
exec uv run worldseed --host 0.0.0.0 --port "${LISTEN_PORT}"