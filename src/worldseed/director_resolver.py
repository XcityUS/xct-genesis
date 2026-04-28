"""External DM resolve — apply a watcher-supplied judgment to a queued request.

Reads a PendingDMRequest, validates the supplied effects through the same
pipeline as in-process DM (validate / sanitize / snapshot / execute / rollback /
narrative), then marks the request resolved or failed in the director queue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from worldseed.world import WorldEngine


def resolve(
    engine: WorldEngine,
    request_id: str,
    narrative: str,
    effects_raw: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Apply an external DM judgment. Returns (ok, reason)."""
    from worldseed.engine.dm_resolver import (
        apply_dm_response,
        sanitize_dm_effects,
        validate_dm_effects,
    )
    from worldseed.models.config_schema import DMConfig, EffectConfig
    from worldseed.protocol.dm import DMResponse

    director = engine.director_runtime()
    req = director.get_dm_request(request_id)
    if req is None:
        return False, f"DM request '{request_id}' not found"
    if req.status != "pending":
        return False, f"DM request status is {req.status}"

    try:
        effects = [EffectConfig(**raw) for raw in effects_raw]
    except Exception as exc:
        director.fail_dm_request(request_id, f"effect schema invalid: {exc}")
        return False, f"effect schema invalid: {exc}"

    dm_config = DMConfig(**req.dm_config)
    valid, reason = validate_dm_effects(effects, dm_config, engine.state)
    if not valid:
        director.fail_dm_request(request_id, f"validation: {reason}")
        return False, f"validation: {reason}"
    sanitize_dm_effects(effects)

    response = DMResponse(narrative=narrative, effects=effects)
    recipient = req.actor_agent_id if req.source_type == "action" else None
    narrative_source = "dm" if req.source_type == "action" else req.source_type

    # Carry the actor through the apply ctx so emit_event effects attribute
    # to the original actor instead of "system" — matches in-process semantics.
    apply_ctx: dict[str, Any] = {"agent_id": req.actor_agent_id or "", "tick": engine.tick}
    ok = apply_dm_response(
        response=response,
        store=engine.state,
        event_log=engine.event_log,
        ctx=apply_ctx,
        tick=engine.tick,
        dm_scope=dm_config.scope,
        narrative_recipient=recipient,
        narrative_event_type="dm_narrative",
        narrative_source=narrative_source,
        inbox_manager=engine.inbox_manager,
    )
    if not ok:
        director.fail_dm_request(request_id, "effect_execution_failed")
        return False, "effect_execution_failed"

    director.mark_dm_resolved(
        request_id,
        {
            "narrative": narrative,
            "effects": [e.model_dump(exclude_none=True) for e in effects],
        },
    )
    engine.refresh_perception_and_observe()
    return True, "ok"
