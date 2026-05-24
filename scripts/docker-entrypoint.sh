#!/bin/bash
set -euo pipefail

# ── WorldSeed Railway entrypoint ─────────────────────────────────
# Railway injects PORT (e.g. 8080). Default 8000 for local testing.
LISTEN_PORT="${PORT:-8000}"

echo "=== WorldSeed starting on 0.0.0.0:${LISTEN_PORT} ==="

# ── Configure OpenClaw gateway (agent runtime) ───────────────────
# Requires OPENCLAW_MODEL env var (e.g. "anthropic/claude-sonnet-4").
# OPENCLAW_API_KEY is optional — uses OPENAI_API_KEY / ANTHROPIC_API_KEY from .env if unset.
OPENCLAW_MODEL="${OPENCLAW_MODEL:-}"
OPENCLAW_API_KEY="${OPENCLAW_API_KEY:-${ANTHROPIC_API_KEY:-${OPENAI_API_KEY:-}}}"
GATEWAY_TOKEN="${WORLDSEED_GATEWAY_TOKEN:-worldseed-gw-token}"

if [ -n "$OPENCLAW_MODEL" ] && command -v openclaw &>/dev/null; then
    echo "[openclaw] Configuring gateway (model: ${OPENCLAW_MODEL}) ..."

    # Initialise a minimal openclaw.json if none exists
    OPENCLAW_CFG="${HOME}/.openclaw/openclaw.json"
    OPENCLAW_DIR=$(dirname "$OPENCLAW_CFG")
    mkdir -p "$OPENCLAW_DIR"

    cat > "$OPENCLAW_CFG" <<EOF
{
  "agents": {
    "defaults": {
      "model": "${OPENCLAW_MODEL}"
    }
  },
  "gateway": {
    "mode": "local"
  },
  "plugins": {
    "entries": {
      "worldseed": {
        "enabled": true,
        "config": {
          "serverUrl": "ws://localhost:${LISTEN_PORT}/ws",
          "gatewayToken": "${GATEWAY_TOKEN}"
        }
      }
    }
  },
  "channels": {
    "worldseed": {
      "enabled": true,
      "accounts": {
        "default": {
          "serverUrl": "ws://localhost:${LISTEN_PORT}/ws",
          "gatewayToken": "${GATEWAY_TOKEN}"
        }
      }
    }
  }
}
EOF

    # Set API key if provided
    if [ -n "$OPENCLAW_API_KEY" ]; then
        # Write to .env for openclaw
        OPENCLAW_ENV="${HOME}/.openclaw/.env"
        echo "OPENCLAW_API_KEY=${OPENCLAW_API_KEY}" > "$OPENCLAW_ENV"
        # Also try standard provider keys
        if [[ "$OPENCLAW_MODEL" == anthropic* ]]; then
            echo "ANTHROPIC_API_KEY=${OPENCLAW_API_KEY}" >> "$OPENCLAW_ENV"
        elif [[ "$OPENCLAW_MODEL" == openai* ]] || [[ "$OPENCLAW_MODEL" == gpt* ]]; then
            echo "OPENAI_API_KEY=${OPENCLAW_API_KEY}" >> "$OPENCLAW_ENV"
        fi
    fi

    echo "[openclaw] Config written to ${OPENCLAW_CFG}"

    # Install worldseed plugin for OpenClaw
    echo "[openclaw] Installing worldseed plugin..."
    cd /app/openclaw-plugin && openclaw plugins install -l . 2>/dev/null || true
else
    if ! command -v openclaw &>/dev/null; then
        echo "[openclaw] CLI not found — gateway will not be available"
    else
        echo "[openclaw] OPENCLAW_MODEL not set — skipping gateway config"
    fi
fi

# lobby mode — dashboard-first, users configure via UI
exec uv run worldseed --host 0.0.0.0 --port "${LISTEN_PORT}"