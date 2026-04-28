"""DM Resolution — resolves DM judgments for actions with dm config.

Extracted from rules_engine.py to separate DM concern from action processing.
"""

from __future__ import annotations

import copy
import time as _time
from typing import TYPE_CHECKING, Any

import structlog

from worldseed.dm.builder import GM_RESOLVE_OPS
from worldseed.dsl.effects import execute as execute_effect
from worldseed.models.config_schema import DMConfig, EffectConfig
from worldseed.models.event import Event


def _concise_error(exc: Exception) -> str:
    """Extract a short, user-readable error from a DM exception.

    Instructor wraps errors in verbose XML traces. We dig for the
    root cause (e.g. 'NotFoundError: model not found').
    """
    # Walk the exception chain for the root cause
    root: BaseException = exc
    while root.__cause__ is not None:
        root = root.__cause__
    msg = str(root)
    # Truncate verbose messages (Instructor XML, full tracebacks)
    if len(msg) > 200:
        msg = msg[:200] + "..."
    return f"{type(root).__name__}: {msg}"


if TYPE_CHECKING:
    from worldseed.dm.builder import DMContextBuilder
    from worldseed.dm.providers.base import DMProvider
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.inbox import InboxManager
    from worldseed.engine.state_store import StateStore
    from worldseed.models.action import ActionSubmission
    from worldseed.persistence import NullRecorder, RunRecorder

log = structlog.get_logger()


def sanitize_dm_effects(effects: list[EffectConfig]) -> None:
    """Apply safety defaults to DM-generated effects in place.

    DM models may return decrement without min:0, which can produce
    negative values. This adds implicit min:0 for decrement effects
    that don't explicitly set a min value.
    """
    for effect in effects:
        if effect.operator == "decrement" and effect.min is None:
            effect.min = 0


def validate_dm_effects(
    effects: list[EffectConfig],
    dm_config: DMConfig,
    store: StateStore,
) -> tuple[bool, str]:
    """Validate an entire batch of DM-returned effects.

    Checks:
      1. Total effect count <= dm_config.max_effects
      2. Each effect operator is in dm_config.allowed_ops
      3. Entity existence (per-operator logic)

    Returns (valid, reason). If any check fails the entire batch is invalid.
    """
    if len(effects) > dm_config.max_effects:
        return (
            False,
            f"DM returned {len(effects)} effects, max is {dm_config.max_effects}",
        )

    for effect in effects:
        op = effect.operator

        if op not in dm_config.allowed_ops:
            return False, f"Operator '{op}' not in allowed_ops"

        if op == "emit_event":
            continue

        if op in ("set", "increment", "decrement"):
            if effect.target is None:
                return False, f"'{op}' effect missing target"
            entity_id = effect.target.split(".")[0]
            if not entity_id.startswith("$") and store.get(entity_id) is None:
                return False, f"Entity '{entity_id}' not found for '{op}'"

        elif op == "create_entity":
            if effect.id is None:
                return False, "create_entity missing id"
            if store.get(effect.id) is not None:
                return False, f"Entity '{effect.id}' already exists"

        elif op == "remove_entity":
            if effect.target is None:
                return False, "remove_entity missing target"
            if not effect.target.startswith("$") and store.get(effect.target) is None:
                return False, f"Entity '{effect.target}' not found for remove"

        elif op in ("add_relationship", "remove_relationship"):
            from_id = effect.from_entity
            if from_id is None:
                return False, f"'{op}' missing from_entity"
            if not from_id.startswith("$") and store.get(from_id) is None:
                return False, f"Entity '{from_id}' not found for '{op}'"

        elif op in ("list_append", "list_remove"):
            if effect.target is None:
                return False, f"'{op}' effect missing target"
            entity_id = effect.target.split(".")[0]
            if not entity_id.startswith("$") and store.get(entity_id) is None:
                return False, f"Entity '{entity_id}' not found for '{op}'"

        elif op == "list_pop_random":
            if effect.source is None:
                return False, "list_pop_random missing source"
            if effect.target is None:
                return False, "list_pop_random missing target"
            src_id = effect.source.split(".")[0]
            tgt_id = effect.target.split(".")[0]
            if not src_id.startswith("$") and store.get(src_id) is None:
                return False, f"Entity '{src_id}' not found for list_pop_random source"
            if not tgt_id.startswith("$") and store.get(tgt_id) is None:
                return False, f"Entity '{tgt_id}' not found for list_pop_random target"

        elif op == "for_each":
            # DM should not return for_each — it's a config-level operator
            return False, "for_each is not allowed in DM responses"

    return True, ""


async def resolve_dm(
    action: ActionSubmission,
    dm_config: DMConfig,
    ctx: dict[str, Any],
    tick: int,
    dm_provider: DMProvider,
    dm_builder: DMContextBuilder,
    store: StateStore,
    event_log: EventLog,
    recorder: RunRecorder | NullRecorder | None,
    inbox_manager: InboxManager | None = None,
) -> None:
    """Resolve DM judgment for an action with dm config."""
    dm_ctx = dm_builder.build(action, dm_config, tick)

    max_attempts = 2
    response = None
    last_error = ""

    dm_start = _time.monotonic()
    for attempt in range(max_attempts):
        try:
            response = await dm_provider.judge(dm_ctx)
        except Exception as exc:
            last_error = _concise_error(exc)
            log.warning(
                "dm_call_failed",
                action=action.action_type,
                attempt=attempt + 1,
                error=last_error,
                exc_info=True,
            )
            response = None
            continue

        valid, reason = validate_dm_effects(response.effects, dm_config, store)
        if valid:
            sanitize_dm_effects(response.effects)
            break

        last_error = f"validation: {reason}"
        log.warning(
            "dm_validation_failed",
            action=action.action_type,
            reason=reason,
            attempt=attempt + 1,
        )
        dm_ctx.error_feedback = reason
        response = None
    dm_elapsed = _time.monotonic() - dm_start

    if response is None:
        fail_narrative = f"(DM failed: {last_error})" if last_error else "(DM call failed)"
        if recorder is not None:
            recorder.record(
                "dm_call",
                tick,
                action=action.action_type,
                agent_id=action.agent_id,
                params=action.params,
                hint=dm_config.hint,
                target_history=dm_ctx.target_history or "",
                effects=[],
                narrative=fail_narrative,
                tokens_in=0,
                tokens_out=0,
                elapsed_s=round(dm_elapsed, 3),
                failed=True,
            )
        emit_fallback_narrative(
            action.agent_id,
            tick,
            inbox_manager=inbox_manager,
        )
        return

    # Record DM call with real per-call token counts
    if recorder is not None:
        recorder.record(
            "dm_call",
            tick,
            action=action.action_type,
            agent_id=action.agent_id,
            params=action.params,
            hint=dm_config.hint,
            target_history=dm_ctx.target_history or "",
            effects=[e.model_dump(exclude_none=True) for e in response.effects],
            narrative=response.narrative,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            elapsed_s=round(dm_elapsed, 3),
        )

    ok = apply_dm_response(
        response=response,
        store=store,
        event_log=event_log,
        ctx=ctx,
        tick=tick,
        dm_scope=dm_config.scope,
        narrative_recipient=action.agent_id,
        narrative_event_type="dm_narrative",
        narrative_source="dm",
        inbox_manager=inbox_manager,
    )
    if not ok:
        log.warning("dm_effects_rollback", action=action.action_type, exc_info=True)
        emit_fallback_narrative(action.agent_id, tick, inbox_manager=inbox_manager)


async def resolve_gm_command(
    text: str,
    tick: int,
    dm_provider: DMProvider,
    dm_builder: DMContextBuilder,
    store: StateStore,
    event_log: EventLog,
    recorder: RunRecorder | NullRecorder | None,
    target_entity_id: str | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Resolve a GM natural-language command via DM.

    Returns result dict with success/failure info for recording.
    """
    dm_ctx = dm_builder.build_gm_resolve(text, tick, target_entity_id)

    # Synthetic DMConfig for validation
    gm_resolve_config = DMConfig(
        hint="",
        scope="admin",
        allowed_ops=GM_RESOLVE_OPS,
        max_effects=10,
    )

    max_attempts = 2
    response = None
    last_error = ""

    dm_start = _time.monotonic()
    for attempt in range(max_attempts):
        try:
            response = await dm_provider.judge(dm_ctx)
        except Exception as exc:
            last_error = _concise_error(exc)
            log.warning(
                "gm_resolve_dm_failed",
                text=text,
                attempt=attempt + 1,
                error=last_error,
                exc_info=True,
            )
            response = None
            continue

        valid, reason = validate_dm_effects(response.effects, gm_resolve_config, store)
        if valid:
            sanitize_dm_effects(response.effects)
            break

        last_error = f"validation: {reason}"
        log.warning(
            "gm_resolve_validation_failed",
            text=text,
            reason=reason,
            attempt=attempt + 1,
        )
        dm_ctx.error_feedback = reason
        response = None
    dm_elapsed = _time.monotonic() - dm_start

    fail_reason = f"DM failed: {last_error}" if last_error else "DM resolution failed"
    result: dict[str, Any] = {"request_id": request_id, "text": text}

    if response is None:
        result["success"] = False
        result["reason"] = fail_reason
        if recorder is not None:
            recorder.record(
                "gm_resolve",
                tick,
                request_id=request_id,
                text=text,
                effects=[],
                narrative=f"({fail_reason})",
                elapsed_s=round(dm_elapsed, 3),
                success=False,
            )
        # Emit admin event so dashboard sees the failure
        event_log.append(
            Event(
                tick=tick,
                type="gm_resolve_failed",
                source="gm",
                detail=f"Failed to resolve: {text}",
                ttl=5,
                scope="admin",
            )
        )
        return result

    # Record success
    if recorder is not None:
        recorder.record(
            "gm_resolve",
            tick,
            request_id=request_id,
            text=text,
            effects=[e.model_dump(exclude_none=True) for e in response.effects],
            narrative=response.narrative,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            elapsed_s=round(dm_elapsed, 3),
            success=True,
        )

    ok = apply_dm_response(
        response=response,
        store=store,
        event_log=event_log,
        ctx={"recorder": recorder},
        tick=tick,
        dm_scope=gm_resolve_config.scope,
        narrative_recipient=None,
        narrative_event_type="gm_resolve",
        narrative_source="gm",
        narrative_ttl=5,
        inbox_manager=None,
    )
    if not ok:
        log.warning("gm_resolve_effects_rollback", text=text, exc_info=True)
        result["success"] = False
        result["reason"] = "Effect execution failed, rolled back"
        return result

    result["success"] = True
    result["effects_count"] = len(response.effects)
    result["narrative"] = response.narrative
    return result


def emit_fallback_narrative(
    agent_id: str,
    tick: int,
    inbox_manager: InboxManager | None = None,
) -> None:
    """Emit fallback narrative when DM fails. Delivers to actor as whisper."""
    if inbox_manager is None:
        log.warning("emit_fallback_no_inbox", agent=agent_id)
        return
    from worldseed.engine.inbox import InboxWhisper

    inbox = inbox_manager.get_or_create(agent_id)
    inbox.append_whisper(
        InboxWhisper(
            tick=tick,
            source="dm",
            detail="The outcome is unclear.",
            type="dm_narrative",
        )
    )


def snapshot_entities(
    store: StateStore,
    effects: list[EffectConfig],
) -> dict[str, dict[str, Any]]:
    """Snapshot properties of entities affected by DM effects."""
    snapshots: dict[str, dict[str, Any]] = {}
    for effect in effects:
        entity_id: str | None = None
        if effect.target is not None:
            entity_id = effect.target.split(".")[0]
        elif effect.from_entity is not None:
            entity_id = effect.from_entity
        if entity_id and not entity_id.startswith("$"):
            entity = store.get(entity_id)
            if entity is not None and entity_id not in snapshots:
                snapshots[entity_id] = copy.deepcopy(dict(entity.data))
    return snapshots


def restore_snapshots(
    store: StateStore,
    snapshots: dict[str, dict[str, Any]],
) -> None:
    """Restore entity properties from snapshots."""
    for eid, props in snapshots.items():
        entity = store.get(eid)
        if entity is not None:
            entity.data = props


def apply_dm_response(
    *,
    response: Any,  # DMResponse
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
    dm_scope: str,
    narrative_recipient: str | None,
    narrative_event_type: str = "dm_narrative",
    narrative_source: str = "dm",
    narrative_ttl: int = 3,
    inbox_manager: InboxManager | None = None,
) -> bool:
    """Apply DM effects atomically and deliver narrative. Returns success.

    Caller is responsible for validating and sanitizing effects beforehand.
    On failure, state is rolled back and the narrative is NOT emitted —
    the caller decides how to surface the failure (whisper, log, result dict).
    """
    snapshots = snapshot_entities(store, response.effects)
    dm_ctx_effects = {**ctx, "dm_scope": dm_scope}
    try:
        for effect in response.effects:
            execute_effect(effect, store, event_log, dm_ctx_effects, tick)
    except Exception:
        restore_snapshots(store, snapshots)
        return False

    if response.narrative:
        _deliver_narrative(
            narrative=response.narrative,
            recipient=narrative_recipient,
            event_type=narrative_event_type,
            source=narrative_source,
            scope=dm_scope or "global",
            tick=tick,
            ttl=narrative_ttl,
            event_log=event_log,
            inbox_manager=inbox_manager,
        )
    return True


def _deliver_narrative(
    *,
    narrative: str,
    recipient: str | None,
    event_type: str,
    source: str,
    scope: str,
    tick: int,
    ttl: int,
    event_log: EventLog,
    inbox_manager: InboxManager | None,
) -> None:
    """Whisper to a single recipient, or fall back to a scoped global event."""
    if recipient and inbox_manager is not None:
        from worldseed.engine.inbox import InboxWhisper

        inbox_manager.get_or_create(recipient).append_whisper(
            InboxWhisper(
                tick=tick,
                source=source,
                detail=narrative,
                type=event_type,
            )
        )
    else:
        event_log.append(
            Event(
                tick=tick,
                type=event_type,
                source=source,
                detail=narrative,
                ttl=ttl,
                scope=scope,
            )
        )


async def resolve_consequence_dm(
    consequence_name: str,
    dm_config: DMConfig,
    ctx: dict[str, Any],
    tick: int,
    dm_provider: DMProvider,
    dm_builder: DMContextBuilder,
    store: StateStore,
    event_log: EventLog,
    recorder: RunRecorder | NullRecorder | None,
) -> None:
    """Resolve DM judgment triggered by a consequence.

    Uses the same DM call/validate/apply pattern as action DM,
    but with a synthetic context (no agent, no action).
    """
    from worldseed.models.action import ActionSubmission

    # Build a synthetic action for the DM builder
    synthetic = ActionSubmission(
        agent_id="",
        action_type=f"consequence:{consequence_name}",
        params={},
    )
    dm_ctx = dm_builder.build(synthetic, dm_config, tick)
    dm_ctx.prompt_mode = "consequence"

    max_attempts = 2
    response = None
    last_error = ""

    dm_start = _time.monotonic()
    for attempt in range(max_attempts):
        try:
            response = await dm_provider.judge(dm_ctx)
        except Exception as exc:
            last_error = _concise_error(exc)
            log.warning(
                "consequence_dm_failed",
                consequence=consequence_name,
                attempt=attempt + 1,
                error=last_error,
            )
            response = None
            continue

        valid, reason = validate_dm_effects(response.effects, dm_config, store)
        if valid:
            sanitize_dm_effects(response.effects)
            break

        last_error = f"validation: {reason}"
        log.warning(
            "consequence_dm_validation_failed",
            consequence=consequence_name,
            reason=reason,
            attempt=attempt + 1,
        )
        dm_ctx.error_feedback = reason
        response = None
    dm_elapsed = _time.monotonic() - dm_start

    if response is None:
        log.warning(
            "consequence_dm_all_attempts_failed",
            consequence=consequence_name,
            error=last_error,
        )
        if recorder is not None:
            recorder.record(
                "dm_call",
                tick,
                action=f"consequence:{consequence_name}",
                agent_id="",
                params={},
                hint=dm_config.hint,
                effects=[],
                narrative=f"(Consequence DM failed: {last_error})",
                tokens_in=0,
                tokens_out=0,
                elapsed_s=round(dm_elapsed, 3),
                failed=True,
            )
        return

    if recorder is not None:
        recorder.record(
            "dm_call",
            tick,
            action=f"consequence:{consequence_name}",
            agent_id="",
            params={},
            hint=dm_config.hint,
            effects=[e.model_dump(exclude_none=True) for e in response.effects],
            narrative=response.narrative,
            tokens_in=response.tokens_in,
            tokens_out=response.tokens_out,
            elapsed_s=round(dm_elapsed, 3),
        )

    ok = apply_dm_response(
        response=response,
        store=store,
        event_log=event_log,
        ctx=ctx,
        tick=tick,
        dm_scope=dm_config.scope,
        narrative_recipient=None,
        narrative_event_type="dm_narrative",
        narrative_source="consequence",
        inbox_manager=None,
    )
    if not ok:
        log.warning(
            "consequence_dm_effects_rollback",
            consequence=consequence_name,
            exc_info=True,
        )
