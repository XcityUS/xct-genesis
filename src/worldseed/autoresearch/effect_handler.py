"""DSL effect operators for the autoresearch scene.

One operator per action (4 total). EffectConfig is strictly typed so we
can't pass an ``action`` field through a single shared operator.

Synchronous bookkeeping (paper creation, review recording, status
transitions) happens inline in the operator. Long-running work (training
runs for run_experiment, auto-verify spawned by write_paper) is enqueued
into a pending queue that the async worker drains with a GPU mutex — the
effect operator returns immediately so the tick loop stays non-blocking.

Operators registered here:
- ``autoresearch_propose_hypothesis`` — record private hypothesis on agent
- ``autoresearch_run_experiment``    — enqueue experiment work
- ``autoresearch_write_paper``       — create paper(verifying), enqueue auto-verify
- ``autoresearch_review_paper``      — append review, transition status
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.dsl.effects._registry import register_effect

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.state_store import StateStore
    from worldseed.models.config_schema import EffectConfig


# ---------------------------------------------------------------------------
# Operators — each is a thin wrapper that reads action_params from ctx and
# delegates to a handler function in handlers/.
# ---------------------------------------------------------------------------


def _stash_and_dispatch(
    action_name: str,
    handler_fn: Any,
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    """Shared: stash ``last_action`` onto the agent, then run the handler.

    Stashing before dispatch means even rejected actions show up in the
    agent's next-wake self_state — agents learn from their own mistakes.
    """
    from worldseed.autoresearch.handlers._common import (
        get_action_params,
        get_agent_id,
        stash_last_action,
    )

    stash_last_action(store, get_agent_id(ctx), action_name, get_action_params(ctx), tick)
    handler_fn(store, event_log, ctx, tick)


def _propose_hypothesis_op(
    effect: EffectConfig,
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    """Append a private hypothesis to the agent's own hypotheses list."""
    from worldseed.autoresearch.handlers import propose_hypothesis as h

    _stash_and_dispatch("propose_hypothesis", h.handle, store, event_log, ctx, tick)


def _run_experiment_op(
    effect: EffectConfig,
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    """Enqueue an experiment request. Worker picks it up with GPU mutex."""
    from worldseed.autoresearch.handlers import run_experiment as h

    _stash_and_dispatch("run_experiment", h.handle, store, event_log, ctx, tick)


def _write_paper_op(
    effect: EffectConfig,
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    """Create a paper entity in verifying status, render markdown, enqueue
    an auto-verify so the worker can transition status to under_review or
    contested when the rerun completes."""
    from worldseed.autoresearch.handlers import write_paper as h

    _stash_and_dispatch("write_paper", h.handle, store, event_log, ctx, tick)


def _review_paper_op(
    effect: EffectConfig,
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    """Append a review to a paper. If ≥2 matching verdicts, transition status."""
    from worldseed.autoresearch.handlers import review_paper as h

    _stash_and_dispatch("review_paper", h.handle, store, event_log, ctx, tick)


register_effect("autoresearch_propose_hypothesis", _propose_hypothesis_op)
register_effect("autoresearch_run_experiment", _run_experiment_op)
register_effect("autoresearch_write_paper", _write_paper_op)
register_effect("autoresearch_review_paper", _review_paper_op)
