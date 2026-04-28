"""CLI subcommand: worldseed run — Codex-subagent-friendly launcher."""

from __future__ import annotations

import argparse
import json
import secrets
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
import uvicorn

from worldseed.cli._probe import wait_for_health
from worldseed.cli._workspace import init_workspace
from worldseed.world import WorldEngine

log = structlog.get_logger()


def run(args: argparse.Namespace) -> None:
    """Start engine, prepare workspace, claim agents, and print worker prompts."""
    from worldseed.dm.providers.llm import LiteLLMDMProvider
    from worldseed.gazette.context import detect_language
    from worldseed.persistence import RunRecorder
    from worldseed.scene.config import load_config as load_scene_config
    from worldseed.server.app import create_app

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.is_file():
        log.error("config_not_found", path=str(config_path))
        sys.exit(1)

    scene_cfg = load_scene_config(config_path)
    run_id = args.run_id or f"{scene_cfg.scene.id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    workspace = (
        Path(args.workspace).expanduser().resolve()
        if args.workspace
        else Path.home() / ".worldseed" / "workspaces" / run_id
    )

    dm = LiteLLMDMProvider(model=args.dm_model, fallback_model=args.dm_fallback)
    engine_run_id = secrets.token_hex(4)
    recorder = RunRecorder(
        run_id=engine_run_id,
        config_path=config_path,
        scene_id=scene_cfg.scene.id,
        dm_model=args.dm_model or "",
        resolved_config=scene_cfg.model_dump(),
    )
    detected = detect_language({"scene": {"description": scene_cfg.scene.description}})
    language = args.language or (detected if detected != "en" else "")
    engine = WorldEngine(config_path, dm_provider=dm, recorder=recorder, language=language)
    engine.prepopulate_agents()

    app = create_app(
        engine=engine,
        tick_interval=engine.config.scene.tick_interval,
        run_id=engine_run_id,
        port=args.port,
        auto_start_tick=True,
    )
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    base_url = f"http://127.0.0.1:{args.port}"
    wait_for_health(base_url)

    init_workspace(config_path, workspace, force=args.force)
    _patch_manifest(
        workspace,
        engine_run_id=engine_run_id,
        server_url=base_url,
        trajectory_ref="trajectory.md",
        story_ref="story.md",
    )
    _write_trajectory(scene_cfg.model_dump(), workspace, force=args.force)

    engine.register_from_config()
    claimed = [agent.id for agent in scene_cfg.agents]
    _print_launch_summary(
        scene_id=scene_cfg.scene.id,
        run_id=engine_run_id,
        workspace=workspace,
        base_url=base_url,
        claimed=claimed,
    )

    shutdown = threading.Event()

    def handle_signal(sig: int, frame: object) -> None:
        shutdown.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while not shutdown.is_set():
            shutdown.wait(timeout=2.0)
            if args.max_ticks is not None and engine.tick >= args.max_ticks:
                log.info("run_max_ticks_reached", tick=engine.tick)
                break
    finally:
        server.should_exit = True
        server_thread.join(timeout=5)
        if engine.recorder:
            engine.recorder.finalize(engine.tick, len(engine.get_registered_agents()))


def _patch_manifest(
    workspace: Path,
    *,
    engine_run_id: str,
    server_url: str,
    trajectory_ref: str,
    story_ref: str,
) -> None:
    path = workspace / "manifest.json"
    payload: dict[str, Any] = {}
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    payload["run_id"] = engine_run_id
    payload["server_url"] = server_url
    payload["trajectory_ref"] = trajectory_ref
    payload["story_ref"] = story_ref
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_trajectory(scenario: dict[str, Any], workspace: Path, *, force: bool) -> None:
    path = workspace / "trajectory.md"
    if path.exists() and not force:
        return
    path.write_text(_trajectory_markdown(scenario), encoding="utf-8")


def _trajectory_markdown(scenario: dict[str, Any]) -> str:
    scene = scenario.get("scene") or {}
    agents = scenario.get("agents") or []
    entities = scenario.get("entities") or []
    brief: dict[str, Any] = next(
        (e for e in entities if isinstance(e, dict) and e.get("type") == "brief"),
        {},
    )

    roles: dict[str, list[str]] = {}
    for agent in agents:
        if not isinstance(agent, dict) or not agent.get("id"):
            continue
        role = str(agent.get("role") or "worker")
        roles.setdefault(role, []).append(agent["id"])

    objective = brief.get("objective") or scene.get("description") or "Run the scenario to a useful deliverable."
    success = brief.get("success_criteria") or []
    success_block = "\n".join(f"- {item}" for item in success) or "- Define success with the user."
    role_lines = "\n".join(f"- {role}: {', '.join(ids)}" for role, ids in sorted(roles.items())) or (
        "- (no roles declared in scenario.yaml)"
    )

    return f"""# Trajectory

The primary session's operating plan for this run. Not engine logic, not a
scheduler, not the user-facing story. Edit it as the goal or the evidence
changes.

## Objective

{objective}

## Success Criteria

{success_block}

## Roles

{role_lines}

## Operating Principles

- Use the engine for facts: actions, events, inboxes, signals, audit.
- Use the primary session for judgment: phase changes, quality bar, strategy.
- Every durable claim should have an artifact id or file ref.
- Every critique, selection, rebuttal, revision should point to exact target ids.

## Plan The Stages

Edit this section once you've decided the run shape. List each stage, which
roles wake during it, what artifacts you expect, and the condition for moving
on. Example skeleton:

    ### 1. <stage-name>

    Wake: <role-or-agent-list>

    Expected artifacts:
    - ...

    Exit when ...
"""


def _print_launch_summary(
    *,
    scene_id: str,
    run_id: str,
    workspace: Path,
    base_url: str,
    claimed: list[str],
) -> None:
    prompt_by_agent = {
        agent_id: (
            f"You are {agent_id}.\n\n"
            f"Read {workspace}/agents/{agent_id}/AGENT.md.\n"
            f"Read {workspace}/trajectory.md.\n"
            "Use:\n"
            f"  WORLDSEED_WORKSPACE={workspace}\n"
            f"  WORLDSEED_AGENT_ID={agent_id}\n"
            f"  WORLDSEED_URL={base_url}\n\n"
            f"Run `python3 {workspace}/ws.py perceive` first. Work only under "
            f"{workspace}/agents/{agent_id}/. Publish world-relevant facts with "
            f"`python3 {workspace}/ws.py publish ...`. Return artifact ids, next "
            "intent, and blockers."
        )
        for agent_id in claimed
    }
    watcher_prompt = (
        f"Long-poll {base_url}/api/director/signals?timeout_s=30&limit=10. "
        "Return urgent/checkpoint signals to the primary session. Do not wake agents yourself. "
        "After each return, wait to be continued."
    )
    print("\nWorldSeed run ready")
    print(f"  Scene:      {scene_id}")
    print(f"  Engine run: {run_id}")
    print(f"  Server:     {base_url}")
    print(f"  Workspace:  {workspace}")
    print(f"  Trajectory: {workspace / 'trajectory.md'}")
    print(f"  Story:      {workspace / 'story.md'}")
    print(f"  Claimed:    {', '.join(claimed)}")
    print("\nWatcher prompt:")
    print(watcher_prompt)
    print("\nAgent prompts JSON:")
    print(json.dumps(prompt_by_agent, indent=2, ensure_ascii=False))
    print("\nPress Ctrl+C to stop the engine.\n")
