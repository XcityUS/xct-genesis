"""Director checkpoint signals — cadence-driven attention pings."""

from __future__ import annotations

from pathlib import Path

from worldseed.engine.inbox import InboxEvent
from worldseed.models.config_schema import (
    DirectorCheckpointConfig,
    DirectorConfig,
)
from worldseed.scene.config import load_config
from worldseed.world import WorldEngine

CONFIGS_DIR = Path(__file__).parent.parent.parent / "configs"


def _make_engine(checkpoint: DirectorCheckpointConfig) -> WorldEngine:
    cfg = load_config(CONFIGS_DIR / "teahouse.yaml")
    cfg.director = DirectorConfig(
        enabled=True,
        dm_mode="signal",
        max_pending_dm=8,
        checkpoint=checkpoint,
    )
    engine = WorldEngine(config=cfg)
    engine.register_from_config()
    return engine


class TestCheckpointEventThreshold:
    def test_fires_when_meaningful_events_cross_threshold(self) -> None:
        engine = _make_engine(
            DirectorCheckpointConfig(
                every_events=2,
                every_minutes=None,
                every_ticks=None,
            )
        )
        # Inject 3 meaningful events; observe should fire once.
        from worldseed.models.event import Event

        for i in range(3):
            engine.event_log.append(Event(tick=0, type="say", source=f"a{i}", detail="x", ttl=99, scope="global"))
        engine._observe_attention()
        checkpoints = engine.peek_director_signals(types=["checkpoint"])
        assert len(checkpoints) == 1

    def test_admin_events_do_not_count(self) -> None:
        engine = _make_engine(
            DirectorCheckpointConfig(
                every_events=2,
                every_minutes=None,
                every_ticks=None,
            )
        )
        from worldseed.models.event import Event

        for i in range(5):
            engine.event_log.append(Event(tick=0, type="admin_thing", source="x", detail="x", ttl=99, scope="admin"))
        engine._observe_attention()
        assert engine.peek_director_signals(types=["checkpoint"]) == []


class TestCheckpointForcedTypes:
    def test_on_event_types_forces_fire(self) -> None:
        engine = _make_engine(
            DirectorCheckpointConfig(
                every_events=100,
                every_minutes=None,
                every_ticks=None,
                on_event_types=["draft_submitted"],
            )
        )
        from worldseed.models.event import Event

        engine.event_log.append(
            Event(
                tick=0,
                type="draft_submitted",
                source="alice",
                detail="x",
                ttl=99,
                scope="global",
            )
        )
        engine._observe_attention()
        checkpoints = engine.peek_director_signals(types=["checkpoint"])
        assert len(checkpoints) == 1
        assert checkpoints[0].reason == "event_type:draft_submitted"


class TestNoCheckpointWhenDisabled:
    def test_director_disabled_no_checkpoints(self) -> None:
        engine = WorldEngine(config_path=CONFIGS_DIR / "teahouse.yaml")
        engine.register_from_config()
        for _ in range(20):
            engine.step()
        assert engine.peek_director_signals() == []


class TestStepAsyncTriggersObserve:
    def test_step_advances_cursor(self) -> None:
        engine = _make_engine(
            DirectorCheckpointConfig(
                every_events=100,  # never fires
                every_minutes=None,
                every_ticks=None,
            )
        )
        # Push some events into the log and step — observe must run inline.
        from worldseed.models.event import Event

        engine.event_log.append(Event(tick=0, type="say", source="x", detail="x", ttl=99, scope="global"))
        engine.step()
        # Cursor advanced — second observe sees no new events.
        baseline = engine._director._checkpoint_state.last_event_cursor
        engine._observe_attention()
        assert engine._director._checkpoint_state.last_event_cursor == baseline


class TestCheckpointPayloadShape:
    def test_payload_has_event_counts_and_pending_dm(self) -> None:
        engine = _make_engine(
            DirectorCheckpointConfig(
                every_events=2,
                every_minutes=None,
                every_ticks=None,
            )
        )
        from worldseed.models.event import Event

        engine.event_log.append(Event(tick=0, type="say", source="alice", detail="hi", ttl=99, scope="global"))
        engine.event_log.append(Event(tick=0, type="move", source="bob", detail="moved", ttl=99, scope="global"))
        # Sanity: one urgent so pending_dm_count includes context.
        engine._inbox_manager.get_or_create("alice").append_event(
            InboxEvent(tick=0, type="alert", source="bob", detail="x", push=True)
        )
        engine._observe_attention()

        signals = engine.peek_director_signals(types=["checkpoint"])
        assert len(signals) == 1
        payload = signals[0].payload
        assert "event_type_counts" in payload
        assert payload["event_type_counts"].get("say") == 1
        assert payload["event_type_counts"].get("move") == 1
        assert "pending_dm_count" in payload
