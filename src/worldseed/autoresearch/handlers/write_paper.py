"""``write_paper`` — create a paper entity in verifying status, trigger auto-verify.

Validates that:
- evidence_experiments exist and were run by the author
- cites (if any) reference existing papers
- hypothesis_id (if any) is one of the author's own private hypotheses

Creates a ``paper`` entity in ``verifying`` status, snapshot-embeds the
hypothesis (making it public for the first time), enqueues an automatic
``VerifyRequest`` so the engine re-runs ``method_commit`` on a fresh GPU.
The worker transitions the paper to ``under_review`` (verify within
tolerance) or ``contested`` (outside tolerance) once verify completes.
Reviewers cannot review a ``verifying`` paper — only ``under_review``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.autoresearch.handlers._common import (
    emit,
    get_action_params,
    get_agent_id,
    next_paper_id,
    parse_csv_list,
)
from worldseed.autoresearch.paper_renderer import _source_at_commit, render_paper
from worldseed.autoresearch.paths import papers_dir
from worldseed.autoresearch.pending_queue import VerifyRequest, get_queue
from worldseed.models.entity import Entity

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.state_store import StateStore


def handle(
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    params = get_action_params(ctx)
    agent_id = get_agent_id(ctx)

    title = str(params.get("title") or "").strip() or "(untitled)"
    claim = str(params.get("claim") or "").strip()
    abstract = str(params.get("abstract") or "").strip()
    method_commit = str(params.get("method_commit") or "").strip()
    evidence_ids = parse_csv_list(params.get("evidence_experiments"))
    cite_ids = parse_csv_list(params.get("cites"))
    hypothesis_id = str(params.get("hypothesis_id") or "").strip() or None

    # Validate evidence: each experiment must exist and belong to the author.
    missing_evidence = [eid for eid in evidence_ids if store.get(eid) is None]
    wrong_author = [
        eid
        for eid in evidence_ids
        if (exp := store.get(eid)) is not None and exp.data.get("author") not in (agent_id, None)
    ]
    if missing_evidence or wrong_author:
        detail_parts: list[str] = []
        if missing_evidence:
            detail_parts.append(f"missing experiments: {missing_evidence}")
        if wrong_author:
            detail_parts.append(f"not your experiments: {wrong_author}")
        emit(
            event_log,
            tick,
            agent_id,
            "write_paper_rejected",
            f"write_paper by {agent_id} rejected — {'; '.join(detail_parts)}",
            scope="global",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    # Cites: must exist (else we can't reason about them). Any status is fine
    # — we snapshot the current status onto this paper, and if a cited paper
    # is later rejected we mark just that cite as ``dead_cite`` without
    # touching the citing paper. This allows forward progress without thrash.
    missing_cites = [cid for cid in cite_ids if store.get(cid) is None]
    if missing_cites:
        emit(
            event_log,
            tick,
            agent_id,
            "write_paper_rejected",
            f"write_paper by {agent_id} rejected — cites reference papers that don't exist: {missing_cites}",
            scope="global",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    # Build cite snapshots — record the cited paper's status at the moment of citing.
    cite_snapshots = {}
    for cid in cite_ids:
        cited = store.get(cid)
        cite_snapshots[cid] = {
            "status_at_cite": cited.data.get("status", "unknown") if cited else "missing",
            "dead": False,
        }

    # Hypothesis snapshot: if the author bound this paper to one of their
    # private hypotheses, copy the hypothesis content into the paper so it
    # becomes public. Validates that hypothesis belongs to the author AND
    # that each evidence experiment was actually run as a test of this
    # hypothesis (experiment.hypothesis_id == paper.hypothesis_id).
    hypothesis_snapshot: dict[str, Any] | None = None
    agent = store.get(agent_id)
    if hypothesis_id and agent is not None:
        own_hyps = list(agent.data.get("hypotheses") or [])
        match = next((h for h in own_hyps if h.get("id") == hypothesis_id), None)
        if match is None:
            emit(
                event_log,
                tick,
                agent_id,
                "write_paper_rejected",
                f"write_paper by {agent_id} rejected — hypothesis_id={hypothesis_id!r} is not yours",
                scope="global",
                target=agent_id,
                push=True,
                recorder=ctx.get("recorder"),
            )
            return

        # Structural fake-paper protection. When you publish a hypothesis,
        # your evidence must come from experiments that actually declared
        # that hypothesis at submission time. Agents cannot claim
        # "SwiGLU improves val_loss" while citing an experiment that was
        # run as a RoPE test — engine rejects.
        mismatched = []
        for eid in evidence_ids:
            exp = store.get(eid)
            if exp is None:
                continue
            exp_hyp = exp.data.get("hypothesis_id")
            if exp_hyp != hypothesis_id:
                mismatched.append((eid, exp_hyp))
        if mismatched:
            details = ", ".join(f"{eid}(hyp={exp_hyp or 'none'})" for eid, exp_hyp in mismatched)
            emit(
                event_log,
                tick,
                agent_id,
                "write_paper_rejected",
                (
                    f"write_paper by {agent_id} rejected — evidence experiments "
                    f"were not run as tests of hypothesis {hypothesis_id}: "
                    f"{details}. To publish this hypothesis you must first run "
                    f"an experiment with run_experiment(hypothesis_id={hypothesis_id!r}, ...). "
                    f"Do NOT reuse experiments from other hypotheses."
                ),
                scope="global",
                target=agent_id,
                push=True,
                recorder=ctx.get("recorder"),
            )
            return

        hypothesis_snapshot = {
            "id": hypothesis_id,
            "claim": match.get("claim"),
            "rationale": match.get("rationale"),
            "builds_on": match.get("builds_on"),
            "first_proposed_tick": match.get("created_tick"),
        }

    # Try to extract a numeric val_loss from the claim (e.g. "val_loss=2.4754").
    # The auto-verify step compares the rerun's val_loss against this. If we
    # can't parse one, fall back to the first evidence experiment's val_loss
    # (same fallback as the old verify_paper handler used).
    expected_val_loss = _extract_val_loss(claim)
    if expected_val_loss is None:
        for eid in evidence_ids:
            exp = store.get(eid)
            if exp is None:
                continue
            v = exp.data.get("val_loss")
            if isinstance(v, (int, float)):
                expected_val_loss = float(v)
                break

    # Capture the train_gpt.py source at method_commit so future agents
    # who want to builds_on this paper can see the exact code their
    # patches will apply to. Without this, agents craft patches against
    # the original baseline and get "find not found" errors when trying
    # to build on papers that already modified the file.
    method_source = _source_at_commit(method_commit) if method_commit else None

    # Create paper entity in verifying status.
    paper_id = next_paper_id(store)
    paper = Entity(
        id=paper_id,
        type="paper",
        _data={
            "title": title,
            "author": agent_id,
            "claim": claim,
            "abstract": abstract,
            "method_commit": method_commit,
            "method_source": method_source,
            "evidence_experiments": evidence_ids,
            "cites": cite_ids,
            "cite_snapshots": cite_snapshots,
            "hypothesis": hypothesis_snapshot,
            "status": "verifying",
            "verified": False,
            "verify_val_loss": None,
            "verify_delta": None,
            "expected_val_loss": expected_val_loss,
            "reviews": [],
            "created_tick": tick,
        },
    )
    store.add(paper)

    # Mark the linked hypothesis as published-pending. Worker will finalize
    # to "published" once verify confirms the paper enters under_review.
    if hypothesis_id and agent is not None:
        own_hyps = list(agent.data.get("hypotheses") or [])
        for h in own_hyps:
            if h.get("id") == hypothesis_id:
                h["status"] = "publishing"
                h["published_as_paper"] = paper_id
                break
        store.update_property(agent_id, "hypotheses", own_hyps)

    # Enqueue auto-verify so the worker re-runs method_commit and transitions
    # paper.status from verifying → under_review (within tolerance) or
    # contested (outside). Until then no reviewer can review (action precondition).
    if method_commit and expected_val_loss is not None:
        get_queue().enqueue_sync(
            VerifyRequest(
                agent_id=agent_id,  # for attribution; this is the author
                paper_id=paper_id,
                method_commit=method_commit,
                expected_val_loss=expected_val_loss,
                submitted_tick=tick,
            )
        )

    # Render markdown.
    try:
        render_paper(paper, store, papers_dir())
    except OSError:
        emit(
            event_log,
            tick,
            agent_id,
            "render_warning",
            f"failed to render {paper_id}.md — entity persisted, markdown missing",
            scope="admin",
        )


def _extract_val_loss(claim: str) -> float | None:
    """Best-effort parse of `val_loss=X.XXXX` (or similar) from claim text."""
    import re

    # Matches "val_loss=2.4754", "val_loss: 2.4754", "val loss of 2.4754"
    m = re.search(r"val[_ ]loss[\s=:]*([0-9]+\.[0-9]+)", claim, re.IGNORECASE)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None
