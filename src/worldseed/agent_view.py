"""Agent-facing action discovery — build action options + enum filtering.

These helpers compute what actions an agent can see and which `enum_from`
values to surface for entity_ref params. Pure-ish: only reads engine state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.engine.action_policy import apply_enum_filter, blocking_action_names

if TYPE_CHECKING:
    from worldseed.world import WorldEngine


def build_action_options(engine: WorldEngine, agent_id: str) -> dict[str, dict[str, Any]]:
    """Compact `{action_name: {param_name: [enum_values] | "type"}}` for one agent.

    `$visible` reads from the agent's inbox snapshot (already computed by the
    perceiver), so this does not re-evaluate visibility DSL.
    """
    from worldseed.dsl.path_resolver import resolve

    visible_ids: list[str] | None = None
    available = engine.actions_available_to(agent_id)
    blocking = set(blocking_action_names(engine._config, engine.state, agent_id, engine.tick))
    if blocking:
        available &= blocking
    options: dict[str, dict[str, Any]] = {}
    ctx = {"agent_id": agent_id, "action_params": {}, "tick": engine.tick}

    for name, action_cfg in engine._config.actions.items():
        if name not in available:
            continue

        params: dict[str, Any] = {}
        skip_action = False
        for p in action_cfg.params:
            if p.enum_from and p.type == "entity_ref":
                if p.enum_from == "$visible":
                    if visible_ids is None:
                        inbox = engine.inbox_manager.get_or_create(agent_id)
                        state = inbox._current_state
                        if state is not None:
                            visible_ids = sorted(
                                list(state.visible_entities.keys()) + list(state.visible_agents.keys())
                            )
                        else:
                            visible_ids = []
                    filtered = list(visible_ids)
                    if p.enum_filter and filtered:
                        filtered = apply_enum_filter(engine.state, filtered, p.enum_filter)
                    params[p.name] = filtered if filtered else p.type
                else:
                    val = resolve(p.enum_from, engine.state, ctx)
                    if isinstance(val, list):
                        resolved = [str(v) for v in val]
                    elif isinstance(val, str):
                        resolved = [val]
                    else:
                        resolved = []
                    if p.enum_filter and resolved:
                        resolved = apply_enum_filter(engine.state, resolved, p.enum_filter)
                    if p.required and not resolved:
                        skip_action = True
                        break
                    params[p.name] = resolved if resolved else p.type
            else:
                params[p.name] = p.type
        if skip_action:
            continue
        options[name] = params
    return options
