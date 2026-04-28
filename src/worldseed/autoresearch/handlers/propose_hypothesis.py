"""``propose_hypothesis`` — privately formalize a new research direction.

Hypotheses are stored as a list on the agent's own entity
(``agent.data["hypotheses"]``). Engine perception treats ``hypotheses`` as a
hidden property (see ``perception.hidden_properties`` in scene config), so
other agents cannot see what their peers are working on. Only when the
owner publishes a paper bound to a hypothesis does its content become public
(snapshot-embedded into the paper).

This action does NOT use GPU and creates no scene-wide event by default —
it's pure planning. The wake summary for the owning agent will surface
their own hypotheses via self_state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.autoresearch.handlers._common import (
    emit,
    get_action_params,
    get_agent_id,
)

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.state_store import StateStore


def _next_hypothesis_id(agent_data: dict[str, Any]) -> str:
    """Generate the next per-agent hypothesis id like ``hyp_001``.

    Per-agent counter (not global) — each agent's hypotheses are numbered
    independently since they are private state.
    """
    existing = agent_data.get("hypotheses") or []
    n = len(existing) + 1
    return f"hyp_{n:03d}"


def handle(
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    params = get_action_params(ctx)
    agent_id = get_agent_id(ctx)

    claim = str(params.get("claim") or "").strip()
    rationale = str(params.get("rationale") or "").strip()
    builds_on = str(params.get("builds_on") or "").strip() or None

    if not claim or not rationale:
        emit(
            event_log,
            tick,
            agent_id,
            "action_error",
            "propose_hypothesis rejected — claim and rationale are required",
            scope="admin",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    agent = store.get(agent_id)
    if agent is None:
        return

    # builds_on validation: drop silently if not a valid paper_id rather
    # than reject the whole hypothesis. Claude often puts free-text
    # descriptions here even after we tell it to use paper_NNN — losing the
    # linkage is fine; losing the entire hypothesis is not. Emit an admin
    # warning so the agent learns over subsequent wakes.
    if builds_on:
        cited = store.get(builds_on)
        if cited is None or cited.type != "paper":
            emit(
                event_log,
                tick,
                agent_id,
                "warning",
                f"propose_hypothesis: builds_on={builds_on!r} is not a paper_id — "
                f"dropping the linkage but keeping the hypothesis. Use a paper_id "
                f"like 'paper_004' for builds_on, or omit it entirely.",
                scope="admin",
                target=agent_id,
                push=False,
                recorder=ctx.get("recorder"),
            )
            builds_on = None

    hypotheses: list[dict[str, Any]] = list(agent.data.get("hypotheses") or [])
    hyp_id = _next_hypothesis_id(agent.data)

    new_hyp: dict[str, Any] = {
        "id": hyp_id,
        "claim": claim,
        "rationale": rationale,
        "builds_on": builds_on,
        "status": "proposed",  # proposed → testing → published / refuted / abandoned
        "linked_experiments": [],
        "published_as_paper": None,
        "created_tick": tick,
    }
    hypotheses.append(new_hyp)
    store.update_property(agent_id, "hypotheses", hypotheses)

    # Admin-scoped event to give the owner a wake-friendly confirmation.
    # Other agents cannot see this (scope=admin + target=owner).
    emit(
        event_log,
        tick,
        agent_id,
        "hypothesis_proposed",
        f"{agent_id} formed private hypothesis {hyp_id}: {claim[:120]}",
        scope="admin",
        target=agent_id,
        push=True,
        recorder=ctx.get("recorder"),
    )
