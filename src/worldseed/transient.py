"""Transient-state serialization helpers — extracted from WorldEngine.

Transient state is the in-memory data that must survive pause/resume but is
not part of the entity store: pending action queue, per-agent inbox events
and whispers, think-interval overrides, recent EventLog entries, and the
director-signal queue / checkpoint state. Persistence to `transient.json`
goes through RunRecorder.save_transient.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.engine.inbox import InboxWhisper
from worldseed.models.action import ActionSubmission
from worldseed.models.event import Event

if TYPE_CHECKING:
    from worldseed.world import WorldEngine


def collect(engine: WorldEngine) -> dict[str, Any]:
    """Snapshot transient state into a JSON-serializable dict."""
    te = engine._tick_engine
    inbox_mgr = te._inbox_manager

    inboxes: dict[str, Any] = {}
    if inbox_mgr is not None:
        for aid, inbox in inbox_mgr.all_inboxes().items():
            events = [e.to_dict() for e in inbox.peek_events()]
            whispers = [m.to_dict() for m in inbox._whispers]
            if events or whispers:
                inboxes[aid] = {"events": events, "whispers": whispers}

    pending_actions = [
        {
            "agent_id": a.agent_id,
            "action_type": a.action_type,
            "params": a.params,
            "tick_submitted": a.tick_submitted,
        }
        for a in te._queue._queue
    ]

    intervals = dict(engine.registry._think_intervals)

    recent_events = [
        {
            "tick": e.tick,
            "type": e.type,
            "source": e.source,
            "detail": e.detail,
            "ttl": e.ttl,
            "scope": e.scope,
            "target": e.target,
        }
        for e in te._event_log.get_events()
    ]

    return {
        "inboxes": inboxes,
        "pending_actions": pending_actions,
        "think_intervals": intervals,
        "recent_events": recent_events,
        "event_log_total_appended": te._event_log.total_appended,
        "director": engine._director.to_dict(),
    }


def restore(engine: WorldEngine, data: dict[str, Any]) -> None:
    """Apply a transient snapshot back onto a fresh engine."""
    te = engine._tick_engine
    inbox_mgr = te._inbox_manager

    # Restore inboxes — only whispers, not events. Events are restored into
    # the EventLog and the perceiver re-delivers them on the next tick.
    if inbox_mgr is not None:
        for aid, inbox_data in data.get("inboxes", {}).items():
            inbox = inbox_mgr.get_or_create(aid)
            for m in inbox_data.get("whispers", []):
                inbox.append_whisper(InboxWhisper(**m))
            # Set last_perceive_tick so perceiver doesn't re-deliver already-seen events.
            inbox.last_perceive_tick = engine.tick

    for a in data.get("pending_actions", []):
        te._queue._queue.append(
            ActionSubmission(
                agent_id=a["agent_id"],
                action_type=a["action_type"],
                params=a.get("params", {}),
                tick_submitted=a.get("tick_submitted", 0),
            )
        )

    for aid, interval in data.get("think_intervals", {}).items():
        engine.registry._think_intervals[aid] = interval

    for e in data.get("recent_events", []):
        te._event_log.append(
            Event(
                tick=e["tick"],
                type=e["type"],
                source=e["source"],
                detail=e["detail"],
                ttl=e.get("ttl", 3),
                scope=e.get("scope", "global"),
                target=e.get("target"),
            )
        )

    # Seed the monotonic event counter BEFORE the director restores its
    # cursor — without this, the cursor saved before TTL cleanup would
    # silently suppress checkpoint cadence after resume.
    saved_total = data.get("event_log_total_appended")
    if isinstance(saved_total, int):
        te._event_log.seed_total_appended(saved_total)

    director_data = data.get("director")
    if director_data:
        engine._director.restore(director_data)
