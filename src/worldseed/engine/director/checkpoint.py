"""Checkpoint cadence evaluation — pure function on policy + state + recent events.

Engine never summarizes semantically; the payload is deterministic metadata.
An external main agent decides what to do next based on the refs.
"""

from __future__ import annotations

import time
from typing import Any

from worldseed.engine.director.models import (
    CheckpointPolicy,
    CheckpointState,
    DirectorSignal,
)
from worldseed.models.event import Event


def _is_meaningful(event: Event, policy: CheckpointPolicy) -> bool:
    """Filter bookkeeping noise per policy. See `ignore_event_scopes/types`."""
    if event.scope in policy.ignore_event_scopes:
        return False
    return event.type not in policy.ignore_event_types


def evaluate(
    *,
    tick: int,
    monotonic_now: float,
    new_events: list[Event],
    policy: CheckpointPolicy,
    state: CheckpointState,
    new_id: str,
    pending_dm_count: int,
    appended_now: int,
) -> tuple[DirectorSignal | None, CheckpointState]:
    """Return (maybe-signal, advanced state).

    `appended_now` is the EventLog's monotonic total. The cursor stores that
    same total to survive TTL cleanup — slicing by integer index against a
    list that shrinks would miscount once events expire.

    Cadence triggers:
      - meaningful new event count crosses every_events
      - elapsed wall time crosses every_minutes (in seconds)
      - tick delta crosses every_ticks
      - any new event has a type in on_event_types (forced fire)
    """
    advanced = CheckpointState(
        last_signal_tick=state.last_signal_tick,
        last_signal_time=state.last_signal_time,
        last_event_cursor=appended_now,
        events_since_checkpoint=state.events_since_checkpoint,
    )

    meaningful = [e for e in new_events if _is_meaningful(e, policy)]
    advanced.events_since_checkpoint += len(meaningful)

    if not policy.is_enabled():
        return None, advanced

    forced_event_type = _find_forced(meaningful, policy.on_event_types)

    reason: str | None = None
    if forced_event_type is not None:
        reason = f"event_type:{forced_event_type}"
    elif policy.every_events is not None and advanced.events_since_checkpoint >= policy.every_events:
        reason = f"events_since_last>={policy.every_events}"
    elif (
        policy.every_minutes is not None
        and state.last_signal_time > 0
        and (monotonic_now - state.last_signal_time) >= policy.every_minutes * 60
    ):
        reason = f"minutes_elapsed>={policy.every_minutes}"
    elif (
        policy.every_ticks is not None
        and (tick - max(0, state.last_signal_tick)) >= policy.every_ticks
        and state.last_signal_tick != tick
    ):
        reason = f"ticks_elapsed>={policy.every_ticks}"

    if reason is None:
        return None, advanced

    payload = _payload(meaningful, pending_dm_count)
    refs = {
        "recent_event_count": len(meaningful),
        "tick": tick,
    }
    signal = DirectorSignal(
        id=new_id,
        type="checkpoint",
        tick=tick,
        created_at=monotonic_now if monotonic_now > 0 else time.time(),
        reason=reason,
        refs=refs,
        payload=payload,
    )

    advanced.last_signal_tick = tick
    advanced.last_signal_time = monotonic_now
    advanced.events_since_checkpoint = 0
    return signal, advanced


def _find_forced(events: list[Event], force_types: list[str]) -> str | None:
    if not force_types:
        return None
    force_set = set(force_types)
    for ev in events:
        if ev.type in force_set:
            return ev.type
    return None


def _payload(events: list[Event], pending_dm_count: int) -> dict[str, Any]:
    """Compact, deterministic checkpoint payload — no LLM summary."""
    type_counts: dict[str, int] = {}
    for ev in events:
        type_counts[ev.type] = type_counts.get(ev.type, 0) + 1

    recent_refs = [{"tick": ev.tick, "type": ev.type, "source": ev.source} for ev in events[-10:]]

    return {
        "event_type_counts": type_counts,
        "recent_event_refs": recent_refs,
        "pending_dm_count": pending_dm_count,
    }
