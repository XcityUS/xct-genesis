"""Skeleton builder for `worldseed present-skeleton`.

Reads a workspace folder + the matching ~/.worldseed/runs/{run_id}/ event
stream (if present) + the workspace's scenario.yaml + workspace/story.md, and
writes <workspace>/present-skeleton.json with the PilotDataset shape.

Mechanical fields are filled from the workspace contents. Narrative fields
contain literal "TODO: ..." placeholders for MAIN/Codex to overwrite when
shipping the run.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _agent_lane_rows(workspace: Path) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    agents_dir = workspace / "agents"
    if not agents_dir.is_dir():
        return rows
    for agent_dir in sorted(agents_dir.iterdir()):
        if not agent_dir.is_dir():
            continue
        for jsonl_path in sorted(agent_dir.glob("*.jsonl")):
            for row in _read_jsonl(jsonl_path):
                rows.append((agent_dir.name, row))
    return rows


def _event_moments(run_id: str) -> list[dict[str, Any]]:
    stream_path = Path.home() / ".worldseed" / "runs" / run_id / "stream.jsonl"
    moments: list[dict[str, Any]] = []
    for event in _read_jsonl(stream_path):
        if event.get("kind") not in {"action", "wakeup", "whisper", "register"}:
            continue
        label = event.get("type") or event.get("name") or event.get("kind") or ""
        if not isinstance(label, str) or not label:
            continue
        moments.append({
            "tick": event.get("tick"),
            "actor": event.get("agent") or event.get("source") or event.get("kind"),
            "label": f"TODO: 描述「{label}」发生了什么",
        })
        if len(moments) >= 12:
            break
    return moments


def _branches_from_lanes(rows: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    branches: list[dict[str, Any]] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for agent_id, row in rows:
        artifact_id = row.get("artifact_id") or row.get("id") or row.get("target_artifact_id")
        if not isinstance(artifact_id, str):
            continue
        grouped.setdefault(f"{agent_id}/{artifact_id}", []).append(row)
    for letter_index, (key, group) in enumerate(grouped.items()):
        letter = chr(65 + letter_index) if letter_index < 26 else f"X{letter_index}"
        first = group[0]
        branches.append({
            "letter": letter,
            "title": f"TODO: 标题(来自 {key})",
            "status": "chosen" if any(r.get("status") == "selected" for r in group) else "parked",
            "statusLabel": "TODO: 状态标签",
            "thesis": f"TODO: 论点(来自 {first.get('agent_id', key.split('/')[0])} 的 {first.get('artifact_id', '')})",
            "attempts": "TODO: 写出尝试过什么",
            "result": "TODO: 写出结果",
            "decision": "TODO: 写出决定理由",
        })
    if not branches:
        branches.append({
            "letter": "A",
            "title": "TODO: 第一个 deliverable 的标题",
            "status": "chosen",
            "statusLabel": "TODO",
            "thesis": "TODO: 论点",
        })
    return branches


def _panel_from_scenario(scenario: dict[str, Any]) -> list[dict[str, Any]]:
    panel: list[dict[str, Any]] = []
    for agent in scenario.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        character = agent.get("character") or {}
        identity = character.get("identity") or "TODO: 自我介绍"
        panel.append({
            "avatar": agent.get("avatar") or "🧠",
            "name": agent.get("id") or "TODO",
            "bio": identity if isinstance(identity, str) else "TODO: 自我介绍",
        })
    return panel


def _eyebrow(manifest: dict[str, Any], scenario: dict[str, Any]) -> str:
    scene = scenario.get("scene") or {}
    scenario_id = scene.get("id") or manifest.get("scenario_id") or "RUN"
    created = manifest.get("created_at") or datetime.utcnow().isoformat()
    return f"{scenario_id.upper()} · {created[:10]}"


def build_skeleton(workspace: Path) -> dict[str, Any]:
    workspace = workspace.expanduser().resolve()
    manifest_path = workspace / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    scenario = _load_yaml(workspace / "scenario.yaml")
    scene = scenario.get("scene") or {}
    rows = _agent_lane_rows(workspace)
    branches = _branches_from_lanes(rows)
    intro = _read_text(workspace / "story.md")
    moments = _event_moments(workspace.name)
    title = scene.get("title") or scene.get("description") or "TODO: 给这次 run 一个标题"
    subtitle = (
        scene.get("description")
        or manifest.get("objective")
        or "TODO: 一两句话总结这次 run 在解决什么问题"
    )
    rules_list = [rule for rule in (scenario.get("rules") or []) if isinstance(rule, str)]
    rules = rules_list or ["TODO: 这次 run 的硬规则"]

    return {
        "eyebrow": _eyebrow(manifest, scenario),
        "title": title,
        "subtitle": subtitle,
        "meta": f"{workspace.name} · {len(manifest.get('agents') or [])} agents · {len(rows)} lane rows",
        "story": {
            "intro": intro or "TODO: 把 story.md 写好后这里会自动同步;现在请覆盖这段。",
            "moments": moments or [
                {"tick": 0, "actor": "MAIN", "label": "TODO: 第一个事件"},
            ],
        },
        "verdict": {
            "lead": "TODO: 一句话总结结论(选哪个 / 砍哪个 / 为什么)",
            "bullets": ["TODO: 关键证据 1", "TODO: 关键证据 2"],
            "recommend": "TODO: 推荐下一步动作",
            "deliverables": [
                {"icon": "📄", "name": branch["title"], "meta": f"branch {branch['letter']}"}
                for branch in branches[:6]
            ],
        },
        "question": {
            "Topic": scene.get("description") or "TODO: 主题",
            "Scope": "TODO: 数据/资源/约束",
            "Decision": "TODO: 这次 run 要做什么决定",
            "Protocol": "TODO: 流程",
        },
        "panel": _panel_from_scenario(scenario),
        "rules": rules,
        "branchMap": {"shape": "wide" if len(branches) <= 4 else "mixed"},
        "branches": branches,
        "confidence": {
            "tested": ["TODO: 已经验证过的部分"],
            "untested": ["TODO: 还没验证 / 不确定的部分"],
            "next": ["TODO: 下一步要测什么"],
        },
    }


def run(args: argparse.Namespace) -> None:
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"workspace not found: {workspace}")
    skeleton = build_skeleton(workspace)
    output = workspace / "present-skeleton.json"
    output.write_text(json.dumps(skeleton, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {output}")
