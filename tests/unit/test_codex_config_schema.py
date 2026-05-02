from __future__ import annotations

import pytest
from pydantic import ValidationError

from worldseed.models.config_schema import SceneConfig


def _base_config() -> dict[str, object]:
    return {
        "scene": {"id": "t", "description": "t"},
        "entities": [],
        "actions": {
            "talk": {
                "description": "Talk",
                "params": [],
                "preconditions": [],
                "effects": [],
            }
        },
    }


def test_scene_codex_config_accepts_runner_wiring() -> None:
    raw = _base_config()
    raw["scene"] = {
        "id": "t",
        "description": "t",
        "codex": {
            "cwd": {
                "mode": "git_worktree_per_agent",
                "root_env": "AUTORESEARCH_WORKSPACE",
                "main_subdir": "main",
            },
            "env": {
                "AUTORESEARCH_WORKSPACE": "{cwd_root}",
                "AUTORESEARCH_AGENT_WORKTREE": "{agent_cwd}",
            },
            "describe": ["worktrees under {cwd_root}/worktrees"],
            "async_refresh": {
                "enabled": True,
                "pending_event_groups": [
                    {
                        "name": "experiments",
                        "queued_events": ["experiment_queued"],
                        "terminal_events": ["experiment_completed"],
                    }
                ],
                "refresh_when": {
                    "rows_gt_state_entities": {
                        "path": "{cwd_root}/main/results.tsv",
                        "entity_type": "experiment",
                    }
                },
            },
        },
    }

    config = SceneConfig.model_validate(raw)

    assert config.scene.codex.cwd is not None
    assert config.scene.codex.cwd.mode == "git_worktree_per_agent"
    assert config.scene.codex.async_refresh.enabled is True


def test_scene_codex_config_rejects_unknown_fields() -> None:
    raw = _base_config()
    raw["scene"] = {
        "id": "t",
        "description": "t",
        "codex": {"rootEnv": "AUTORESEARCH_WORKSPACE"},
    }

    with pytest.raises(ValidationError):
        SceneConfig.model_validate(raw)


def test_scene_codex_config_rejects_invalid_cwd_mode() -> None:
    raw = _base_config()
    raw["scene"] = {
        "id": "t",
        "description": "t",
        "codex": {"cwd": {"mode": "git-worktree-per-agent"}},
    }

    with pytest.raises(ValidationError):
        SceneConfig.model_validate(raw)
