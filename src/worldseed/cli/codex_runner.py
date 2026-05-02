"""CLI subcommand: worldseed codex-runner.

Automates the Codex-subagent loop against a running WorldSeed server.

In manual tick mode, the runner pauses the engine, runs one Codex CLI
activation per selected agent, steps the engine once, then repeats.

In auto tick mode, the server tick runner remains live. This process only
waits for new ticks and activates agents; the engine advances itself.

This runner intentionally keeps game-specific judgment out of Python. Agent
actions are chosen by `codex exec` from each workspace `AGENT.md` plus the live
`ws.py perceive` output. Pending DM requests are handed to a separate Codex DM
activation, which must return a structured narrative/effects judgment.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml


@dataclass(frozen=True)
class RunnerConfig:
    workspace: Path
    base_url: str
    repo_root: Path
    agents: list[str]
    max_cycles: int
    tick_mode: str
    agent_timeout: float
    dm_timeout: float
    dm_max_attempts: int
    signal_timeout: float
    parallel: bool
    dangerous_bypass: bool
    model: str | None
    dry_run: bool
    async_wait_timeout: float
    async_poll_interval: float
    codex_config: dict[str, Any]


@dataclass(frozen=True)
class AsyncGroupStatus:
    name: str
    queued: int
    terminal: int

    @property
    def pending(self) -> int:
        return max(0, self.queued - self.terminal)


@dataclass(frozen=True)
class AsyncRefreshStatus:
    groups: list[AsyncGroupStatus]
    refresh_rows: int | None = None
    state_entities: int | None = None

    @property
    def pending_total(self) -> int:
        return sum(group.pending for group in self.groups)

    @property
    def needs_state_refresh(self) -> bool:
        if self.refresh_rows is None or self.state_entities is None:
            return False
        return self.refresh_rows > self.state_entities

    def pending_summary(self) -> str:
        return ", ".join(f"{group.name}={group.pending}" for group in self.groups)


def codex_runner(args: Any) -> None:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"workspace not found: {workspace}")
    manifest = _load_json(workspace / "manifest.json")
    agents = _resolve_agents(args.agents, manifest)
    if not agents:
        raise SystemExit("no agents selected")
    scenario = _load_yaml(workspace / "scenario.yaml")
    codex_config = _codex_config(scenario)

    cfg = RunnerConfig(
        workspace=workspace,
        base_url=args.url.rstrip("/"),
        repo_root=Path(args.repo_root).expanduser().resolve(),
        agents=agents,
        max_cycles=args.max_cycles,
        tick_mode=args.tick_mode,
        agent_timeout=args.agent_timeout,
        dm_timeout=args.dm_timeout,
        dm_max_attempts=args.dm_max_attempts,
        signal_timeout=args.signal_timeout,
        parallel=args.parallel,
        dangerous_bypass=args.dangerous_bypass,
        model=args.model,
        dry_run=args.dry_run,
        async_wait_timeout=args.async_wait_timeout,
        async_poll_interval=max(0.5, args.async_poll_interval),
        codex_config=codex_config,
    )

    print("WorldSeed Codex runner")
    print(f"  Server:    {cfg.base_url}")
    print(f"  Workspace: {cfg.workspace}")
    print(f"  Agents:    {', '.join(cfg.agents)}")
    print(f"  Cycles:    {cfg.max_cycles}")
    print(f"  Tick mode: {cfg.tick_mode}")
    for line in _codex_describe(cfg):
        print(f"  {line}")
    if cfg.dry_run:
        print("  Mode:      dry-run")

    last_health: dict[str, Any] | None = None
    if not cfg.dry_run and cfg.tick_mode == "manual":
        _post(cfg, "/api/tick/pause", {})
        _handle_director_signals(cfg)
    elif not cfg.dry_run:
        last_health = _ensure_auto_tick_running(cfg)
        _handle_director_signals(cfg)

    for cycle in range(1, cfg.max_cycles + 1):
        print(f"\ncycle {cycle}/{cfg.max_cycles}")
        if cfg.dry_run:
            for agent_id in cfg.agents:
                print(_codex_command_preview(cfg, agent_id))
            continue

        if cfg.tick_mode == "auto":
            if cycle == 1:
                last_health = _get(cfg, "/health")
                print(f"  auto tick: status={last_health.get('status')} tick={last_health.get('tick')}")
            else:
                last_health = _wait_for_next_auto_tick(cfg, _health_tick(last_health))
            _handle_director_signals(cfg)

        _run_agent_activations(cfg)
        if cfg.tick_mode == "manual":
            step = _post(cfg, "/api/tick/step", {})
            print(f"  step: tick={step.get('tick')} actions_processed={step.get('actions_processed')}")
            _handle_director_signals(cfg)
            if _wait_for_scene_async_refresh(cfg):
                _handle_director_signals(cfg)

            health = _get(cfg, "/health")
            print(f"  health: status={health.get('status')} tick={health.get('tick')}")
        else:
            _handle_director_signals(cfg)
            last_health = _get(cfg, "/health")
            print(f"  health: status={last_health.get('status')} tick={last_health.get('tick')}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"missing required file: {path}") from exc
    return _require_json_object(data, label=str(path))


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise SystemExit(f"missing required file: {path}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"scenario must be a mapping: {path}")
    return data


def _resolve_agents(raw: str | None, manifest: dict[str, Any]) -> list[str]:
    if raw:
        return [a.strip() for a in raw.split(",") if a.strip()]
    agents = manifest.get("agents") or []
    return [str(a) for a in agents]


def _codex_config(scenario: dict[str, Any]) -> dict[str, Any]:
    scene = scenario.get("scene") or {}
    if not isinstance(scene, dict):
        return {}
    cfg = scene.get("codex") or {}
    return cfg if isinstance(cfg, dict) else {}


def _get(cfg: RunnerConfig, path: str) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(f"{cfg.base_url}{path}")
        resp.raise_for_status()
        return _require_json_object(resp.json(), label=path)


def _post(cfg: RunnerConfig, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        resp = client.post(f"{cfg.base_url}{path}", json=body if body is not None else {})
        resp.raise_for_status()
        return _require_json_object(resp.json(), label=path)


def _require_json_object(data: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise SystemExit(f"expected JSON object from {label}, got {type(data).__name__}")
    return data


def _ensure_auto_tick_running(cfg: RunnerConfig) -> dict[str, Any]:
    health = _get(cfg, "/health")
    if health.get("status") == "live" and health.get("running") is True:
        return health

    print(
        f"  auto tick: server is not live (status={health.get('status')} tick={health.get('tick')}); requesting resume"
    )
    _post(cfg, "/api/tick/resume", {})
    deadline = time.monotonic() + max(30.0, cfg.async_poll_interval)
    while time.monotonic() < deadline:
        time.sleep(min(1.0, cfg.async_poll_interval))
        health = _get(cfg, "/health")
        if health.get("status") == "live" and health.get("running") is True:
            return health

    raise SystemExit(
        "tick-mode=auto requires the server background tick runner to be live. "
        f"Current health: status={health.get('status')} tick={health.get('tick')} "
        f"running={health.get('running')}"
    )


def _wait_for_next_auto_tick(cfg: RunnerConfig, previous_tick: int | None) -> dict[str, Any]:
    if previous_tick is None:
        return _ensure_auto_tick_running(cfg)

    print(f"  auto tick: waiting for tick > {previous_tick}")
    deadline = time.monotonic() + max(60.0, cfg.async_wait_timeout)
    last_health: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        time.sleep(cfg.async_poll_interval)
        health = _get(cfg, "/health")
        last_health = health
        if health.get("status") != "live" or health.get("running") is not True:
            raise SystemExit(
                "auto tick runner stopped unexpectedly: "
                f"status={health.get('status')} tick={health.get('tick')} running={health.get('running')}"
            )
        tick = _health_tick(health)
        if tick is not None and tick > previous_tick:
            print(f"  auto tick: observed tick={tick}")
            return health

    health = last_health or _get(cfg, "/health")
    raise SystemExit(
        "timed out waiting for server auto tick: "
        f"previous_tick={previous_tick} current_tick={health.get('tick')} status={health.get('status')}"
    )


def _health_tick(health: dict[str, Any] | None) -> int | None:
    if not health:
        return None
    tick = health.get("tick")
    return tick if isinstance(tick, int) else None


def _handle_director_signals(cfg: RunnerConfig) -> None:
    while True:
        try:
            data = _get(
                cfg,
                f"/api/director/signals?timeout_s={cfg.signal_timeout}&limit=100",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return
            raise
        signals = data.get("signals") or []
        if not signals:
            return

        progressed = False
        for sig in signals:
            sid = str(sig.get("id"))
            stype = sig.get("type")
            if stype == "dm_request":
                req_id = (sig.get("refs") or {}).get("dm_request_id")
                if not req_id:
                    raise SystemExit(f"dm_request signal missing request id: {sig}")
                if _resolve_dm_request(cfg, str(req_id)):
                    progressed = True
                    continue
                raise SystemExit(f"unsupported pending DM request: {req_id}")
            if stype in {"urgent", "checkpoint"}:
                _post(cfg, f"/api/director/signals/{sid}/ack", {})
                print(f"  ack {stype}: {sid} {sig.get('reason', '')}")
                progressed = True
                continue
            raise SystemExit(f"unknown director signal type: {sig}")

        if not progressed:
            return


def _resolve_dm_request(cfg: RunnerConfig, request_id: str) -> bool:
    req = _get(cfg, f"/api/director/dm/{request_id}")
    if req.get("status") != "pending":
        return True

    last_error = ""
    for attempt in range(1, cfg.dm_max_attempts + 1):
        proc, response = _run_dm_activation(cfg, req, attempt=attempt, last_error=last_error)
        _print_dm_result(request_id, proc)
        if proc.returncode != 0:
            last_error = f"codex dm exited {proc.returncode}"
            continue
        if response is None:
            last_error = "codex dm did not return valid JSON"
            continue
        response = _normalize_dm_response(response)
        invalid_reason = _validate_dm_response_locally(response, req)
        if invalid_reason:
            last_error = invalid_reason
            print(f"  dm local validation failed: {invalid_reason}", file=sys.stderr)
            continue

        try:
            _post(cfg, f"/api/director/dm/{request_id}/resolve", response)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text
            raise SystemExit(f"DM resolve rejected for {request_id}: {detail}") from exc

        print(
            "  resolve dm: "
            f"{request_id} {req.get('source_type')}:{req.get('source_name')} "
            f"effects={len(response.get('effects') or [])}"
        )
        return True

    raise SystemExit(f"Codex DM failed for {request_id}: {last_error or 'unknown error'}")


def _wait_for_scene_async_refresh(cfg: RunnerConfig) -> bool:
    async_cfg = cfg.codex_config.get("async_refresh") or {}
    if not isinstance(async_cfg, dict) or not async_cfg.get("enabled"):
        return False

    run_id = _current_run_id(cfg)
    if not run_id:
        return False

    status = _async_refresh_status(cfg, run_id, async_cfg)
    if status.needs_state_refresh:
        return _step_for_async_refresh(cfg, status, reason="state behind results")
    if status.pending_total <= 0:
        return False
    if cfg.async_wait_timeout <= 0:
        print(f"  async: work still pending ({status.pending_summary()}); async wait disabled")
        return False

    deadline = time.monotonic() + cfg.async_wait_timeout
    last_pending = status.pending_total
    print(f"  async: waiting for background work ({status.pending_summary()})")

    while time.monotonic() < deadline:
        time.sleep(cfg.async_poll_interval)
        status = _async_refresh_status(cfg, run_id, async_cfg)
        if status.needs_state_refresh:
            return _step_for_async_refresh(cfg, status, reason="results completed")
        if status.pending_total <= 0:
            return _step_for_async_refresh(cfg, status, reason="async work completed")
        if status.pending_total != last_pending:
            print(f"  async: still pending ({status.pending_summary()})")
            last_pending = status.pending_total

    status = _async_refresh_status(cfg, run_id, async_cfg)
    print(f"  async: wait timed out ({status.pending_summary()}); leaving perception for a later tick")
    return False


def _current_run_id(cfg: RunnerConfig) -> str:
    try:
        health = _get(cfg, "/health")
    except httpx.HTTPError:
        return ""
    return str(health.get("run_id") or "")


def _async_refresh_status(cfg: RunnerConfig, run_id: str, async_cfg: dict[str, Any]) -> AsyncRefreshStatus:
    events = _run_events(cfg, run_id)
    event_types = [str(e.get("type") or "") for e in events]

    groups: list[AsyncGroupStatus] = []
    for raw_group in async_cfg.get("pending_event_groups") or []:
        if not isinstance(raw_group, dict):
            continue
        name = str(raw_group.get("name") or "work")
        queued_events = [str(e) for e in raw_group.get("queued_events") or []]
        terminal_events = [str(e) for e in raw_group.get("terminal_events") or []]
        groups.append(
            AsyncGroupStatus(
                name=name,
                queued=sum(event_types.count(event_type) for event_type in queued_events),
                terminal=sum(event_types.count(event_type) for event_type in terminal_events),
            )
        )

    refresh_cfg = async_cfg.get("refresh_when") or {}
    refresh_rows: int | None = None
    state_entities: int | None = None
    if isinstance(refresh_cfg, dict):
        rows_cfg = refresh_cfg.get("rows_gt_state_entities") or {}
        if isinstance(rows_cfg, dict):
            path_tmpl = rows_cfg.get("path")
            entity_type = rows_cfg.get("entity_type")
            if isinstance(path_tmpl, str) and isinstance(entity_type, str):
                refresh_rows = _row_count(Path(_render_template(cfg, path_tmpl)))
                state_entities = _state_entity_count(cfg, run_id, entity_type)

    return AsyncRefreshStatus(groups=groups, refresh_rows=refresh_rows, state_entities=state_entities)


def _run_events(cfg: RunnerConfig, run_id: str) -> list[dict[str, Any]]:
    try:
        data = _get(cfg, f"/api/runs/{run_id}/stream?kind=event&limit=20000")
    except httpx.HTTPError:
        return []
    events = data.get("events") or []
    return [e for e in events if isinstance(e, dict)]


def _row_count(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return 0
    return sum(1 for line in lines[1:] if line.strip())


def _state_entity_count(cfg: RunnerConfig, run_id: str, entity_type: str) -> int:
    try:
        data = _get(cfg, f"/api/runs/{run_id}/state")
    except httpx.HTTPError:
        return 0
    entities = data.get("entities") or []
    return sum(1 for e in entities if isinstance(e, dict) and e.get("type") == entity_type)


def _step_for_async_refresh(cfg: RunnerConfig, status: AsyncRefreshStatus, *, reason: str) -> bool:
    step = _post(cfg, "/api/tick/step", {})
    extra = ""
    if status.refresh_rows is not None and status.state_entities is not None:
        extra = f" rows={status.refresh_rows} state_entities={status.state_entities}"
    print(f"  async refresh: {reason}; tick={step.get('tick')}{extra}")
    return True


def _run_agent_activations(cfg: RunnerConfig) -> None:
    if cfg.parallel:
        workers = min(len(cfg.agents), max(1, os.cpu_count() or 1))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_run_one_agent, cfg, aid): aid for aid in cfg.agents}
            for fut in concurrent.futures.as_completed(futs):
                _print_agent_result(futs[fut], fut.result())
    else:
        for agent_id in cfg.agents:
            _print_agent_result(agent_id, _run_one_agent(cfg, agent_id))


def _run_one_agent(cfg: RunnerConfig, agent_id: str) -> subprocess.CompletedProcess[str]:
    agent_cwd = _agent_cwd(cfg, agent_id)
    prompt = _activation_prompt(cfg, agent_id, agent_cwd)
    output_dir = cfg.workspace / ".codex-runner"
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / f"{agent_id}-{int(time.time() * 1000)}.txt"

    cmd = _codex_command(cfg, output_file, cd=agent_cwd)
    env = os.environ.copy()
    env.update(
        {
            "WORLDSEED_WORKSPACE": str(cfg.workspace),
            "WORLDSEED_AGENT_ID": agent_id,
            "WORLDSEED_URL": cfg.base_url,
        }
    )
    env.update(_codex_agent_env(cfg, agent_id, agent_cwd))
    return subprocess.run(
        cmd,
        input=prompt,
        text=True,
        capture_output=True,
        cwd=str(agent_cwd),
        env=env,
        timeout=cfg.agent_timeout,
        check=False,
    )


def _codex_command(
    cfg: RunnerConfig,
    output_file: Path,
    *,
    output_schema: Path | None = None,
    cd: Path | None = None,
) -> list[str]:
    cd = cd or cfg.repo_root
    cmd = [
        "codex",
        "exec",
        "--cd",
        str(cd),
        "--add-dir",
        str(cfg.workspace),
        "--output-last-message",
        str(output_file),
    ]
    if output_schema is not None:
        cmd.extend(["--output-schema", str(output_schema)])
    if cfg.model:
        cmd.extend(["--model", cfg.model])
    if cfg.dangerous_bypass:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.append("--full-auto")
    cmd.append("-")
    return cmd


def _codex_command_preview(cfg: RunnerConfig, agent_id: str) -> str:
    output = cfg.workspace / ".codex-runner" / f"{agent_id}-TIMESTAMP.txt"
    return "  " + " ".join(shlex.quote(p) for p in _codex_command(cfg, output, cd=_agent_cwd(cfg, agent_id)))


def _run_dm_activation(
    cfg: RunnerConfig,
    req: dict[str, Any],
    *,
    attempt: int,
    last_error: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any] | None]:
    output_dir = cfg.workspace / ".codex-runner"
    output_dir.mkdir(exist_ok=True)
    output_file = output_dir / f"dm-{req.get('id', 'request')}-{int(time.time() * 1000)}.json"

    cmd = _codex_command(cfg, output_file)
    env = os.environ.copy()
    env.update(
        {
            "WORLDSEED_WORKSPACE": str(cfg.workspace),
            "WORLDSEED_AGENT_ID": "dm",
            "WORLDSEED_URL": cfg.base_url,
        }
    )
    proc = subprocess.run(
        cmd,
        input=_dm_prompt(req, attempt=attempt, last_error=last_error),
        text=True,
        capture_output=True,
        cwd=str(cfg.repo_root),
        env=env,
        timeout=cfg.dm_timeout,
        check=False,
    )
    if proc.returncode != 0:
        return proc, None
    try:
        response = _read_json_output(output_file, proc.stdout)
    except ValueError as exc:
        print(f"  dm parse failed: {exc}", file=sys.stderr)
        return proc, None
    if not isinstance(response.get("narrative"), str):
        return proc, None
    if not isinstance(response.get("effects"), list):
        return proc, None
    return proc, {"narrative": response["narrative"], "effects": response["effects"]}


def _normalize_dm_response(response: dict[str, Any]) -> dict[str, Any]:
    """Tolerate common model aliases while keeping judgment outside Python."""
    effects: list[Any] = response.get("effects") or []
    normalized: list[Any] = []
    for raw in effects:
        if not isinstance(raw, dict):
            normalized.append(raw)
            continue
        effect = dict(raw)
        op = effect.get("operator")
        if op in {"increment", "decrement"} and "amount" in effect:
            effect.setdefault("by", effect.pop("amount"))
        if op == "emit_event":
            if "message" in effect:
                effect.setdefault("detail", effect.pop("message"))
            if "duration" in effect:
                effect.setdefault("ttl", effect.pop("duration"))
        normalized.append(effect)
    return {"narrative": response.get("narrative", ""), "effects": normalized}


def _validate_dm_response_locally(response: dict[str, Any], req: dict[str, Any]) -> str | None:
    from worldseed.models.config_schema import EffectConfig

    dm_config = req.get("dm_config") or {}
    allowed_ops = set(dm_config.get("allowed_ops") or [])
    max_effects = dm_config.get("max_effects")
    effects = response.get("effects") or []
    if max_effects is not None and len(effects) > int(max_effects):
        return f"too many effects: {len(effects)} > {max_effects}"

    for idx, effect in enumerate(effects):
        if not isinstance(effect, dict):
            return f"effect {idx} is not an object"
        op = effect.get("operator")
        if allowed_ops and op not in allowed_ops:
            return f"effect {idx} operator {op!r} not in allowed_ops"
        try:
            EffectConfig(**effect)
        except Exception as exc:
            return f"effect {idx} schema invalid: {exc}"
    return None


def _read_json_output(output_file: Path, stdout: str) -> dict[str, Any]:
    raw = output_file.read_text(encoding="utf-8") if output_file.exists() else stdout
    raw = raw.strip()
    if not raw:
        raw = stdout.strip()
    if raw.startswith("```"):
        raw = raw.removeprefix("```json").removeprefix("```").strip()
        raw = raw.removesuffix("```").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(raw):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(raw[idx:])
                break
            except json.JSONDecodeError:
                continue
        else:
            raise ValueError("no JSON object found")
    if not isinstance(parsed, dict):
        raise ValueError("JSON output is not an object")
    return parsed


def _dm_prompt(req: dict[str, Any], *, attempt: int, last_error: str) -> str:
    dm_config = req.get("dm_config") or {}
    allowed_ops = dm_config.get("allowed_ops") or []
    max_effects = dm_config.get("max_effects")
    scope = dm_config.get("scope")
    retry_block = f"\nPrevious attempt failed: {last_error}\n" if last_error else ""
    return f"""You are the WorldSeed DM for exactly one pending request.

Resolve the request by judging the scene situation, then return one JSON object
matching this schema:
{{
  "narrative": "short DM narration",
  "effects": [{{"operator": "...", "...": "..."}}]
}}

Rules:
- Return JSON only. No markdown, no code fence, no commentary outside JSON.
- Use only these effect operators: {json.dumps(allowed_ops)}.
- Use at most {max_effects} effects.
- Effects must be valid WorldSeed EffectConfig dictionaries.
- Prefer concrete entity ids in effect targets, e.g. "patient_queue.treated".
  You may use DSL refs from the original action context such as "$agent" or
  "$target" only when they are clearly present in ctx.action_params.
- For emit_event, include operator, type, detail, ttl, scope, and push when
  useful. The request dm_config scope is {scope!r}.
- For increment/decrement, use "by" for the numeric amount, not "amount".
- The deterministic action effects/events may already be reflected in
  dm_context.world_state. Add only the DM judgment effects called for by the
  hint and current situation.
- If the correct judgment is narrative-only, return an empty effects array.
{retry_block}
Pending DM request JSON:
{json.dumps(req, ensure_ascii=False, indent=2)}

Attempt: {attempt}
"""


def _activation_prompt(cfg: RunnerConfig, agent_id: str, agent_cwd: Path) -> str:
    agent_md = cfg.workspace / "agents" / agent_id / "AGENT.md"
    trajectory = cfg.workspace / "trajectory.md"
    scenario = cfg.workspace / "scenario.yaml"
    extra_env = _scene_env_hint(cfg, agent_id, agent_cwd)
    edit_scope = _edit_scope_hint(cfg, agent_id, agent_cwd)
    extra_prompt = _codex_activation_instructions(cfg, agent_id, agent_cwd)
    return f"""You are {agent_id}.

Read {agent_md}.
Read {trajectory}.
Read {scenario}, especially the action descriptions and parameter requirements.
Your current working directory is:
  {agent_cwd}

Environment is already set:
  WORLDSEED_WORKSPACE={cfg.workspace}
  WORLDSEED_AGENT_ID={agent_id}
  WORLDSEED_URL={cfg.base_url}
{extra_env}

Use `python3 "$WORLDSEED_WORKSPACE/ws.py" ...` for ws.py calls. The wrapper is
self-contained; do not run dependency syncs just to perceive or act.

{extra_prompt}

Run one WorldSeed actor activation only:
1. Run `python3 "$WORLDSEED_WORKSPACE/ws.py" perceive`.
2. Read the live state and `action_options`.
3. Choose one useful legal action that fits your character and does not repeat
   prior completed actions shown in `self_state` or recent events. Check target
   status before acting; prefer unresolved/open targets and avoid actions whose
   preconditions clearly require a non-finalized target.
4. If the action requires file references, write the required workspace-relative
   files first at the paths described by the scene action schema and pass those
   paths in the relevant action parameters. JSONL is only an optional machine
   index, not the main collaboration document.
5. Submit it with `python3 "$WORLDSEED_WORKSPACE/ws.py" act ...`, or `publish`
   only if you also create an optional lane index row.
6. Update status.
7. Return a short summary: action submitted, handoff document if any, next
   intent, blockers.

Do not wait for the next tick. {edit_scope}
"""


def _scene_env_hint(cfg: RunnerConfig, agent_id: str, agent_cwd: Path) -> str:
    hint = cfg.codex_config.get("env_hint")
    return _render_template(cfg, str(hint), agent_id=agent_id, agent_cwd=agent_cwd) if hint else ""


def _edit_scope_hint(cfg: RunnerConfig, agent_id: str, agent_cwd: Path) -> str:
    hint = cfg.codex_config.get("edit_scope_hint")
    if hint:
        return _render_template(cfg, str(hint), agent_id=agent_id, agent_cwd=agent_cwd)
    return (
        "Do not edit files outside your own private area and new shared files "
        "explicitly required by the scene action schema:\n"
        f"{cfg.workspace}/agents/{agent_id}/\n"
        f"{cfg.workspace}/shared/ (create only the workspace-relative files requested by the action)"
    )


def _agent_cwd(cfg: RunnerConfig, agent_id: str) -> Path:
    cwd_cfg = cfg.codex_config.get("cwd") or {}
    if isinstance(cwd_cfg, dict) and cwd_cfg.get("mode") == "git_worktree_per_agent":
        root = _codex_cwd_root(cfg)
        if root is None:
            raise SystemExit("codex cwd root is not configured or does not exist")
        return _ensure_git_worktree(cwd_cfg, root, agent_id)
    return cfg.repo_root


def _codex_cwd_root(cfg: RunnerConfig) -> Path | None:
    cwd_cfg = cfg.codex_config.get("cwd") or {}
    if not isinstance(cwd_cfg, dict):
        return None
    raw = cwd_cfg.get("root")
    env_name = cwd_cfg.get("root_env")
    if not raw and isinstance(env_name, str):
        raw = os.environ.get(env_name)
    if not raw:
        return None
    root = Path(str(raw)).expanduser().resolve()
    main_subdir = str(cwd_cfg.get("main_subdir") or "")
    git_dir = root / main_subdir / ".git" if main_subdir else root / ".git"
    return root if git_dir.exists() else None


def _ensure_git_worktree(cwd_cfg: dict[str, Any], root: Path, agent_id: str) -> Path:
    main_subdir = str(cwd_cfg.get("main_subdir") or "")
    worktrees_subdir = str(cwd_cfg.get("worktrees_subdir") or "worktrees")
    base_ref = str(cwd_cfg.get("base_ref") or "HEAD")
    branch_prefix = str(cwd_cfg.get("branch_prefix") or "codex/")
    main = root / main_subdir if main_subdir else root
    worktree = root / worktrees_subdir / agent_id
    if worktree.exists():
        return worktree

    worktree.parent.mkdir(parents=True, exist_ok=True)
    branch = f"{branch_prefix}{agent_id}"
    create = subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-b", branch, str(worktree), base_ref],
        text=True,
        capture_output=True,
        check=False,
    )
    if create.returncode == 0:
        return worktree

    attach = subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(worktree), branch],
        text=True,
        capture_output=True,
        check=False,
    )
    if attach.returncode == 0:
        return worktree

    detail = (attach.stderr or create.stderr or "").strip()
    raise SystemExit(f"failed to create Codex worktree for {agent_id}: {detail}")


def _codex_agent_env(cfg: RunnerConfig, agent_id: str, agent_cwd: Path) -> dict[str, str]:
    raw_env = cfg.codex_config.get("env") or {}
    if not isinstance(raw_env, dict):
        return {}
    rendered: dict[str, str] = {}
    for key, value in raw_env.items():
        if isinstance(key, str) and isinstance(value, str):
            rendered[key] = _render_template(cfg, value, agent_id=agent_id, agent_cwd=agent_cwd)
    return rendered


def _codex_activation_instructions(cfg: RunnerConfig, agent_id: str, agent_cwd: Path) -> str:
    raw = cfg.codex_config.get("activation_instructions")
    return _render_template(cfg, str(raw), agent_id=agent_id, agent_cwd=agent_cwd) if raw else ""


def _codex_describe(cfg: RunnerConfig) -> list[str]:
    raw = cfg.codex_config.get("describe") or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [_render_template(cfg, str(line)) for line in raw]


def _render_template(
    cfg: RunnerConfig,
    text: str,
    *,
    agent_id: str = "",
    agent_cwd: Path | None = None,
) -> str:
    root = _codex_cwd_root(cfg)
    values = {
        "workspace": str(cfg.workspace),
        "repo_root": str(cfg.repo_root),
        "agent_id": agent_id,
        "agent_cwd": str(agent_cwd or ""),
        "cwd_root": str(root or ""),
    }
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _print_agent_result(agent_id: str, proc: subprocess.CompletedProcess[str]) -> None:
    status = "ok" if proc.returncode == 0 else f"exit={proc.returncode}"
    print(f"  agent {agent_id}: {status}")
    if proc.stdout.strip():
        print(_indent_tail(proc.stdout, "    stdout: "))
    if proc.stderr.strip():
        print(_indent_tail(proc.stderr, "    stderr: ", max_lines=8), file=sys.stderr)


def _print_dm_result(request_id: str, proc: subprocess.CompletedProcess[str]) -> None:
    status = "ok" if proc.returncode == 0 else f"exit={proc.returncode}"
    print(f"  dm {request_id}: {status}")
    if proc.stdout.strip():
        print(_indent_tail(proc.stdout, "    stdout: ", max_lines=8))
    if proc.stderr.strip():
        print(_indent_tail(proc.stderr, "    stderr: ", max_lines=8), file=sys.stderr)


def _indent_tail(text: str, prefix: str, *, max_lines: int = 12) -> str:
    lines = text.strip().splitlines()
    if len(lines) > max_lines:
        lines = ["..."] + lines[-max_lines:]
    return "\n".join(prefix + line for line in lines)
