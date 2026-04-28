"""``run_experiment`` — validate and enqueue an experiment for the worker.

The DSL effect handler is synchronous; real training is enqueued into
``pending_queue.get_queue()`` and drained by the async worker (see
``worker.py``) with a GPU mutex. The worker creates the ``experiment``
entity when the run completes and emits ``experiment_completed``.

Two ways to specify the train_gpt.py code:
- ``patches`` (preferred): list of {find, replace} dicts applied to baseline
- ``new_train_py`` (fallback): full file string, risk of LLM truncation

Exactly one must be provided.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worldseed.autoresearch.handlers._common import (
    emit,
    get_action_params,
    get_agent_id,
    next_experiment_id,
)
from worldseed.autoresearch.paths import main_worktree
from worldseed.autoresearch.pending_queue import ExperimentRequest, get_queue

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.state_store import StateStore


def _baseline_source() -> str:
    """Read the baseline train_gpt.py shipped with the scene."""
    p = Path(__file__).parent.parent / "baseline_template" / "train_gpt.py"
    return p.read_text(encoding="utf-8")


def _source_at_commit(commit: str) -> str | None:
    """Fetch train_gpt.py contents at a specific git commit from the shared
    worktree. Returns None if git show fails (commit missing, etc.).

    Used when an experiment builds_on an accepted paper — patches apply to
    that paper's frozen version rather than the original baseline.
    """
    wt = main_worktree()
    try:
        result = subprocess.run(
            ["git", "show", f"{commit}:train_gpt.py"],
            cwd=wt,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace")
    except (OSError, FileNotFoundError):
        return None


def _parse_patches(raw: object) -> list[dict[str, str]] | str:
    """Coerce a patches param into a clean list, or return error string.

    Accepts:
    - already-a-list: [{"find": ..., "replace": ...}, ...]
    - JSON string of the same shape
    """
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return f"patches must be JSON-parseable: {e}"
    else:
        parsed = raw
    if not isinstance(parsed, list):
        return f"patches must be a list, got {type(parsed).__name__}"
    out: list[dict[str, str]] = []
    for i, p in enumerate(parsed):
        if not isinstance(p, dict):
            return f"patch[{i}] must be a dict with 'find' and 'replace', got {type(p).__name__}"
        find = p.get("find")
        replace = p.get("replace")
        if not isinstance(find, str) or not isinstance(replace, str):
            return f"patch[{i}] requires 'find': str and 'replace': str"
        out.append({"find": find, "replace": replace})
    return out


def _apply_patches(base: str, patches: list[dict[str, str]]) -> tuple[str | None, str | None]:
    """Apply patches in order. Each find must match exactly once.

    Returns (new_source, None) on success or (None, error_message) on failure.
    """
    src = base
    for i, p in enumerate(patches):
        find = p["find"]
        replace = p["replace"]
        count = src.count(find)
        if count == 0:
            preview = find if len(find) <= 80 else find[:77] + "…"
            return None, f"patch[{i}] find string not found in current file: {preview!r}"
        if count > 1:
            preview = find if len(find) <= 80 else find[:77] + "…"
            return (
                None,
                f"patch[{i}] find string matched {count} times (must be exactly 1); "
                f"add more surrounding context to disambiguate: {preview!r}",
            )
        src = src.replace(find, replace, 1)
    return src, None


def handle(
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    params = get_action_params(ctx)
    agent_id = get_agent_id(ctx)

    description = str(params.get("description") or "").strip()
    hypothesis_id = str(params.get("hypothesis_id") or "").strip() or None
    raw_patches = params.get("patches")
    raw_full = params.get("new_train_py")

    if not description:
        emit(
            event_log,
            tick,
            agent_id,
            "action_error",
            "run_experiment rejected — description is required",
            scope="admin",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    has_patches = bool(raw_patches)
    has_full = bool(raw_full)
    if has_patches == has_full:
        emit(
            event_log,
            tick,
            agent_id,
            "action_error",
            (
                "run_experiment rejected — provide EXACTLY ONE of "
                "{patches, new_train_py}; got "
                + ("both" if has_patches and has_full else "neither")
                + ". Prefer patches for almost all changes."
            ),
            scope="admin",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    # Validate hypothesis_id if given + resolve builds_on paper ahead of
    # patch application. hypothesis is checked BEFORE patches so we know
    # which source to apply patches to — baseline or a prior accepted
    # paper's commit.
    agent = store.get(agent_id)
    hypothesis_data: dict[str, Any] | None = None
    if hypothesis_id:
        if agent is None:
            emit(
                event_log,
                tick,
                agent_id,
                "action_error",
                "run_experiment rejected — agent not found",
                scope="admin",
                target=agent_id,
                push=True,
                recorder=ctx.get("recorder"),
            )
            return
        own_hyps = agent.data.get("hypotheses") or []
        hypothesis_data = next((h for h in own_hyps if h.get("id") == hypothesis_id), None)
        if hypothesis_data is None:
            emit(
                event_log,
                tick,
                agent_id,
                "action_error",
                f"run_experiment rejected — hypothesis_id={hypothesis_id!r} is not one of your own hypotheses",
                scope="admin",
                target=agent_id,
                push=True,
                recorder=ctx.get("recorder"),
            )
            return

    # Resolve the starting source for patches: if hypothesis has a
    # builds_on reference to an accepted paper, start from that paper's
    # method_commit (so progress compounds). Otherwise start from baseline.
    # This is the mechanism that lets the corpus actually accumulate
    # improvements instead of every experiment re-starting from val_loss
    # ~2.50.
    base_source = _baseline_source()
    base_origin = "baseline"
    if hypothesis_data:
        builds_on = hypothesis_data.get("builds_on")
        if builds_on:
            parent_paper = store.get(builds_on)
            if (
                parent_paper is not None
                and parent_paper.type == "paper"
                and parent_paper.data.get("status") == "accepted"
            ):
                parent_commit = parent_paper.data.get("method_commit")
                if parent_commit:
                    parent_source = _source_at_commit(parent_commit)
                    if parent_source is not None:
                        base_source = parent_source
                        base_origin = f"paper {builds_on} @ {parent_commit[:7]}"

    # Resolve the new_train_py source from either patches or full file.
    if has_patches:
        patches = _parse_patches(raw_patches)
        if isinstance(patches, str):  # error message
            emit(
                event_log,
                tick,
                agent_id,
                "action_error",
                f"run_experiment rejected — {patches}",
                scope="admin",
                target=agent_id,
                push=True,
                recorder=ctx.get("recorder"),
            )
            return
        if not patches:
            emit(
                event_log,
                tick,
                agent_id,
                "action_error",
                "run_experiment rejected — patches list is empty",
                scope="admin",
                target=agent_id,
                push=True,
                recorder=ctx.get("recorder"),
            )
            return
        new_train_py, err = _apply_patches(base_source, patches)
        if err is not None:
            emit(
                event_log,
                tick,
                agent_id,
                "action_error",
                f"run_experiment rejected — {err} (patching against {base_origin})",
                scope="admin",
                target=agent_id,
                push=True,
                recorder=ctx.get("recorder"),
            )
            return
    else:
        if not isinstance(raw_full, str):
            emit(
                event_log,
                tick,
                agent_id,
                "action_error",
                "run_experiment rejected — new_train_py must be a string",
                scope="admin",
                target=agent_id,
                push=True,
                recorder=ctx.get("recorder"),
            )
            return
        new_train_py = raw_full

    # Rate-limit: reject if this agent already has a queued OR in-flight
    # experiment. Counts both the pending FIFO queue AND items the worker
    # has already dequeued but not yet completed — previously only queue was
    # checked, which let agents spam submissions during training.
    queue = get_queue()
    outstanding = queue.outstanding_for_agent(agent_id)
    if outstanding > 0:
        emit(
            event_log,
            tick,
            agent_id,
            "action_error",
            f"run_experiment rejected — you already have {outstanding} outstanding experiment(s); wait for results",
            scope="admin",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    experiment_id = next_experiment_id(store)
    # new_train_py at this point is guaranteed str (set by either branch above)
    assert isinstance(new_train_py, str)
    request = ExperimentRequest(
        agent_id=agent_id,
        experiment_id=experiment_id,
        new_train_py=new_train_py,
        description=description,
        submitted_tick=tick,
        hypothesis_id=hypothesis_id,
    )
    get_queue().enqueue_sync(request)

    import structlog as _sl

    _sl.get_logger().info(
        "autoresearch_experiment_enqueued",
        agent=agent_id,
        experiment_id=experiment_id,
        desc=description[:80],
        queue_depth=get_queue().pending_count(),
        via="patches" if has_patches else "full_file",
    )

    emit(
        event_log,
        tick,
        agent_id,
        "experiment_enqueued",
        f"{experiment_id} queued by {agent_id}: {description}",
        scope="global",
        target=agent_id,
    )
