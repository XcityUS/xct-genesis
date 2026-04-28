"""Workspace scaffolding for `worldseed run` and `bootstrap_run.py`.

Reads a scenario.yaml, creates the run workspace folder, copies the agent
runtime helper (`ws.py`), and writes per-agent `AGENT.md` + `status.json`.
Generic — does not assume specific agent roles.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WS_PY = REPO_ROOT / "scripts" / "worldseed_agent.py"

AGENT_MD_TEMPLATE = """# {agent_id}

{identity}

## Goal

{goals_block}

## Personality

{personality}

## Drives — your operating constraints

{drives_block}

## How to publish (your stable interface)

Use the workspace wrapper from the shell:

```bash
python3 "$WORLDSEED_WORKSPACE/ws.py" perceive
python3 "$WORLDSEED_WORKSPACE/ws.py" status --state working --focus "what you are doing"
python3 "$WORLDSEED_WORKSPACE/ws.py" publish ACTION --lane FILE.jsonl --row '{{...}}' key=value
```

Env vars `WORLDSEED_WORKSPACE` and `WORLDSEED_AGENT_ID={agent_id}` are set by
MAIN before you run. You are already activated by MAIN/bootstrap. Do not
register yourself or pass tokens.

## Your lane

`agents/{agent_id}/`:

- `*.jsonl` — your append-only artifact history (you choose the filenames)
- `files/` — your binary outputs (images, html, audio, etc)
- `scratch/` — private drafts, throw-away scripts; nobody else reads this

## Actions available in this scene

{actions_block}

Cross-artifact references use a `target_artifact_id` field (or whatever the
action defines). Add references inside the lane row body so final presentation
can join them visually.

## When to stop

When your current unit is done, update status, then return a short summary:
what you did, artifact ids produced, next intent, blockers.

## Hard rules

- Never write secrets (API keys, tokens) into any workspace file.
- Do not run register. MAIN/bootstrap activates agents before spawning them.
- Never write outside `agents/{agent_id}/`.
- Lane jsonl files are append-only. New versions of an artifact = new id with
  a `revised_from` field referencing the old id.
- Do not edit other agents' lane files.
"""


def _format_block(items: list[str], bullet: str = "- ", empty: str = "_(none specified)_") -> str:
    if not items:
        return empty
    return "\n".join(f"{bullet}{item}" for item in items)


def _format_actions(actions: dict[str, Any]) -> str:
    if not actions:
        return "_(no actions declared)_"
    out: list[str] = []
    for name, cfg in actions.items():
        params = cfg.get("params") or []
        param_names = ", ".join(p.get("name", "?") for p in params)
        desc = cfg.get("description") or ""
        out.append(f"### `{name}({param_names})`\n{desc}")
        if params:
            for p in params:
                req = " *(required)*" if p.get("required") else ""
                pdesc = p.get("description") or ""
                out.append(f"- `{p.get('name')}` ({p.get('type', 'any')}){req} — {pdesc}")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _check_condition(cond: dict[str, Any], agent_cfg: dict[str, Any]) -> bool:
    """Evaluate a single `available_to` rule against an agent's config.

    Handles only `{operator: check, left: $agent.X, op: ==/!=, right: ...}`.
    For richer rules, returns True and warns — AGENT.md will list the action
    and the engine remains the source of truth at runtime.
    """
    operator = cond.get("operator")
    left = cond.get("left")
    op = cond.get("op")
    right = cond.get("right")
    if operator == "check" and isinstance(left, str) and left.startswith("$agent.") and op in ("==", "!="):
        value = agent_cfg.get(left.removeprefix("$agent."))
        return value == right if op == "==" else value != right
    print(
        f"WARN: init_workspace can't evaluate available_to rule {cond!r}; "
        "AGENT.md will list the action conservatively. Engine enforces at runtime.",
        file=sys.stderr,
    )
    return True


def _available_actions_for_agent(actions: dict[str, Any], agent_cfg: dict[str, Any]) -> dict[str, Any]:
    visible: dict[str, Any] = {}
    for name, cfg in actions.items():
        rules = cfg.get("available_to")
        if not rules or all(_check_condition(rule, agent_cfg) for rule in rules):
            visible[name] = cfg
    return visible


def _available_actions_by_agent(scenario: dict[str, Any]) -> dict[str, dict[str, Any]]:
    actions = scenario.get("actions") or {}
    return {
        agent["id"]: _available_actions_for_agent(actions, agent)
        for agent in scenario.get("agents") or []
        if agent.get("id")
    }


def _agent_md(agent_id: str, character: dict[str, Any], actions: dict[str, Any]) -> str:
    identity = character.get("identity") or "_(no identity declared in scenario.yaml)_"
    personality = character.get("personality") or "_(no personality declared)_"
    goals = character.get("goals") or []
    drives = character.get("drives") or []
    return AGENT_MD_TEMPLATE.format(
        agent_id=agent_id,
        identity=identity,
        personality=personality,
        goals_block=_format_block(goals, bullet="- "),
        drives_block=_format_block(drives, bullet="- "),
        actions_block=_format_actions(actions),
    )


def _default_status() -> dict[str, Any]:
    return {
        "state": "idle",
        "focus": "",
        "blockers": [],
        "updated_at": datetime.now(UTC).isoformat(),
    }


def _brief_objective(scenario: dict[str, Any]) -> str | None:
    for entity in scenario.get("entities") or []:
        if isinstance(entity, dict) and entity.get("type") == "brief":
            obj = entity.get("objective")
            return obj if isinstance(obj, str) else None
    return None


def _default_manifest(scenario: dict[str, Any], agent_ids: list[str]) -> dict[str, Any]:
    scene = scenario.get("scene") or {}
    objective = _brief_objective(scenario) or scene.get("description") or "_(no objective in scenario)_"
    return {
        "scenario_id": scene.get("id") or "unknown",
        "objective": objective,
        "agents": agent_ids,
        "story_ref": "story.md",
        "created_at": datetime.now(UTC).isoformat(),
    }


def _default_story(scenario: dict[str, Any]) -> str:
    scene = scenario.get("scene") or {}
    objective = _brief_objective(scenario) or scene.get("description") or "WorldSeed run"
    return f"""# Run Story

Objective: {objective}

This file is the user-facing narrative for the run. MAIN or a curator should
update it near the end after reading the artifact lanes, critiques, selections,
and final package.

Suggested sections:

- What happened
- What each role produced
- Which branches diverged
- Which critiques changed the direction
- What was revised
- What was selected or rejected
- Final artifact refs
"""


def init_workspace(scenario_path: Path, workspace: Path, *, force: bool = False) -> dict[str, Any]:
    scenario_path = scenario_path.expanduser().resolve()
    workspace = workspace.expanduser().resolve()
    if not scenario_path.is_file():
        raise FileNotFoundError(f"scenario not found: {scenario_path}")

    scenario = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    agents = scenario.get("agents") or []
    actions_by_agent = _available_actions_by_agent(scenario)
    agent_ids = [a["id"] for a in agents if a.get("id")]

    workspace.mkdir(parents=True, exist_ok=True)

    if not WS_PY.is_file():
        raise FileNotFoundError(f"missing source ws.py at {WS_PY}")
    shutil.copy2(WS_PY, workspace / "ws.py")
    shutil.copy2(scenario_path, workspace / "scenario.yaml")

    manifest_path = workspace / "manifest.json"
    if force or not manifest_path.exists():
        manifest_path.write_text(json.dumps(_default_manifest(scenario, agent_ids), indent=2, ensure_ascii=False))

    (workspace / "deliverable").mkdir(exist_ok=True)

    story_path = workspace / "story.md"
    if force or not story_path.exists():
        story_path.write_text(_default_story(scenario), encoding="utf-8")

    written = []
    for agent_cfg in agents:
        aid = agent_cfg.get("id")
        if not aid:
            continue
        adir = workspace / "agents" / aid
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "files").mkdir(exist_ok=True)
        (adir / "scratch").mkdir(exist_ok=True)

        agent_md = adir / "AGENT.md"
        if force or not agent_md.exists():
            agent_md.write_text(
                _agent_md(aid, agent_cfg.get("character") or {}, actions_by_agent.get(aid, {})),
                encoding="utf-8",
            )

        status_json = adir / "status.json"
        if force or not status_json.exists():
            status_json.write_text(json.dumps(_default_status(), indent=2, ensure_ascii=False))
        written.append(aid)

    return {
        "workspace": str(workspace),
        "scenario": str(scenario_path),
        "agents": written,
        "ws_py_copied": True,
    }
