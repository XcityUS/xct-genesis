# openclaw-runtime

Standalone Node container that runs the OpenClaw gateway with the
WorldSeed channel plugin. It dials the xct-genesis server's `/ws`
endpoint and drives every registered citizen agent.

Deployed as a sibling Railway service inside the **xct-genesis** project
so it can reach the engine over the project-local private network.

## Required env (on the Railway service)

| Var                       | Example                                    |
|---------------------------|--------------------------------------------|
| `WORLDSEED_HOST`          | `xct-genesis.railway.internal`             |
| `WORLDSEED_PORT`          | `${{xct-genesis.PORT}}` (Railway reference) |
| `WORLDSEED_GATEWAY_TOKEN` | shared secret (same as on xct-genesis)     |
| `OPENCLAW_MODEL`          | e.g. `xcity/glm-5.1`                       |
| `OPENCLAW_API_KEY`        | API key for the chosen model               |
| `ACCOUNT_ID` (optional)   | defaults to `xct-genesis`                  |

`WORLDSEED_PROTO` defaults to `ws`. Set to `wss` if you choose to dial
the public URL instead of `*.railway.internal`.

## How it connects

```
openclaw-runtime ── ws://${WORLDSEED_HOST}:${WORLDSEED_PORT}/ws ──> xct-genesis
       │
       └─ loads /app/openclaw-plugin (worldseed channel)
```

The plugin authenticates with `WORLDSEED_GATEWAY_TOKEN`, receives the
scene + agent payload, and runs autonomous loops for each citizen.
