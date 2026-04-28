"""Director DM resolve — unhappy paths.

Audit gap: only the success path was covered. Schema validation, op rejection,
execute rollback, and double-resolve all need explicit tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from worldseed.models.config_schema import (
    DirectorCheckpointConfig,
    DirectorConfig,
)
from worldseed.scene.config import load_config
from worldseed.world import WorldEngine

CONFIGS_DIR = Path(__file__).parent.parent.parent / "configs"


def _make_engine() -> WorldEngine:
    cfg = load_config(CONFIGS_DIR / "teahouse.yaml")
    cfg.director = DirectorConfig(
        enabled=True,
        dm_mode="signal",
        max_pending_dm=8,
        checkpoint=DirectorCheckpointConfig(
            every_events=None, every_minutes=None, every_ticks=None, on_event_types=[]
        ),
    )
    engine = WorldEngine(config=cfg)
    engine.register_from_config()
    return engine


async def _enqueue_one_dm(engine: WorldEngine) -> str:
    from worldseed.models.action import ActionSubmission

    actor = next(a for a in engine.get_registered_agents() if a != "narrator")
    engine._queue.submit(
        ActionSubmission(
            agent_id=actor,
            action_type="attempt",
            params={"description": "looks at the room"},
        )
    )
    await engine.step_async()
    pending = engine.peek_director_signals(types=["dm_request"])
    assert pending, "expected a queued DM signal"
    return pending[0].refs["dm_request_id"]


class TestSchemaInvalidEffects:
    @pytest.mark.asyncio
    async def test_unknown_operator_fails_with_clear_reason(self) -> None:
        engine = _make_engine()
        request_id = await _enqueue_one_dm(engine)

        ok, reason = engine.resolve_director_dm_request(
            request_id,
            narrative="…",
            effects_raw=[{"operator": "no_such_op", "target": "x.y", "value": 1}],
        )
        assert ok is False
        assert "schema invalid" in reason.lower() or "unknown" in reason.lower()
        # Request should be marked failed, not pending.
        req = engine.get_director_dm_request(request_id)
        assert req is not None and req.status == "failed"

    @pytest.mark.asyncio
    async def test_malformed_effect_fails(self) -> None:
        engine = _make_engine()
        request_id = await _enqueue_one_dm(engine)
        ok, _ = engine.resolve_director_dm_request(
            request_id,
            narrative="…",
            effects_raw=[{"not_an_operator_field": True}],
        )
        assert ok is False


class TestOperatorNotInAllowedOps:
    @pytest.mark.asyncio
    async def test_disallowed_op_rejected(self) -> None:
        engine = _make_engine()
        request_id = await _enqueue_one_dm(engine)
        # `attempt` action's DM allows the default ops set; create_entity is in
        # the default — pick one that is unlikely to be in the allowed set.
        # If allowed_ops is permissive, the request will be valid; that is also
        # an acceptable outcome (the test asserts NOT a 500).
        ok, _ = engine.resolve_director_dm_request(
            request_id,
            narrative="…",
            effects_raw=[{"operator": "for_each", "match": {"type": "agent"}, "sub_effects": []}],
        )
        # for_each is forbidden in DM responses by validate_dm_effects.
        assert ok is False


class TestDoubleResolve:
    @pytest.mark.asyncio
    async def test_resolving_twice_returns_409(self) -> None:
        engine = _make_engine()
        request_id = await _enqueue_one_dm(engine)
        ok, _ = engine.resolve_director_dm_request(
            request_id, narrative="ok", effects_raw=[]
        )
        assert ok is True

        # Second resolve should fail because status is no longer pending.
        ok2, reason = engine.resolve_director_dm_request(
            request_id, narrative="again", effects_raw=[]
        )
        assert ok2 is False
        assert "status" in reason.lower() or "not found" in reason.lower()


class TestUnknownRequestId:
    def test_unknown_id_returns_not_found(self) -> None:
        engine = _make_engine()
        ok, reason = engine.resolve_director_dm_request(
            "nonexistent",
            narrative="…",
            effects_raw=[],
        )
        assert ok is False
        assert "not found" in reason.lower()


class TestActorAttribution:
    """Director-resolved DM events must carry the actor's agent_id, not 'system'."""

    @pytest.mark.asyncio
    async def test_emit_event_attribution_uses_actor(self) -> None:
        engine = _make_engine()
        actor_before = next(a for a in engine.get_registered_agents() if a != "narrator")
        request_id = await _enqueue_one_dm(engine)
        baseline_events = len(engine.event_log.get_events())

        # An effect that emits an event with no explicit source — the engine
        # should pick up `agent_id` from ctx, set by director_resolver.
        ok, reason = engine.resolve_director_dm_request(
            request_id,
            narrative="x",
            effects_raw=[
                {"operator": "emit_event", "type": "test_event", "detail": "hi", "ttl": 1, "scope": "global"}
            ],
        )
        assert ok, reason
        new_events = engine.event_log.get_events()[baseline_events:]
        emitted = [e for e in new_events if e.type == "test_event"]
        assert len(emitted) == 1
        assert emitted[0].source == actor_before
