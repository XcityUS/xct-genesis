"""Shared helpers for autoresearch action handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.models.event import Event

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.state_store import StateStore
    from worldseed.persistence import NullRecorder, RunRecorder


_paper_counter = 0
_experiment_counter = 0


def next_paper_id(store: StateStore) -> str:
    """Return the next sequential paper id like ``paper_001``.

    Seed from max existing entity id to recover after engine restart.
    """
    global _paper_counter
    papers = store.query_by_type("paper")
    max_n = _paper_counter
    for p in papers:
        try:
            n = int(p.id.split("_", 1)[1])
            max_n = max(max_n, n)
        except (IndexError, ValueError):
            continue
    _paper_counter = max_n + 1
    return f"paper_{_paper_counter:03d}"


def next_experiment_id(store: StateStore) -> str:
    """Return the next sequential experiment id like ``experiment_017``.

    Increments a module-level counter so queued-but-not-yet-completed
    experiments don't collide when the worker hasn't created the entity yet.
    """
    global _experiment_counter
    exps = store.query_by_type("experiment")
    max_n = _experiment_counter
    for e in exps:
        try:
            n = int(e.id.split("_", 1)[1])
            max_n = max(max_n, n)
        except (IndexError, ValueError):
            continue
    _experiment_counter = max_n + 1
    return f"experiment_{_experiment_counter:03d}"


def parse_csv_list(raw: object) -> list[str]:
    """Parse comma-separated string OR list into a clean list of strings.

    Action schema declares these params as free_text, but Claude sometimes
    sends them as JSON arrays anyway. Accept both shapes:
    - ``None`` / ``""``                  → []
    - ``"a, b , c"``                     → ["a", "b", "c"]
    - ``["a", "b", "c"]``                → ["a", "b", "c"]
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(s).strip() for s in raw if str(s).strip()]
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [s.strip() for s in str(raw).split(",") if s.strip()]


def emit(
    event_log: EventLog,
    tick: int,
    agent_id: str,
    event_type: str,
    detail: str,
    *,
    scope: str = "global",
    push: bool = False,
    highlight: bool = False,
    target: str | None = None,
    ttl: int = 10,
    recorder: RunRecorder | NullRecorder | None = None,
) -> None:
    """Small wrapper to emit an event (keeps handler code focused on logic).

    ``ttl`` defaults to 10 — longer than the standard 3 so slow worker
    completions (experiments take 3+ minutes) don't expire before agents
    see them.

    If ``recorder`` is given, also persists to stream.jsonl so tooling
    can inspect the event history without re-reading wakeups.
    """
    event_log.append(
        Event(
            tick=tick,
            type=event_type,
            source=agent_id,
            detail=detail,
            ttl=ttl,
            scope=scope,
            target=target,
            push=push,
            highlight=highlight,
        )
    )
    if recorder is not None:
        try:
            recorder.record(
                "event",
                tick,
                type=event_type,
                source=agent_id,
                detail=detail,
                scope=scope,
                target=target or "",
            )
        except Exception:  # noqa: BLE001
            pass


def get_action_params(ctx: dict[str, Any]) -> dict[str, Any]:
    """Pull the action_params dict out of ctx, defaulting to empty."""
    return ctx.get("action_params") or {}


def get_agent_id(ctx: dict[str, Any]) -> str:
    """Read the acting agent id from ctx, defaulting to ``"system"``."""
    return str(ctx.get("agent_id") or "system")


# Params we echo back in self_state.last_action. Deliberately whitelist —
# new_train_py / patches / abstract can be huge and would bloat every
# perception snapshot. Only small identifier/intent fields survive.
_LAST_ACTION_PARAM_WHITELIST = (
    "description",
    "title",
    "claim",
    "paper_id",
    "verdict",
    "hypothesis_id",
    "builds_on",
    "method_commit",
)


def stash_last_action(
    store: StateStore,
    agent_id: str,
    action: str,
    params: dict[str, Any],
    tick: int,
) -> None:
    """Write ``agent.data["last_action"]`` so the agent's next wake can see
    what it did on the previous wake.

    Called once per action submission (even if the handler rejects it —
    agents learn from their mistakes too). ``hidden_properties`` keeps
    this invisible to other agents.
    """
    if not agent_id or agent_id == "system":
        return
    agent = store.get(agent_id)
    if agent is None:
        return
    trimmed: dict[str, str] = {}
    for key in _LAST_ACTION_PARAM_WHITELIST:
        v = params.get(key)
        if v in (None, "", [], {}):
            continue
        s = str(v)
        trimmed[key] = s if len(s) <= 200 else s[:197] + "…"
    store.update_property(
        agent_id,
        "last_action",
        {
            "action": action,
            "tick": tick,
            "params": trimmed,
        },
    )
