"""Event Log — in-memory event store with TTL-based cleanup."""

from __future__ import annotations

from worldseed.models.event import Event

MAX_PERMANENT_EVENTS = 500  # cap to prevent unbounded memory growth


class EventLog:
    """In-memory event store for engine use (perceiver, wakeup, consequences).

    Events live here with TTL for agent perception. Persistence to
    stream.jsonl is NOT this class's job — callers that need persistence
    record to RunRecorder separately. This decoupling prevents duplicate
    records (action events already have action records in stream).
    """

    def __init__(self) -> None:
        self._events: list[Event] = []
        self._total_appended: int = 0  # monotonic — survives TTL cleanup

    def append(self, event: Event) -> None:
        """Add an event to the in-memory log."""
        self._events.append(event)
        self._total_appended += 1

    @property
    def size(self) -> int:
        """Number of events currently in the log (after TTL cleanup)."""
        return len(self._events)

    @property
    def total_appended(self) -> int:
        """Total events ever appended; never decreases on TTL cleanup.

        Use this as a cursor when you need to count new events across ticks
        — `len(self._events)` would silently shrink and miscount.
        """
        return self._total_appended

    def seed_total_appended(self, value: int) -> None:
        """Restore the monotonic counter from a saved snapshot.

        Pause/resume only re-appends live (non-expired) events, so the raw
        append count would reset to that smaller number. Call this with the
        saved value so director cursors stay aligned across resume.
        """
        self._total_appended = max(self._total_appended, value)

    def get_events(
        self,
        since_tick: int | None = None,
        event_type: str | None = None,
    ) -> list[Event]:
        """Get events, optionally filtered by tick and/or type."""
        result = self._events
        if since_tick is not None:
            result = [e for e in result if e.tick >= since_tick]
        if event_type is not None:
            result = [e for e in result if e.type == event_type]
        return result

    def cleanup(self, current_tick: int) -> None:
        """Remove events whose TTL has expired.

        An event at tick T with ttl N is alive while current_tick <= T + N.
        TTL="permanent" events never expire but are capped at _MAX_PERMANENT.
        """
        self._events = [
            e
            for e in self._events
            if e.ttl == "permanent" or (isinstance(e.ttl, int) and e.tick + e.ttl >= current_tick)
        ]
        # Cap permanent events to prevent unbounded growth
        permanent = [e for e in self._events if e.ttl == "permanent"]
        if len(permanent) > MAX_PERMANENT_EVENTS:
            # Keep newest, drop oldest
            to_drop = set(id(e) for e in permanent[: len(permanent) - MAX_PERMANENT_EVENTS])
            self._events = [e for e in self._events if id(e) not in to_drop]
