#!/bin/bash
# openclaw-runtime entrypoint.
#
# Writes ~/.openclaw/openclaw.json from env vars, installs the bundled
# worldseed plugin, then exec's `openclaw gateway`.
#
# Required env (set on the Railway service):
#   WORLDSEED_HOST       — xct-genesis private DNS, e.g. xct-genesis.railway.internal
#   WORLDSEED_PORT       — xct-genesis listening port (Railway-assigned PORT
#                          of the xct-genesis service; pass via reference variable)
#   WORLDSEED_GATEWAY_TOKEN — shared with xct-genesis (env on both services)
#   OPENCLAW_MODEL       — e.g. "xcity/glm-5.1"
#   OPENCLAW_API_KEY     — used as OPENAI_API_KEY (since tokenhub speaks OpenAI)
#                          Falls back to OPENAI_API_KEY / ANTHROPIC_API_KEY.
# Optional:
#   ACCOUNT_ID           — defaults to "xct-genesis"
#   WORLDSEED_PROTO      — defaults to "ws" (use "wss" for public TLS)
#   HEALTHCHECK_PORT     — port to bind tiny healthcheck on (default $PORT)
set -euo pipefail

ACCOUNT_ID="${ACCOUNT_ID:-xct-genesis}"
PROTO="${WORLDSEED_PROTO:-ws}"
HOST="${WORLDSEED_HOST:?WORLDSEED_HOST required (e.g. xct-genesis.railway.internal)}"
WS_PORT="${WORLDSEED_PORT:?WORLDSEED_PORT required (xct-genesis listening port)}"
GATEWAY_TOKEN="${WORLDSEED_GATEWAY_TOKEN:?WORLDSEED_GATEWAY_TOKEN required}"
MODEL="${OPENCLAW_MODEL:?OPENCLAW_MODEL required}"
API_KEY="${OPENCLAW_API_KEY:-${OPENAI_API_KEY:-${ANTHROPIC_API_KEY:-}}}"
HC_PORT="${HEALTHCHECK_PORT:-${PORT:-8080}}"

SERVER_URL="${PROTO}://${HOST}:${WS_PORT}/ws"

OPENCLAW_CFG="${HOME}/.openclaw/openclaw.json"
mkdir -p "$(dirname "$OPENCLAW_CFG")"

# openclaw expects accounts.default (or an explicit bindings entry) for
# channel routing; we also write the named account so logs are clear.
ACCT_BLOCK=$(cat <<ACCT
        "default": {
          "serverUrl": "${SERVER_URL}",
          "gatewayToken": "${GATEWAY_TOKEN}"
        },
        "${ACCOUNT_ID}": {
          "serverUrl": "${SERVER_URL}",
          "gatewayToken": "${GATEWAY_TOKEN}"
        }
ACCT
)

cat > "$OPENCLAW_CFG" <<EOF
{
  "agents": {
    "defaults": {
      "model": "${MODEL}"
    }
  },
  "gateway": {
    "mode": "local"
  },
  "plugins": {
    "load": {
      "paths": ["/app/openclaw-plugin"]
    },
    "entries": {
      "worldseed": {
        "enabled": true,
        "config": {
          "serverUrl": "${SERVER_URL}",
          "gatewayToken": "${GATEWAY_TOKEN}"
        }
      }
    }
  },
  "channels": {
    "worldseed": {
      "enabled": true,
      "accounts": {
${ACCT_BLOCK}
      }
    }
  }
}
EOF

if [ -n "$API_KEY" ]; then
    OPENCLAW_ENV_FILE="${HOME}/.openclaw/.env"
    {
        echo "OPENCLAW_API_KEY=${API_KEY}"
        if [[ "$MODEL" == anthropic* ]]; then
            echo "ANTHROPIC_API_KEY=${API_KEY}"
        else
            echo "OPENAI_API_KEY=${API_KEY}"
        fi
    } > "$OPENCLAW_ENV_FILE"
fi

echo "[openclaw-runtime] Config written: ${OPENCLAW_CFG}"
echo "[openclaw-runtime]   target: ${SERVER_URL}"
echo "[openclaw-runtime]   account: ${ACCOUNT_ID}, model: ${MODEL}"

# Plugin is loaded via plugins.load.paths set in openclaw.json above,
# so no extra install step is needed at boot.

# Railway expects every service to listen on $PORT for healthcheck.
# openclaw gateway has no HTTP listener of its own — bind a permanent
# "OK" responder on $HC_PORT in the background so Railway's TCP probe
# passes. We use python (already absent — use a tiny node one-liner).
node -e "
const http = require('http');
http.createServer((_, res) => { res.statusCode = 200; res.end('ok'); }).listen(${HC_PORT}, '0.0.0.0', () => {
  console.log('[healthcheck] listening on :${HC_PORT}');
});
" &

cd /
echo "[openclaw-runtime] Starting openclaw gateway..."
exec openclaw gateway
