# --- openclaw runtime for xct-genesis ---
# Standalone container that runs the openclaw gateway with the
# WorldSeed channel plugin. It dials the xct-genesis server's
# /ws endpoint and drives every registered citizen agent.
#
# Deployed as a sibling Railway service inside the xct-genesis
# project. Reaches the engine over Railway's private network via
# ws://${WORLDSEED_HOST}:${WORLDSEED_PORT}/ws.

FROM node:22-slim

# git is needed because some packages in the openclaw dep graph are
# referenced via git URLs at install time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# openclaw CLI — pin to the version the WorldSeed plugin was authored
# against. Later releases removed `dispatchInboundReplyWithBase` and
# break the plugin at load.
RUN npm install -g openclaw@2026.3.13

# WorldSeed channel plugin source. Installed at runtime by the
# entrypoint so the plugin shows up in openclaw's plugin registry.
COPY openclaw-plugin /app/openclaw-plugin
RUN cd /app/openclaw-plugin && npm install --omit=dev --no-audit --no-fund

# Shared data referenced by the worldseed plugin (gateway.ts reads
# ../../shared/languages.json relative to its install location). The
# plugin survives without it (falls back to defaults), but we ship it
# so the experience is identical to xct-genesis-bundled mode.
COPY shared /app/shared

COPY openclaw-runtime/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# openclaw gateway doesn't accept inbound HTTP on a single, predictable
# port — but Railway requires every service to bind one. We expose a
# tiny netcat-driven healthcheck via the entrypoint instead. PORT is
# Railway-injected; default 8080 for local docker.
ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
