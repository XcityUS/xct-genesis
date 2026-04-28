"""Tests for checkpoint cadence evaluation — pure function semantics."""

from __future__ import annotations

from worldseed.engine.director.checkpoint import evaluate
from worldseed.engine.director.models import CheckpointPolicy, CheckpointState
from worldseed.models.event import Event


def _ev(tick: int, etype: str = "say", scope: str = "global") -> Event:
    return Event(tick=tick, type=etype, source="x", detail="", ttl=99, scope=scope)


class TestEventsCadence:
    def test_fires_when_threshold_reached(self) -> None:
        policy = CheckpointPolicy(every_events=3, every_minutes=None, every_ticks=None)
        state = CheckpointState()
        events = [_ev(1), _ev(1), _ev(1)]
        sig, advanced = evaluate(
            tick=1,
            monotonic_now=100.0,
            new_events=events,
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=0,
            appended_now=99,
        )
        assert sig is not None
        assert sig.type == "checkpoint"
        assert "events_since_last>=3" in sig.reason
        assert advanced.events_since_checkpoint == 0  # reset after fire

    def test_does_not_fire_below_threshold(self) -> None:
        policy = CheckpointPolicy(every_events=5, every_minutes=None, every_ticks=None)
        state = CheckpointState()
        sig, advanced = evaluate(
            tick=1,
            monotonic_now=100.0,
            new_events=[_ev(1), _ev(1)],
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=0,
            appended_now=99,
        )
        assert sig is None
        assert advanced.events_since_checkpoint == 2

    def test_excludes_admin_scope_from_count(self) -> None:
        policy = CheckpointPolicy(every_events=2, every_minutes=None, every_ticks=None)
        state = CheckpointState()
        events = [_ev(1, scope="admin"), _ev(1, scope="admin"), _ev(1)]
        sig, advanced = evaluate(
            tick=1,
            monotonic_now=100.0,
            new_events=events,
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=0,
            appended_now=99,
        )
        # Only 1 meaningful event → below threshold of 2.
        assert sig is None
        assert advanced.events_since_checkpoint == 1

    def test_excludes_action_rejected_from_count(self) -> None:
        policy = CheckpointPolicy(every_events=2, every_minutes=None, every_ticks=None)
        state = CheckpointState()
        events = [_ev(1, etype="action_rejected"), _ev(1)]
        sig, advanced = evaluate(
            tick=1,
            monotonic_now=100.0,
            new_events=events,
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=0,
            appended_now=99,
        )
        assert sig is None  # only 1 meaningful event
        assert advanced.events_since_checkpoint == 1


class TestForcedEventTypes:
    def test_on_event_types_forces_checkpoint(self) -> None:
        policy = CheckpointPolicy(
            every_events=100,  # high so events alone wouldn't fire
            every_minutes=None,
            every_ticks=None,
            on_event_types=["draft_submitted"],
        )
        state = CheckpointState()
        events = [_ev(1, etype="draft_submitted")]
        sig, _ = evaluate(
            tick=1,
            monotonic_now=100.0,
            new_events=events,
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=0,
            appended_now=99,
        )
        assert sig is not None
        assert sig.reason == "event_type:draft_submitted"


class TestTicksCadence:
    def test_fires_after_tick_delta(self) -> None:
        policy = CheckpointPolicy(every_events=None, every_minutes=None, every_ticks=5)
        state = CheckpointState(last_signal_tick=0)
        sig, _ = evaluate(
            tick=5,
            monotonic_now=100.0,
            new_events=[],
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=0,
            appended_now=99,
        )
        assert sig is not None
        assert "ticks_elapsed>=5" in sig.reason

    def test_does_not_fire_too_soon(self) -> None:
        policy = CheckpointPolicy(every_events=None, every_minutes=None, every_ticks=10)
        state = CheckpointState(last_signal_tick=0)
        sig, _ = evaluate(
            tick=3,
            monotonic_now=100.0,
            new_events=[],
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=0,
            appended_now=99,
        )
        assert sig is None


class TestDisabledPolicy:
    def test_no_dimensions_enabled_never_fires(self) -> None:
        policy = CheckpointPolicy(every_events=None, every_minutes=None, every_ticks=None, on_event_types=[])
        state = CheckpointState()
        # 100 events should still not fire when policy is disabled.
        sig, advanced = evaluate(
            tick=1,
            monotonic_now=100.0,
            new_events=[_ev(1) for _ in range(100)],
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=0,
            appended_now=100,
        )
        assert sig is None
        assert advanced.last_event_cursor == 100  # cursor still advances


class TestPayload:
    def test_payload_has_event_counts_and_recent_refs(self) -> None:
        policy = CheckpointPolicy(every_events=2, every_minutes=None, every_ticks=None)
        state = CheckpointState()
        events = [_ev(1, etype="say"), _ev(1, etype="say"), _ev(2, etype="move")]
        sig, _ = evaluate(
            tick=2,
            monotonic_now=100.0,
            new_events=events,
            policy=policy,
            state=state,
            new_id="c1",
            pending_dm_count=3,
            appended_now=3,
        )
        assert sig is not None
        assert sig.payload["event_type_counts"] == {"say": 2, "move": 1}
        assert sig.payload["pending_dm_count"] == 3
        assert len(sig.payload["recent_event_refs"]) == 3
