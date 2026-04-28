"""DSL functions for querying the EventLog: event, events_since, last_event_tick."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from worldseed.engine.state_store import StateStore

from worldseed.dsl.functions._registry import register_function
from worldseed.dsl.functions.helpers import parse_kwargs


def _call_event(
    args_str: str,
    store: StateStore,
    ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    """event(type=X) → list of matching events from EventLog."""
    event_log = ctx.get("event_log")
    if event_log is None:
        return []

    kw = parse_kwargs(args_str)
    event_type = kw.get("type")
    if event_type is None:
        return []

    events = event_log.get_events(event_type=event_type)
    return [e.to_dict() for e in events]


def _call_events_since(
    args_str: str,
    store: StateStore,
    ctx: dict[str, Any],
) -> list[dict[str, Any]]:
    """events_since(type=X, max_age_ticks=N) → events of type X within last N ticks.

    Window is inclusive: returns events with `tick >= current_tick - N`.
    Use length(events_since(...)) to count, or compare with empty list to detect absence.
    """
    event_log = ctx.get("event_log")
    if event_log is None:
        return []

    kw = parse_kwargs(args_str)
    event_type = kw.get("type")
    if event_type is None:
        return []

    try:
        max_age = int(kw.get("max_age_ticks", "0"))
    except (TypeError, ValueError):
        return []

    current_tick = ctx.get("tick", 0)
    since = max(0, current_tick - max_age)
    events = event_log.get_events(since_tick=since, event_type=event_type)
    return [e.to_dict() for e in events]


def _call_last_event_tick(
    args_str: str,
    store: StateStore,
    ctx: dict[str, Any],
) -> int:
    """last_event_tick(type=X) → highest tick at which an event of type X was seen.

    Returns -1 when no matching event exists, so callers can write
    `$tick - last_event_tick(type=X) >= N` and have a sensible result on cold start.
    """
    event_log = ctx.get("event_log")
    if event_log is None:
        return -1

    kw = parse_kwargs(args_str)
    event_type = kw.get("type")
    if event_type is None:
        return -1

    events = event_log.get_events(event_type=event_type)
    if not events:
        return -1
    return int(max(e.tick for e in events))


register_function("event", _call_event)
register_function("events_since", _call_events_since)
register_function("last_event_tick", _call_last_event_tick)
