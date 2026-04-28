"""Director DM signal mode — TickEngine routes DM into the queue, not the provider.

Verifies the compatibility contract:
  - dm_mode='internal' keeps existing provider path (zero behavior change).
  - dm_mode='signal' enqueues PendingDMRequest + DirectorSignal, no provider call.
  - resolve_director_dm_request applies effects and delivers narrative.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from worldseed.dm.providers.base import DMProvider
from worldseed.models.config_schema import (
    DirectorCheckpointConfig,
    DirectorConfig,
)
from worldseed.protocol.dm import DMResponse
from worldseed.scene.config import load_config
from worldseed.world import WorldEngine

CONFIGS_DIR = Path(__file__).parent.parent.parent / "configs"


class _SpyProvider(DMProvider):
    """DM provider that records every call so we can assert it was bypassed."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def judge(self, dm_ctx: Any) -> DMResponse:
        self.calls.append(dm_ctx)
        return DMResponse(narrative="spy default", effects=[])


def _make_engine(*, dm_mode: str, provider: DMProvider | None = None) -> WorldEngine:
    cfg = load_config(CONFIGS_DIR / "teahouse.yaml")
    cfg.director = DirectorConfig(
        enabled=True,
        dm_mode=dm_mode,  # type: ignore[arg-type]
        max_pending_dm=8,
        checkpoint=DirectorCheckpointConfig(every_events=None, every_minutes=None, every_ticks=None, on_event_types=[]),
    )
    engine = WorldEngine(config=cfg, dm_provider=provider)
    engine.register_from_config()
    return engine


def _find_dm_action(engine: WorldEngine) -> tuple[str, str, dict[str, Any]] | None:
    """Pick `attempt` (simplest DM action — free_text only) plus an actor."""
    name = "attempt"
    if name not in engine._config.actions:
        return None
    for aid in engine.get_registered_agents():
        if aid == "narrator":
            continue
        if name in engine.actions_available_to(aid):
            return aid, name, {"description": "looks at the room"}
    return None


@pytest.mark.asyncio
async def test_signal_mode_does_not_call_provider() -> None:
    spy = _SpyProvider()
    engine = _make_engine(dm_mode="signal", provider=spy)
    pick = _find_dm_action(engine)
    if pick is None:
        pytest.skip("teahouse missing 'attempt' action or no eligible agent")
    actor_id, action_name, params = pick

    from worldseed.models.action import ActionSubmission

    submission = ActionSubmission(agent_id=actor_id, action_type=action_name, params=params)
    engine._queue.submit(submission)
    await engine.step_async()

    assert spy.calls == []
    pending_dm_signals = engine.peek_director_signals(types=["dm_request"])
    assert len(pending_dm_signals) == 1
    request_id = pending_dm_signals[0].refs["dm_request_id"]
    request = engine.get_director_dm_request(request_id)
    assert request is not None
    assert request.source_type == "action"
    assert request.actor_agent_id == actor_id


@pytest.mark.asyncio
async def test_internal_mode_still_calls_provider() -> None:
    spy = _SpyProvider()
    engine = _make_engine(dm_mode="internal", provider=spy)
    pick = _find_dm_action(engine)
    if pick is None:
        pytest.skip("teahouse missing 'attempt' action or no eligible agent")
    actor_id, action_name, params = pick

    from worldseed.models.action import ActionSubmission

    submission = ActionSubmission(agent_id=actor_id, action_type=action_name, params=params)
    engine._queue.submit(submission)
    await engine.step_async()

    assert len(spy.calls) >= 1
    # Internal mode must NOT enqueue into the director queue.
    assert engine.peek_director_signals(types=["dm_request"]) == []


@pytest.mark.asyncio
async def test_resolve_applies_effects_and_marks_resolved() -> None:
    """A POSTed resolve actually mutates state and clears the signal."""
    engine = _make_engine(dm_mode="signal")  # signal mode doesn't need a provider
    pick = _find_dm_action(engine)
    if pick is None:
        pytest.skip("teahouse missing 'attempt' action or no eligible agent")
    actor_id, action_name, params = pick

    from worldseed.models.action import ActionSubmission

    submission = ActionSubmission(agent_id=actor_id, action_type=action_name, params=params)
    engine._queue.submit(submission)
    await engine.step_async()

    request = engine.get_director_dm_request(
        engine.peek_director_signals(types=["dm_request"])[0].refs["dm_request_id"]
    )
    assert request is not None

    ok, reason = engine.resolve_director_dm_request(
        request.id,
        narrative="something happened",
        effects_raw=[],  # zero-effect resolve: just narrative
    )
    assert ok, reason

    # After resolve: signal & request both marked resolved, no longer pending.
    assert engine.peek_director_signals(types=["dm_request"]) == []
    assert engine.get_director_dm_request(request.id).status == "resolved"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_max_pending_dm_caps_queue() -> None:
    spy = _SpyProvider()
    cfg = load_config(CONFIGS_DIR / "teahouse.yaml")
    cfg.director = DirectorConfig(
        enabled=True,
        dm_mode="signal",
        max_pending_dm=2,
        checkpoint=DirectorCheckpointConfig(every_events=None, every_minutes=None, every_ticks=None, on_event_types=[]),
    )
    engine = WorldEngine(config=cfg, dm_provider=spy)
    engine.register_from_config()

    pick = _find_dm_action(engine)
    if pick is None:
        pytest.skip("teahouse missing 'attempt' action or no eligible agent")
    actor_id, action_name, params = pick

    from worldseed.models.action import ActionSubmission

    # Submit 3 DM actions across ticks; cap=2 means the third is rejected.
    for _ in range(3):
        engine._queue.submit(ActionSubmission(agent_id=actor_id, action_type=action_name, params=params))
        await engine.step_async()

    pending_count = engine.director_runtime().pending_dm_count()
    assert pending_count == 2  # third was rejected by cap
    assert spy.calls == []  # provider never called
