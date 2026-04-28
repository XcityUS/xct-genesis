"""Action dispatch helpers — mechanical-action bookkeeping after RulesEngine runs.

The engine separates "execute the rule" (RulesEngine) from "make the world react
to the result" (this module). Stream recording, action_rejected emission, the
inbox whisper for rejections, and the post-success highlight scan all live here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.engine.inbox import InboxWhisper
from worldseed.models.event import Event

if TYPE_CHECKING:
    from worldseed.engine.rules_engine import ActionResult
    from worldseed.models.action import ActionSubmission
    from worldseed.models.config_schema import ActionConfig
    from worldseed.world import WorldEngine


def apply_mechanical_result(
    engine: WorldEngine,
    action_cfg: ActionConfig,
    submission: ActionSubmission,
    result: ActionResult,
) -> None:
    """Run all the bookkeeping that follows a mechanical action's execution."""
    rec_kwargs: dict[str, Any] = {
        "agent_id": submission.agent_id,
        "action_type": submission.action_type,
        "params": submission.params,
        "success": result.success,
        "reason": result.reason,
    }
    if result.success and action_cfg.highlight:
        rec_kwargs["highlight"] = True
    engine.recorder.record("action", engine.tick, **rec_kwargs)

    if not result.success:
        _record_rejection(engine, submission, result)
        return

    _record_success_highlights(engine)
    engine.refresh_perception_and_observe()


def _record_rejection(
    engine: WorldEngine,
    submission: ActionSubmission,
    result: ActionResult,
) -> None:
    detail = f"{submission.agent_id} tried '{submission.action_type}' but failed: {result.reason}"
    engine.event_log.append(
        Event(
            tick=engine.tick,
            type="action_rejected",
            source=submission.agent_id,
            detail=detail,
            ttl=5,
            scope="admin",
            highlight=True,
        )
    )
    engine.recorder.record("highlight", engine.tick, label=detail, source="action_rejected")
    if engine.inbox_manager is not None:
        inbox = engine.inbox_manager.get_or_create(submission.agent_id)
        inbox.append_whisper(
            InboxWhisper(
                tick=engine.tick,
                source="system",
                detail=f"Your '{submission.action_type}' action failed: {result.reason}",
                type="action_failed",
            )
        )


def _record_success_highlights(engine: WorldEngine) -> None:
    """Stream Layer-2 engine highlights (entity_created, relationship_changed, ...)."""
    seen = engine._tick_engine._recorded_highlight_ids
    for evt in engine.event_log.get_events(since_tick=engine.tick):
        eid = id(evt)
        if evt.highlight and evt.type != "highlight" and eid not in seen:
            seen.add(eid)
            engine.recorder.record(
                "highlight",
                engine.tick,
                label=evt.detail,
                source=evt.type,
            )
