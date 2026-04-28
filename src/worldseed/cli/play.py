"""CLI subcommand: worldseed play — start engine + server.

Two modes:

  --agent-runtime none      (default)
    Bare engine + HTTP/WS server. Agents are driven externally by any runtime
    that uses
    POST /act + GET /perceive. Ticks auto-start. No OpenClaw, no gateway.

  --agent-runtime openclaw  (legacy)
    Auto-spawn the OpenClaw gateway, wire the WebSocket connector, wait
    for preset agents to register via plugin, then start ticks. Useful
    when you want plug-and-play agents without writing your own runtime.
"""

from __future__ import annotations

import argparse
import secrets
import signal
import sys
import threading
import time
from pathlib import Path

import httpx
import structlog
import uvicorn

from worldseed.cli._probe import wait_for_health
from worldseed.world import WorldEngine

log = structlog.get_logger()


def _clean_stale_worldseed_sessions(current_run_id: str) -> None:
    """Remove old WorldSeed session entries from OpenClaw's session store.

    Session keys look like ``agent:{id}:worldseed:{run_id}``.
    Only invoked when --agent-runtime openclaw is selected.
    """
    import json
    import re

    agents_dir = Path.home() / ".openclaw" / "agents"
    if not agents_dir.is_dir():
        return

    ws_pattern = re.compile(r"^agent:.+:worldseed:(.+)$")
    total_removed = 0

    for agent_dir in agents_dir.iterdir():
        store_path = agent_dir / "sessions" / "sessions.json"
        if not store_path.is_file():
            continue
        try:
            store: dict[str, object] = json.loads(store_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        to_delete = [key for key in store if (m := ws_pattern.match(key)) and m.group(1) != current_run_id]
        if not to_delete:
            continue

        for key in to_delete:
            del store[key]

        try:
            store_path.write_text(json.dumps(store, indent=2), "utf-8")
            total_removed += len(to_delete)
        except OSError:
            pass

    if total_removed:
        log.info("cleaned_stale_worldseed_sessions", removed=total_removed)


def play(args: argparse.Namespace) -> None:
    """Start engine + server. Mode chosen by --agent-runtime."""
    from worldseed.dm.providers.llm import LiteLLMDMProvider
    from worldseed.persistence import RunRecorder
    from worldseed.scene.config import load_config as _load_config
    from worldseed.server.app import create_app

    runtime = getattr(args, "agent_runtime", "none")
    use_openclaw = runtime == "openclaw"

    config_path = Path(args.config)
    if not config_path.exists():
        log.error("config_not_found", path=str(config_path))
        sys.exit(1)

    dm = LiteLLMDMProvider(
        model=args.dm_model,
        fallback_model=args.dm_fallback,
    )

    run_id = secrets.token_hex(4)
    scene_cfg = _load_config(config_path)
    recorder = RunRecorder(
        run_id=run_id,
        config_path=config_path,
        scene_id=scene_cfg.scene.id,
        dm_model=args.dm_model or "",
        resolved_config=scene_cfg.model_dump(),
    )

    from worldseed.gazette.context import detect_language

    desc = scene_cfg.scene.description
    detected = detect_language({"scene": {"description": desc}})
    language = args.language or (detected if detected != "en" else "")

    engine = WorldEngine(
        config_path,
        dm_provider=dm,
        recorder=recorder,
        language=language,
    )
    engine.prepopulate_agents()

    # Default mode: ticks start with the server, no external connector.
    # OpenClaw mode: ticks start after the gateway brings agents online.
    app = create_app(
        engine=engine,
        tick_interval=engine.config.scene.tick_interval,
        run_id=run_id,
        port=args.port,
        auto_start_tick=not use_openclaw,
    )

    if use_openclaw:
        from worldseed.connector.websocket import WebSocketConnector

        ws_conn = WebSocketConnector(app.state.ws_manager)
        app.state.tick_runner.connector = ws_conn

    max_dm = args.max_dm_calls
    max_ticks = args.max_ticks
    timeout_min = args.timeout
    start_time = time.monotonic()

    port = args.port
    base_url = f"http://127.0.0.1:{port}"

    uvi_config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(uvi_config)
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    try:
        wait_for_health(base_url, attempts=30, delay=0.5)
    except RuntimeError:
        log.error("server_start_timeout")
        sys.exit(1)

    agent_count = len(engine.config.agents)

    log.info(
        "play_started",
        scene=engine.config.scene.id,
        agent_runtime=runtime,
        agents=agent_count,
        dm_model=args.dm_model,
        max_ticks=max_ticks,
        max_dm_calls=max_dm,
        timeout_min=timeout_min,
        dashboard=base_url,
    )

    if use_openclaw:
        _clean_stale_worldseed_sessions(run_id)

        def _auto_connect_agents() -> None:
            try:
                httpx.post(f"{base_url}/api/tick/resume", timeout=5)
                log.info("tick_resume_ok")
            except Exception:
                log.warning("tick_resume_failed")
                return

            expected = len(engine.config.agents)
            for _ in range(120):
                try:
                    r = httpx.get(f"{base_url}/health", timeout=2)
                    ready = len(r.json().get("agents", {}).get("ready", []))
                    if ready >= expected:
                        log.info("all_agents_ready", ready=ready)
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            else:
                log.warning("agents_ready_timeout")

        threading.Thread(target=_auto_connect_agents, daemon=True).start()

    config_max_ticks = engine.config.scene.max_ticks
    effective_max_ticks = (
        min(t for t in (max_ticks, config_max_ticks) if t is not None) if max_ticks or config_max_ticks else None
    )

    print(f"\n  WorldSeed play: {engine.config.scene.id}")
    print(f"  Run id:    {run_id}")
    print(f"  Run store: ~/.worldseed/runs/{run_id}")
    print(f"  Server:    {base_url}")
    print(f"  Agents:    {agent_count}")
    print(f"  Runtime:   {runtime}")
    print(f"  DM:        {args.dm_model or '(none)'}")
    print(f"  Max ticks: {effective_max_ticks or 'unlimited'}")
    if effective_max_ticks and not max_ticks:
        print(
            f"  ⚠ Will auto-stop after {effective_max_ticks} ticks (default)."
            " Set scene.max_ticks in config or --max-ticks to change."
        )
    if max_dm:
        print(f"  Max DM calls: {max_dm}")
    if timeout_min:
        print(f"  Timeout: {timeout_min}m")

    if not use_openclaw:
        print()
        print("  [agent runtime: none] Engine + server are up. To run agents:")
        print(f"    1. python scripts/init_workspace.py --scenario {config_path} \\")
        print("           --workspace ~/.worldseed/workspaces/<run_id>")
        print(f"    2. POST {base_url}/register for each preset agent")
        print(f"    3. spawn subagents with WORLDSEED_URL={base_url} and")
        print("       WORLDSEED_AGENT_ID=<their id>; they use ws.py to act/perceive")
        print(f"    4. watcher long-polls {base_url}/api/director/signals")

    print("  Press Ctrl+C to stop.\n")

    shutdown = threading.Event()

    def handle_signal(sig: int, frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    paused = False
    try:
        while not shutdown.is_set():
            shutdown.wait(timeout=5.0)

            if paused:
                continue

            tick = engine.tick
            reason = None
            if max_ticks and tick >= max_ticks:
                reason = f"max_ticks_reached ({tick})"
            elif max_dm and dm.call_count >= max_dm:
                reason = f"max_dm_calls_reached ({dm.call_count})"
            elif timeout_min:
                elapsed = (time.monotonic() - start_time) / 60
                if elapsed >= timeout_min:
                    reason = f"timeout_reached ({round(elapsed, 1)}m)"

            if reason:
                log.info("budget_reached_pausing", reason=reason)
                httpx.post(f"{base_url}/api/tick/pause", timeout=2)
                paused = True
                print(f"\n  Paused: {reason}")
                print(f"  Server still live at {base_url}")
                print("  Press Ctrl+C to shut down.\n")
    finally:
        print("\n  Shutting down...")
        entities = [e.to_full_dict() for e in engine.state.all_entities()]
        recorder.save_final_state(entities)
        recorder.finalize(
            tick_count=engine.tick,
            agent_count=len(engine.get_registered_agents()),
        )
        server.should_exit = True
        if use_openclaw:
            from worldseed.server.routes._shared import _kill_gateway

            _kill_gateway(app)
        server_thread.join(timeout=5)
        print("  Done.")
