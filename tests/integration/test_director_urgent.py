"""Director urgent signals — fire alongside the existing wake path, not in place of it.

Compatibility contract: existing connector.notify continues to wake gateway-
connected agents. Urgent signals are an additional observability channel for
main agents — they must not change current wake behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from worldseed.engine.inbox import InboxEvent
from worldseed.models.config_schema import (
    DirectorCheckpointConfig,
    DirectorConfig,
)
from worldseed.scene.config import load_config
from worldseed.world import WorldEngine

CONFIGS_DIR = Path(__file__).parent.parent.parent / "configs"


def _make_enabled_engine() -> WorldEngine:
    cfg = load_config(CONFIGS_DIR / "teahouse.yaml")
    cfg.director = DirectorConfig(
        enabled=True,
        dm_mode="signal",
        max_pending_dm=8,
        checkpoint=DirectorCheckpointConfig(
            every_events=None,
            every_minutes=None,
            every_ticks=None,
            on_event_types=[],
        ),
    )
    engine = WorldEngine(config=cfg)
    engine.register_from_config()
    return engine


class TestUrgentFromPushEvent:
    def test_push_event_produces_urgent_signal(self) -> None:
        engine = _make_enabled_engine()
        # Manually inject a push event into one agent's inbox to simulate
        # what perceiver.deliver would do for a real push event.
        agents = engine.get_registered_agents()
        target = next(a for a in agents if a != "narrator")
        other = next(a for a in agents if a != target and a != "narrator")
        inbox = engine._inbox_manager.get_or_create(target)
        inbox.append_event(
            InboxEvent(
                tick=engine.tick,
                type="alert",
                source=other,
                detail="something happened",
                push=True,
            )
        )

        engine._observe_attention()

        urgents = engine.peek_director_signals(types=["urgent"])
        assert len(urgents) == 1
        assert urgents[0].target_agent_id == target
        assert urgents[0].refs.get("event_ref")

    def test_disabled_director_makes_no_signal(self) -> None:
        engine = WorldEngine(config_path=CONFIGS_DIR / "teahouse.yaml")
        engine.register_from_config()
        agents = engine.get_registered_agents()
        target = next(a for a in agents if a != "narrator")
        engine._inbox_manager.get_or_create(target).append_event(
            InboxEvent(tick=0, type="alert", source="x", detail="x", push=True)
        )
        engine._observe_attention()
        assert engine.peek_director_signals() == []


class TestUrgentDedup:
    def test_same_target_event_ref_not_doubled(self) -> None:
        engine = _make_enabled_engine()
        agents = [a for a in engine.get_registered_agents() if a != "narrator"]
        target = agents[0]
        other = agents[1]
        inbox = engine._inbox_manager.get_or_create(target)
        inbox.append_event(
            InboxEvent(
                tick=engine.tick,
                type="alert",
                source=other,
                detail="x",
                push=True,
            )
        )
        engine._observe_attention()
        # Observe again without changes → no new signal.
        engine._observe_attention()
        assert len(engine.peek_director_signals(types=["urgent"])) == 1


class TestUrgentDoesNotDrain:
    def test_inbox_events_remain_after_observe(self) -> None:
        engine = _make_enabled_engine()
        target = next(a for a in engine.get_registered_agents() if a != "narrator")
        inbox = engine._inbox_manager.get_or_create(target)
        inbox.append_event(InboxEvent(tick=0, type="alert", source="x", detail="d", push=True))
        before = len(inbox.peek_events())
        engine._observe_attention()
        after = len(inbox.peek_events())
        assert before == after


@pytest.mark.parametrize("config_name", ["teahouse.yaml"])
def test_step_runs_observe_when_enabled(config_name: str) -> None:
    """Stepping the engine should run observe_attention without crashing."""
    cfg = load_config(CONFIGS_DIR / config_name)
    cfg.director = DirectorConfig(
        enabled=True,
        dm_mode="signal",
        max_pending_dm=8,
        checkpoint=DirectorCheckpointConfig(every_events=None, every_minutes=None, every_ticks=None, on_event_types=[]),
    )
    engine = WorldEngine(config=cfg)
    engine.register_from_config()
    for _ in range(3):
        engine.step()
    # No urgent expected when no push events have occurred.
    assert engine.peek_director_signals(types=["urgent"]) == []
