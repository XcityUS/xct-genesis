# CLAUDE.md — WorldSeed (xct-genesis)

## Project Overview

WorldSeed is a **multi-agent world engine** where AI-driven characters live autonomously inside YAML-defined scenes. You define entities, rules, agents, and actions in a YAML config; the engine runs a tick loop where agents perceive their world, act, and react — producing emergent narratives.

**Tech Stack:**
- **Backend:** Python 3.11+, FastAPI, Pydantic, LiteLLM (+ Instructor), structlog, uvicorn
- **Frontend:** React 18 + TypeScript + Vite, Tailwind CSS, shadcn/ui, Zustand (state management)
- **Package Manager:** [uv](https://github.com/astral-sh/uv) (fast Python package manager)
- **Agent Runtime:** OpenClaw Gateway (external Node.js process, connects via WebSocket)
- **LLM:** Any LiteLLM-supported provider (Anthropic, OpenAI, Gemini, Ollama, etc.)
- **Deployment:** Docker → Railway (multi-stage: Node frontend build → Python runtime)

## Project Structure

```
xct-genesis/
├── src/worldseed/          # Python backend source
│   ├── __main__.py          # Entry point
│   ├── cli/                 # CLI commands: play, validate, runs
│   │   ├── play.py          # `uv run worldseed play <config.yaml>`
│   │   ├── validate.py      # `uv run worldseed validate <config.yaml>`
│   │   └── codex_runner.py  # Codex subagent integration
│   ├── engine/              # Core tick loop & state
│   │   ├── tick.py          # Tick orchestration
│   │   ├── state_store.py   # Entity CRUD (flat properties only)
│   │   ├── event_log.py     # Events with TTL
│   │   ├── perceiver.py     # Per-agent perception filtering
│   │   ├── rules_engine.py  # Precondition checks + effect execution
│   │   ├── action_queue.py  # Agent action queue
│   │   ├── consequence_scanner.py  # Reactive world rules
│   │   ├── inbox.py         # Per-agent inbox
│   │   └── director/        # Director (GM) action queue & runtime
│   ├── dsl/                 # In-YAML rule engine
│   │   ├── effects/         # Effect executors (set, increment, emit_event, etc.)
│   │   ├── functions/       # DSL functions (random, aggregation, etc.)
│   │   ├── preconditions/   # Action gate conditions
│   │   └── path_resolver.py # $param reference resolution
│   ├── dm/                  # Dungeon Master (LLM judge)
│   │   ├── builder.py       # DM prompt construction
│   │   ├── prompt.py        # Prompt templates
│   │   └── providers/
│   │       ├── llm.py       # LiteLLM + Instructor provider
│   │       └── mock.py      # Mock provider for testing
│   ├── server/              # FastAPI application
│   │   ├── app.py           # App factory
│   │   ├── tick_runner.py   # Background tick scheduling
│   │   ├── websocket.py     # WebSocket handler
│   │   ├── routes/          # API route modules
│   │   │   ├── agents.py    # Agent registration, perception, actions
│   │   │   ├── dashboard.py # Dashboard data
│   │   │   ├── director.py  # Director/GM endpoints
│   │   │   ├── gateway.py   # OpenClaw gateway integration
│   │   │   ├── gazette.py   # Gazette/narrative endpoints
│   │   │   ├── gm.py        # Game master controls
│   │   │   ├── intro.py     # Scene intro/onboarding
│   │   │   ├── runs.py      # Run management
│   │   │   ├── settings.py  # Settings endpoints
│   │   │   └── world.py     # World state endpoints
│   │   └── _validation.py   # Request validation helpers
│   ├── scene/               # Scene config loading & validation
│   │   ├── config.py        # YAML loader + populator
│   │   ├── validator.py     # Config validation
│   │   ├── populator.py     # Entity/agent instantiation
│   │   └── checks/          # Sanity checks (physics, refs, smoke, UI)
│   ├── models/              # Pydantic models
│   │   ├── action.py        # Action models
│   │   ├── config_schema.py # Scene config schema
│   │   ├── entity.py        # Entity models
│   │   └── event.py         # Event models
│   ├── connector/           # Agent connector providers
│   │   ├── base.py          # Base connector interface
│   │   ├── websocket.py     # WebSocket connector
│   │   └── mock.py          # Mock connector for testing
│   ├── gazette/             # Gazette (narrative generation)
│   ├── narrator.py          # Auto-narrator
│   ├── agent_registry.py    # Agent lifecycle management
│   ├── agent_view.py        # Agent-facing world view
│   ├── director_resolver.py # Director action resolution
│   ├── world.py             # WorldEngine top-level facade
│   ├── persistence.py       # Event-sourcing persistence
│   └── paths.py             # Path constants
├── frontend/                # React frontend
│   ├── src/
│   │   ├── App.tsx          # Root component
│   │   ├── main.tsx         # Entry point
│   │   ├── i18n.ts          # i18n setup (en, zh, ja, ko, fr, de, es)
│   │   ├── stores/          # Zustand stores (app, world, agent, stream, etc.)
│   │   └── styles/          # CSS modules
│   └── package.json
├── configs/                 # Scene configs & documentation
│   ├── SCENE_CONFIG.md      # Full scene YAML schema reference
│   ├── SCENE_DSL.md         # DSL expression syntax
│   ├── SCENE_DESIGN.md      # Scene design guide
│   ├── UI_CONFIG.md         # Dashboard UI config schema
│   ├── teahouse.yaml        # Example: espionage scene
│   ├── ai_layoffs.yaml      # Example: corporate layoff scene
│   └── template.yaml        # Starter template
├── docs/
│   ├── ARCHITECTURE.md      # System architecture
│   ├── codex/               # Codex subagent docs
│   └── openclaw/            # OpenClaw integration docs
├── tests/
│   ├── unit/                # Fast, no IO
│   ├── e2e/                 # Real server tests
│   └── scenarios/           # Scene-agnostic tests
├── scripts/
│   └── docker-entrypoint.sh # Docker entrypoint
├── Dockerfile               # Multi-stage: Node build → Python runtime
├── pyproject.toml           # Project config, dependencies, tool settings
└── uv.lock                  # Locked dependencies
```

## Key Architectural Concepts

### Tick Loop
The world advances in discrete **ticks** (default: 5 seconds). Each tick:
1. Pull actions from `ActionQueue` (one per agent)
2. Check preconditions via `RulesEngine` (DSL expressions)
3. Execute deterministic effects **or** call LLM DM for uncertain outcomes
4. Run `ConsequenceScanner` (reactive rules)
5. Run `AutoTick` effects (decay, scheduled events)
6. Deliver filtered perceptions to each agent's inbox
7. Push wake signals to connected agents via WebSocket

### Data Separation
- **Entity** = world state only (id, type, flat properties). No personality, no goals.
- **AgentConfig** = agent identity (character dict, free-form). Stored separately, never accessed by engine/Perceiver/Inbox.
- Only the DM prompt builder reads `AgentConfig`.

### Perception Model
- `perception.visibility`: DSL rules evaluated per observer per entity per tick
- `perception.event_scopes`: custom scopes for event visibility
- `hidden_properties`: properties never sent to agents
- Built-in scopes: `global`, `target_only`, `admin`

### DM (Dungeon Master) System
- Stateless LLM calls via LiteLLM + Instructor for structured output
- DM judges physical outcomes ONLY — never describes other agents' behavior
- Parallel calls per tick via `asyncio.gather()`
- Rate-limited by `MAX_DM_CALLS_PER_TICK`

### DSL (Domain Specific Language)
- In-YAML expression language for preconditions, effects, perception rules
- Supports: arithmetic, comparisons, logical operators, built-in functions
- Effect types: `set`, `increment`, `decrement`, `emit_event`, `create_entity`, `remove_entity`, `add_to_list`, `remove_from_list`, etc.
- See `configs/SCENE_DSL.md` for full syntax

### Scene Config (YAML)
Required sections: `scene`, `entities`, `actions`
Optional: `templates`, `agents`, `consequences`, `auto_tick`, `perception`, `sanity_checks`, `narrator`

### Persistence
Run data saved to `~/.worldseed/runs/{run_id}/`:
- `meta.json`, `config.yaml`, `stream.jsonl`, `state.json`, `state_final.json`, `summary.json`

## Development Commands

```bash
# Setup
uv sync --all-extras

# Run tests
uv run pytest tests/ -q              # all (parallel via xdist)
uv run pytest tests/unit/ -q         # fast, no IO
uv run pytest tests/e2e/ -v          # real server
uv run pytest tests/scenarios/ -q    # scene-agnostic

# Lint, format, type-check
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
uv run mypy src/

# Run a scene
cp .env.example .env
# Add your API key
uv run worldseed play configs/ai_layoffs.yaml
# Dashboard at http://localhost:8000

# Validate a scene config
uv run worldseed validate configs/my_scene.yaml

# Build frontend
cd frontend && npm install && npm run build

# Docker build
docker build -t worldseed .
docker run -p 8000:8000 --env-file .env worldseed
```

## Environment Variables

Set in `.env` (copy from `.env.example`):
- **LLM API key** (one of): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`
- `WORLDSEED_DM_MODEL` — default DM model name
- `BFL_API_KEY` — FLUX image generation (optional)

## OpenClaw Gateway

WorldSeed expects an external **OpenClaw** process for autonomous agent loops. The server spawns it via `subprocess.Popen(["openclaw", "gateway"])`. Without it:
- The world starts and the dashboard works
- Agents do NOT think or act autonomously
- You can still manually interact via the UI

To enable: install OpenClaw (`npm i -g openclaw`) or use the `agent_runtime: custom` option in scene config.

## Important Conventions

- **Python:** Pydantic v2 models, strict mypy, ruff linting (E, F, I, UP), line length 120
- **Tests:** Default to xdist parallel (`-n auto`). Use `pytest -p no:xdist` for sequential.
- **Frontend:** Zustand stores, CSS modules, i18n via react-i18next (7 languages)
- **Scene YAML:** Flat property format preferred over nested `properties:` dict
- **Entity state:** Always flat key-value pairs. No nested objects in entity properties.
- **AgentConfig.character:** Free-form dict, scene-specific. Not validated by engine.

## Common Patterns

### Adding a New Scene
1. Create `configs/my_scene.yaml` (use `template.yaml` as starting point)
2. Validate: `uv run worldseed validate configs/my_scene.yaml`
3. Run: `uv run worldseed play configs/my_scene.yaml`
4. Use `/create-world` skill with AI to generate initial YAML, then hand-craft

### Adding a New DSL Effect
1. Create executor function in `src/worldseed/dsl/effects/`
2. Register in `src/worldseed/dsl/effects/_registry.py`
3. Add tests in `tests/unit/`

### Adding a New API Route
1. Create route module in `src/worldseed/server/routes/`
2. Register in `src/worldseed/server/app.py`
3. Add Pydantic models in `src/worldseed/server/models.py` if needed

## Deployment (Railway)

- Multi-stage Docker build: Node 22 (frontend) → Python 3.11 (backend)
- Railway injects `PORT` env var (default 8000)
- Entrypoint: `scripts/docker-entrypoint.sh`
- Frontend built at `/app/frontend/dist`, served by FastAPI static files
- OpenClaw gateway installed in Stage 2 via NodeSource Node.js 22 + npm
- Required env vars for gateway: `OPENCLAW_MODEL`, `OPENCLAW_API_KEY` (optional, falls back to ANTHROPIC_API_KEY / OPENAI_API_KEY)
