"""Tests for DirectorRuntime — disabled-by-default + enqueue + observe behavior."""

from __future__ import annotations

from worldseed.engine.director.models import CheckpointPolicy
from worldseed.engine.director.runtime import DirectorRuntime
from worldseed.engine.event_log import EventLog
from worldseed.engine.inbox import InboxEvent, InboxManager
from worldseed.engine.wakeup import WakeupResult
from worldseed.models.event import Event


class TestDisabledRuntime:
    def test_default_is_disabled(self) -> None:
        rt = DirectorRuntime()
        assert rt.enabled is False

    def test_enqueue_action_returns_none_when_disabled(self) -> None:
        rt = DirectorRuntime()
        result = rt.enqueue_action_dm_request(
            action={"action_type": "say", "agent_id": "a"},
            dm_config={},
            ctx={},
            dm_context={},
            actor_agent_id="a",
            tick=1,
        )
        assert result is None

    def test_observe_is_noop_when_disabled(self) -> None:
        rt = DirectorRuntime()
        rt.observe_attention(
            tick=1,
            event_log=EventLog(),
            inbox_manager=InboxManager(),
            wakeup_results=[WakeupResult(agent_id="a", should_wake=True)],
        )
        assert rt.peek_signals() == []

    def test_peek_returns_empty_when_disabled(self) -> None:
        rt = DirectorRuntime()
        assert rt.peek_signals() == []


class TestEnabledEnqueueDM:
    def test_signal_mode_enqueues(self) -> None:
        rt = DirectorRuntime(enabled=True, dm_mode="signal")
        rid = rt.enqueue_action_dm_request(
            action={"action_type": "say"},
            dm_config={"hint": "x"},
            ctx={},
            dm_context={"world_state": "..."},
            actor_agent_id="alice",
            tick=1,
        )
        assert rid is not None
        signals = rt.peek_signals()
        assert len(signals) == 1
        assert signals[0].type == "dm_request"
        assert signals[0].refs.get("dm_request_id") == rid

    def test_internal_mode_does_not_enqueue(self) -> None:
        rt = DirectorRuntime(enabled=True, dm_mode="internal")
        rid = rt.enqueue_action_dm_request(
            action={"action_type": "say"},
            dm_config={},
            ctx={},
            dm_context={},
            actor_agent_id="alice",
            tick=1,
        )
        assert rid is None

    def test_max_pending_dm_caps_queue(self) -> None:
        rt = DirectorRuntime(enabled=True, dm_mode="signal", max_pending_dm=2)
        ids = []
        for _ in range(3):
            ids.append(
                rt.enqueue_action_dm_request(
                    action={"action_type": "say"},
                    dm_config={},
                    ctx={},
                    dm_context={},
                    actor_agent_id="alice",
                    tick=1,
                )
            )
        # First two accepted, third rejected.
        assert sum(1 for x in ids if x is not None) == 2
        assert ids[2] is None
        assert rt.pending_dm_count() == 2


class TestUrgentObservation:
    def _setup_inbox_with_push(self) -> InboxManager:
        mgr = InboxManager()
        inbox = mgr.get_or_create("alice")
        inbox.append_event(InboxEvent(tick=1, type="alert", source="bob", detail="x", push=True))
        return mgr

    def test_urgent_signal_from_wakeup_result(self) -> None:
        rt = DirectorRuntime(enabled=True, dm_mode="signal")
        mgr = self._setup_inbox_with_push()
        rt.observe_attention(
            tick=1,
            event_log=EventLog(),
            inbox_manager=mgr,
            wakeup_results=[WakeupResult(agent_id="alice", should_wake=True, reason="bob alert")],
        )
        signals = rt.peek_signals(types=["urgent"])
        assert len(signals) == 1
        assert signals[0].target_agent_id == "alice"
        assert signals[0].refs.get("event_ref")  # contains the push event ref

    def test_no_signal_when_should_not_wake(self) -> None:
        rt = DirectorRuntime(enabled=True, dm_mode="signal")
        mgr = InboxManager()
        rt.observe_attention(
            tick=1,
            event_log=EventLog(),
            inbox_manager=mgr,
            wakeup_results=[WakeupResult(agent_id="alice", should_wake=False)],
        )
        assert rt.peek_signals(types=["urgent"]) == []


class TestCheckpointObservation:
    def test_checkpoint_fires_on_event_threshold(self) -> None:
        rt = DirectorRuntime(
            enabled=True,
            dm_mode="signal",
            checkpoint_policy=CheckpointPolicy(every_events=2, every_minutes=None, every_ticks=None),
        )
        log = EventLog()
        for _ in range(2):
            log.append(Event(tick=1, type="say", source="x", detail="", ttl=99, scope="global"))
        rt.observe_attention(
            tick=1,
            event_log=log,
            inbox_manager=InboxManager(),
            wakeup_results=[],
        )
        assert len(rt.peek_signals(types=["checkpoint"])) == 1

    def test_no_checkpoint_when_policy_empty(self) -> None:
        rt = DirectorRuntime(
            enabled=True,
            dm_mode="signal",
            checkpoint_policy=CheckpointPolicy(
                every_events=None, every_minutes=None, every_ticks=None, on_event_types=[]
            ),
        )
        log = EventLog()
        for _ in range(50):
            log.append(Event(tick=1, type="say", source="x", detail="", ttl=99, scope="global"))
        rt.observe_attention(
            tick=1,
            event_log=log,
            inbox_manager=InboxManager(),
            wakeup_results=[],
        )
        assert rt.peek_signals(types=["checkpoint"]) == []


class TestPersistence:
    def test_to_dict_restore_roundtrip(self) -> None:
        rt = DirectorRuntime(enabled=True, dm_mode="signal")
        rt.enqueue_action_dm_request(
            action={"action_type": "say"},
            dm_config={},
            ctx={},
            dm_context={},
            actor_agent_id="alice",
            tick=1,
        )
        snapshot = rt.to_dict()

        rt2 = DirectorRuntime(enabled=True, dm_mode="signal")
        rt2.restore(snapshot)
        assert rt2.pending_dm_count() == 1
        assert len(rt2.peek_signals(types=["dm_request"])) == 1
