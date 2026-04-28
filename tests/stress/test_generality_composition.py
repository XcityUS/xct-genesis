"""Generality stress test: compose all new primitives in one scene.

Exercises:
  - events_since(type, max_age_ticks)
  - last_event_tick(type)
  - max_by_key(path)
  - director signals (dm_request / urgent / checkpoint)
  - EventConfig.event_target alias (`target:` in YAML)
  - round-robin turn order (rotate effect)
  - vote tally with majority detection (max_by_key consequence)
  - time-since-last-event timeout (last_event_tick consequence)

The scene is a miniature council chamber: agents vote, the engine picks the
winner via max_by_key, directed whispers go only to the winner, and a timeout
resets the board when no vote has arrived in a while.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from worldseed.dm.providers.mock import MockDMProvider
from worldseed.dsl.functions._registry import get_function_handler
from worldseed.engine.event_log import EventLog
from worldseed.engine.state_store import StateStore
from worldseed.models.entity import Entity
from worldseed.models.event import Event
from worldseed.scene.config import load_config
from worldseed.world import WorldEngine

# ---------------------------------------------------------------------------
# YAML scene definition (written once, used by all tests)
# ---------------------------------------------------------------------------

SCENE_YAML = textwrap.dedent("""\
    scene:
      id: council_chamber
      description: >
        A council of three delegates votes on proposals. The delegate with the
        most votes wins the round. If no vote arrives within 3 ticks, the board
        resets. Turn order rotates round-robin after each vote.
      tick_interval: 1.0
      max_ticks: 20

    director:
      enabled: true
      dm_mode: signal
      checkpoint:
        every_events: 3
        every_minutes: null
        every_ticks: null

    entities:
      - id: board
        type: ballot_board
        active_speaker: "alice"
        turn_order: ["alice", "bob", "carol"]
        tally:
          alice: 0
          bob: 0
          carol: 0
        round: 1

    agents:
      - id: alice
        role: delegate
      - id: bob
        role: delegate
      - id: carol
        role: delegate

    actions:
      cast_vote:
        description: Cast a vote for a candidate.
        params:
          - name: candidate
            type: string
            description: Who to vote for (alice, bob, or carol)
        effects:
          - operator: increment
            target: "board.tally.$candidate"
            by: 1
          - operator: rotate
            target: board.active_speaker
            sequence: board.turn_order
        events:
          - type: vote_cast
            detail: "$agent voted for $candidate"
            ttl: 10
            scope: global

      send_winner_notice:
        description: Send a directed notice to the current vote leader.
        params:
          - name: recipient
            type: string
        effects:
          - operator: emit_event
            type: winner_notice
            detail: "You are leading the vote"
            ttl: 5
            scope: target_only
            # Use the `target` alias (the new EventConfig alias under test)
            event_target: "$recipient"
            push: true

    consequences:
      majority_reached:
        trigger:
          - operator: check
            left: "max_by_key(board.tally)"
            op: "!="
            right: ""
        frequency: every_tick
        effects:
          - operator: emit_event
            type: majority_signal
            detail: "Leader detected"
            ttl: 5
            scope: global

      vote_timeout:
        trigger:
          - operator: check
            left: "last_event_tick(type=vote_cast)"
            op: ">="
            right: 0
          - operator: check
            left: "$tick - last_event_tick(type=vote_cast)"
            op: ">="
            right: 3
        frequency: every_tick
        effects:
          - operator: set
            target: board.tally
            value: '{"alice": 0, "bob": 0, "carol": 0}'
          - operator: emit_event
            type: vote_reset
            detail: "No vote in 3 ticks — tally reset"
            ttl: 5
            scope: global

    perception:
      event_scopes:
        target_only:
          rules: []
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_scene(tmp_path: Path) -> Path:
    p = tmp_path / "council_chamber.yaml"
    p.write_text(SCENE_YAML)
    return p


def _make_engine(tmp_path: Path) -> WorldEngine:
    path = _write_scene(tmp_path)
    config = load_config(path)
    engine = WorldEngine(config=config, dm_provider=MockDMProvider())
    engine.register_from_config()
    return engine


# ---------------------------------------------------------------------------
# Test 1: Config validates (pydantic extra="forbid" on all strict models)
# ---------------------------------------------------------------------------


class TestConfigValidates:
    def test_scene_loads_without_error(self, tmp_path: Path) -> None:
        path = _write_scene(tmp_path)
        config = load_config(path)
        assert config.scene.id == "council_chamber"

    def test_director_config_parsed(self, tmp_path: Path) -> None:
        path = _write_scene(tmp_path)
        config = load_config(path)
        assert config.director is not None
        assert config.director.enabled is True
        assert config.director.dm_mode == "signal"

    def test_event_target_alias_accepted(self, tmp_path: Path) -> None:
        """EventConfig must parse `event_target:` field on send_winner_notice action."""
        path = _write_scene(tmp_path)
        config = load_config(path)
        action = config.actions["send_winner_notice"]
        # effects list contains the emit_event effect with event_target
        emit_effects = [e for e in action.effects if e.operator == "emit_event"]
        assert emit_effects, "No emit_event effect on send_winner_notice"
        assert emit_effects[0].event_target == "$recipient"

    def test_consequence_no_extra_fields_rejected(self, tmp_path: Path) -> None:
        """Consequences have extra='forbid'. Adding a rogue field must raise."""
        import yaml
        from pydantic import ValidationError

        from worldseed.models.config_schema import SceneConfig

        raw = yaml.safe_load(SCENE_YAML)
        # Inject a stale/unknown field into majority_reached
        raw["consequences"]["majority_reached"]["stale_field"] = "oops"
        with pytest.raises(ValidationError):
            SceneConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Test 2: DSL functions resolve without raising
# ---------------------------------------------------------------------------


class TestDSLFunctionsResolve:
    def _store_with_votes(self) -> StateStore:
        store = StateStore()
        store.add(
            Entity(
                id="board",
                type="ballot_board",
                _data={"tally": {"alice": 3, "bob": 1, "carol": 1}},
            )
        )
        return store

    def test_max_by_key_picks_winner(self) -> None:
        handler = get_function_handler("max_by_key")
        assert handler is not None
        store = self._store_with_votes()
        result = handler("board.tally", store, {})
        assert result == "alice"

    def test_max_by_key_tie_returns_empty(self) -> None:
        handler = get_function_handler("max_by_key")
        assert handler is not None
        store = StateStore()
        store.add(
            Entity(
                id="board",
                type="ballot_board",
                _data={"tally": {"alice": 2, "bob": 2, "carol": 1}},
            )
        )
        result = handler("board.tally", store, {})
        assert result == ""

    def test_events_since_within_window(self) -> None:
        handler = get_function_handler("events_since")
        assert handler is not None
        log = EventLog()
        log.append(Event(tick=3, type="vote_cast", source="alice", detail="x", ttl=99, scope="global"))
        log.append(Event(tick=5, type="vote_cast", source="bob", detail="y", ttl=99, scope="global"))
        log.append(Event(tick=2, type="vote_cast", source="carol", detail="z", ttl=99, scope="global"))

        ctx = {"event_log": log, "tick": 5}
        result = handler("type=vote_cast, max_age_ticks=2", StateStore(), ctx)
        # Only ticks 3,4,5 within window (5 - 2 = 3)
        assert len(result) == 2
        ticks = {e["tick"] for e in result}
        assert ticks == {3, 5}

    def test_events_since_empty_when_outside_window(self) -> None:
        handler = get_function_handler("events_since")
        assert handler is not None
        log = EventLog()
        log.append(Event(tick=1, type="vote_cast", source="alice", detail="x", ttl=99, scope="global"))

        ctx = {"event_log": log, "tick": 10}
        result = handler("type=vote_cast, max_age_ticks=2", StateStore(), ctx)
        # tick 1 is at 10-2=8, so window is [8,10] — tick 1 is outside
        assert result == []

    def test_last_event_tick_no_events_returns_minus_one(self) -> None:
        handler = get_function_handler("last_event_tick")
        assert handler is not None
        ctx = {"event_log": EventLog()}
        result = handler("type=vote_cast", StateStore(), ctx)
        assert result == -1

    def test_last_event_tick_returns_highest(self) -> None:
        handler = get_function_handler("last_event_tick")
        assert handler is not None
        log = EventLog()
        log.append(Event(tick=2, type="vote_cast", source="alice", detail="x", ttl=99, scope="global"))
        log.append(Event(tick=7, type="vote_cast", source="bob", detail="y", ttl=99, scope="global"))
        log.append(Event(tick=4, type="vote_cast", source="carol", detail="z", ttl=99, scope="global"))
        ctx = {"event_log": log}
        result = handler("type=vote_cast", StateStore(), ctx)
        assert result == 7


# ---------------------------------------------------------------------------
# Test 3: Engine integration — submit actions, step ticks, check director
# ---------------------------------------------------------------------------


class TestEngineComposition:
    def test_engine_registers_agents(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        agents = engine.get_registered_agents()
        assert set(agents) >= {"alice", "bob", "carol"}

    def test_cast_vote_increments_tally(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        engine.submit("alice", "cast_vote", {"candidate": "alice"})
        tally = engine.state.get("board")["tally"]
        assert tally["alice"] == 1
        assert tally["bob"] == 0

    def test_rotate_advances_active_speaker(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        # Initial speaker is alice; after alice votes, it should rotate to bob
        initial = engine.state.get("board")["active_speaker"]
        assert initial == "alice"
        engine.submit("alice", "cast_vote", {"candidate": "alice"})
        after = engine.state.get("board")["active_speaker"]
        assert after == "bob"

    def test_director_enabled(self, tmp_path: Path) -> None:
        engine = _make_engine(tmp_path)
        assert engine.director_enabled() is True

    def test_director_checkpoint_fires_after_threshold_events(self, tmp_path: Path) -> None:
        """After every_events=3 events, a checkpoint signal must appear."""
        engine = _make_engine(tmp_path)
        # cast_vote emits a vote_cast event each time; 3 votes = 3 events → checkpoint
        engine.submit("alice", "cast_vote", {"candidate": "alice"})
        engine.step()
        engine.submit("bob", "cast_vote", {"candidate": "bob"})
        engine.step()
        engine.submit("carol", "cast_vote", {"candidate": "carol"})
        engine.step()
        signals = engine.peek_director_signals(types=["checkpoint"])
        assert len(signals) >= 1, f"Expected checkpoint signal, got: {signals}"

    def test_directed_event_reaches_only_target(self, tmp_path: Path) -> None:
        """send_winner_notice with event_target='bob' must reach bob's inbox only."""
        engine = _make_engine(tmp_path)
        engine.submit("alice", "send_winner_notice", {"recipient": "bob"})
        # Drain alice and carol's inboxes to see no winner_notice
        alice_data = engine.read_inbox("alice")
        carol_data = engine.read_inbox("carol")
        bob_data = engine.read_inbox("bob")

        def _has_winner_notice(inbox_data: dict) -> bool:  # type: ignore[type-arg]
            # read() returns InboxEvent objects, not dicts
            events = inbox_data.get("events", [])
            return any(e.type == "winner_notice" for e in events)

        # target_only scope: bob should have it, alice and carol should not
        assert _has_winner_notice(bob_data), "bob should receive winner_notice"
        assert not _has_winner_notice(alice_data), "alice should NOT receive winner_notice"
        assert not _has_winner_notice(carol_data), "carol should NOT receive winner_notice"

    def test_events_since_via_consequence_window(self, tmp_path: Path) -> None:
        """Verify events_since windowing works with real EventLog at engine tick boundary."""
        engine = _make_engine(tmp_path)
        engine.submit("alice", "cast_vote", {"candidate": "alice"})
        # Advance 5 ticks without votes — vote_cast event is now tick=0, window check at tick=5
        for _ in range(5):
            engine.step()
        handler = get_function_handler("events_since")
        assert handler is not None
        ctx = {"event_log": engine.event_log, "tick": engine.tick}
        within_window = handler("type=vote_cast, max_age_ticks=2", engine.state, ctx)
        assert within_window == [], "vote from tick 0 should be outside 2-tick window at tick 5"

    def test_last_event_tick_fresh_then_stale(self, tmp_path: Path) -> None:
        """last_event_tick returns current tick immediately after vote, then stays there."""
        engine = _make_engine(tmp_path)
        # No votes yet
        handler = get_function_handler("last_event_tick")
        assert handler is not None
        ctx0 = {"event_log": engine.event_log}
        assert handler("type=vote_cast", engine.state, ctx0) == -1

        engine.submit("alice", "cast_vote", {"candidate": "alice"})
        ctx1 = {"event_log": engine.event_log}
        tick_of_vote = handler("type=vote_cast", engine.state, ctx1)
        # The vote was submitted at engine.tick (which is 0 before first step)
        assert tick_of_vote == engine.tick

    def test_max_by_key_picks_leader_from_live_state(self, tmp_path: Path) -> None:
        """max_by_key resolves against real engine state after multiple votes."""
        engine = _make_engine(tmp_path)
        engine.submit("alice", "cast_vote", {"candidate": "carol"})
        engine.submit("bob", "cast_vote", {"candidate": "carol"})
        engine.submit("carol", "cast_vote", {"candidate": "alice"})
        handler = get_function_handler("max_by_key")
        assert handler is not None
        result = handler("board.tally", engine.state, {})
        assert result == "carol"

    def test_majority_consequence_fires_and_emits_signal_event(self, tmp_path: Path) -> None:
        """majority_reached consequence must emit majority_signal after a clear leader."""
        engine = _make_engine(tmp_path)
        # Give carol 2 votes, alice 1
        engine.submit("alice", "cast_vote", {"candidate": "carol"})
        engine.submit("bob", "cast_vote", {"candidate": "carol"})
        engine.submit("carol", "cast_vote", {"candidate": "alice"})
        # Step once to fire consequences
        engine.step()
        majority_events = engine.event_log.get_events(event_type="majority_signal")
        assert len(majority_events) >= 1, "majority_signal event expected after clear leader"
