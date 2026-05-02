"""Action-level policy helpers shared by discovery and validation."""

from __future__ import annotations

from itertools import product
from typing import Any

from worldseed.dsl.path_resolver import resolve
from worldseed.dsl.preconditions import evaluate as evaluate_precondition
from worldseed.engine.state_store import StateStore
from worldseed.models.config_schema import ActionConfig, ParamConfig, SceneConfig


def blocking_action_names(
    config: SceneConfig,
    store: StateStore,
    agent_id: str,
    tick: int,
    *,
    exclude: set[str] | None = None,
) -> list[str]:
    """Return blocking actions that currently have at least one legal target."""
    exclude = exclude or set()
    names: list[str] = []
    for name, action_cfg in config.actions.items():
        if name in exclude or not action_cfg.blocks_when_available:
            continue
        if action_has_legal_option(action_cfg, store, agent_id, tick):
            names.append(name)
    return names


def action_has_legal_option(
    action_cfg: ActionConfig,
    store: StateStore,
    agent_id: str,
    tick: int,
) -> bool:
    """Whether an action is available for this agent with some legal params.

    Non-entity params such as strings/free_text are considered fillable by the
    actor. Entity ref params with `enum_from` must resolve to at least one
    candidate, and preconditions must pass for at least one candidate
    combination.
    """
    base_ctx: dict[str, Any] = {"agent_id": agent_id, "action_params": {}, "tick": tick}

    if action_cfg.available_to is not None:
        if not all(evaluate_precondition(p, store, base_ctx) for p in action_cfg.available_to):
            return False

    entity_choices: list[tuple[str, list[str]]] = []
    for param in action_cfg.params:
        if param.type != "entity_ref":
            continue
        if not param.enum_from:
            # No dynamic target list means the actor may supply an id; policy
            # cannot prove availability from state alone.
            continue
        values = resolve_entity_enum(param, store, base_ctx)
        if param.required and not values:
            return False
        if values:
            entity_choices.append((param.name, values))

    if not entity_choices:
        return all(evaluate_precondition(p, store, base_ctx) for p in action_cfg.preconditions)

    names = [name for name, _values in entity_choices]
    value_lists = [values for _name, values in entity_choices]
    for combo in product(*value_lists):
        ctx = dict(base_ctx)
        ctx["action_params"] = dict(zip(names, combo, strict=True))
        if all(evaluate_precondition(p, store, ctx) for p in action_cfg.preconditions):
            return True
    return False


def resolve_entity_enum(
    param: ParamConfig,
    store: StateStore,
    ctx: dict[str, Any],
) -> list[str]:
    """Resolve an entity_ref enum_from expression against current state."""
    if not param.enum_from or param.enum_from == "$visible":
        return []

    val = resolve(param.enum_from, store, ctx)
    if isinstance(val, list):
        resolved = [str(v) for v in val]
    elif isinstance(val, str):
        resolved = [val]
    else:
        resolved = []

    if param.enum_filter and resolved:
        resolved = apply_enum_filter(store, resolved, param.enum_filter)
    return resolved


def apply_enum_filter(
    store: StateStore,
    entity_ids: list[str],
    enum_filter: dict[str, Any],
) -> list[str]:
    """Keep entity IDs whose entity matches every (key, value) in enum_filter."""
    result: list[str] = []
    for eid in entity_ids:
        entity = store.get(eid)
        if entity is None:
            continue
        match = True
        for key, expected in enum_filter.items():
            if key == "type":
                if entity.type != expected:
                    match = False
                    break
            elif key == "id":
                if entity.id != expected:
                    match = False
                    break
            elif entity.get(key) != expected:
                match = False
                break
        if match:
            result.append(eid)
    return result
