"""Tests for DSL helper functions."""

from __future__ import annotations

from worldseed.dsl.functions import count, relationships_of
from worldseed.engine.state_store import StateStore
from worldseed.models import Entity


def _bunker_store() -> StateStore:
    """Create a minimal bunker state for testing."""
    store = StateStore()
    store.add(
        Entity(
            id="sleeping_quarters",
            type="space",
            _data={
                "description": "Shared sleeping area",
                "connects_to": ["hallway"],
            },
        )
    )
    store.add(
        Entity(
            id="hallway",
            type="space",
            _data={
                "description": "Central corridor",
                "connects_to": ["storage_room", "sleeping_quarters", "entrance"],
            },
        )
    )
    store.add(
        Entity(
            id="storage_room",
            type="space",
            _data={
                "description": "Supply storage room",
                "connects_to": ["hallway"],
            },
        )
    )
    store.add(
        Entity(
            id="entrance",
            type="space",
            _data={
                "description": "Heavy metal door",
                "connects_to": ["hallway"],
            },
        )
    )
    store.add(
        Entity(
            id="food_supply",
            type="resource",
            _data={
                "quantity": 20,
                "unit": "person-days",
                "located_in": ["storage_room"],
            },
        )
    )
    store.add(
        Entity(
            id="old_chen",
            type="agent",
            _data={"location": "sleeping_quarters", "private_stash": 0},
        )
    )
    store.add(
        Entity(
            id="xiao_li",
            type="agent",
            _data={"location": "sleeping_quarters"},
        )
    )
    store.add(
        Entity(
            id="doctor_wang",
            type="agent",
            _data={"location": "hallway"},
        )
    )
    return store


class TestRelationshipsOf:
    def test_single_connection(self) -> None:
        store = _bunker_store()
        result = relationships_of("sleeping_quarters", "connects_to", store)
        assert result == ["hallway"]

    def test_multiple_connections(self) -> None:
        store = _bunker_store()
        result = relationships_of("hallway", "connects_to", store)
        assert set(result) == {"storage_room", "sleeping_quarters", "entrance"}

    def test_nonexistent_entity(self) -> None:
        store = _bunker_store()
        assert relationships_of("ghost", "connects_to", store) == []

    def test_no_matching_type(self) -> None:
        store = _bunker_store()
        assert relationships_of("hallway", "trusts", store) == []


class TestCount:
    def test_count_agents(self) -> None:
        store = _bunker_store()
        assert count(store, "agent") == 3

    def test_count_with_where(self) -> None:
        store = _bunker_store()
        result = count(
            store,
            "agent",
            where="location == sleeping_quarters",
        )
        assert result == 2

    def test_count_no_match(self) -> None:
        store = _bunker_store()
        result = count(
            store,
            "agent",
            where="location == storage_room",
        )
        assert result == 0


class TestEntitiesOf:
    def test_entities_of_reviewed_by_filters_current_agent(self) -> None:
        from worldseed.dsl.functions._registry import get_function_handler

        store = StateStore()
        store.add(
            Entity(
                id="paper_001",
                type="paper",
                _data={
                    "status": "under_review",
                    "author": "alex",
                    "reviews": [{"reviewer": "blair", "verdict": "accept"}],
                },
            )
        )
        store.add(
            Entity(
                id="paper_002",
                type="paper",
                _data={
                    "status": "under_review",
                    "author": "alex",
                    "reviews": [],
                },
            )
        )

        handler = get_function_handler("entities_of")
        assert handler is not None
        result = handler(
            "type='paper', where=status=='under_review' and author != $agent and not reviewed_by($agent)",
            store,
            {"agent_id": "blair"},
        )
        assert result == ["paper_002"]


class TestMaxByKey:
    """max_by_key(path) — pick winning key from a dict-shaped property."""

    def _store_with_tally(self, tally: dict[str, int]) -> StateStore:
        store = StateStore()
        store.add(Entity(id="vote", type="ballot", _data={"tally": tally}))
        return store

    def test_picks_max_value_key(self) -> None:
        from worldseed.dsl.functions._registry import get_function_handler

        handler = get_function_handler("max_by_key")
        assert handler is not None
        store = self._store_with_tally({"yes": 3, "no": 1, "abstain": 2})
        assert handler("vote.tally", store, {}) == "yes"

    def test_tie_returns_empty(self) -> None:
        from worldseed.dsl.functions._registry import get_function_handler

        handler = get_function_handler("max_by_key")
        assert handler is not None
        store = self._store_with_tally({"yes": 2, "no": 2})
        assert handler("vote.tally", store, {}) == ""

    def test_empty_dict_returns_empty(self) -> None:
        from worldseed.dsl.functions._registry import get_function_handler

        handler = get_function_handler("max_by_key")
        assert handler is not None
        store = self._store_with_tally({})
        assert handler("vote.tally", store, {}) == ""

    def test_missing_path_returns_empty(self) -> None:
        from worldseed.dsl.functions._registry import get_function_handler

        handler = get_function_handler("max_by_key")
        assert handler is not None
        store = StateStore()
        assert handler("nope.tally", store, {}) == ""

    def test_skips_non_numeric(self) -> None:
        from worldseed.dsl.functions._registry import get_function_handler

        handler = get_function_handler("max_by_key")
        assert handler is not None
        store = self._store_with_tally({"yes": 3, "garbage": "abc"})  # type: ignore[dict-item]
        assert handler("vote.tally", store, {}) == "yes"
