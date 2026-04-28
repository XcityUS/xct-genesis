"""``review_paper`` — append a review to a paper, transition status on consensus.

Acceptance rules:
- verdict ∈ {accept, request_changes, reject}
- Paper stays in ``draft`` until the first review, then ``under_review``.
- Same reviewer can update their own review — the new verdict replaces the
  old one. This is how real peer review works: reviewers revise their
  verdict after authors respond to feedback. With only 2 non-author
  reviewers in a 3-agent scene, disallowing updates permanently stalls
  any paper that ever gets a ``request_changes``.
- After ≥2 reviews, decide on the latest verdict of each unique reviewer:
  - ≥2 accept, 0 reject → ``accepted``
  - ≥2 reject, 0 accept → ``rejected``
  - mixed (including any ``request_changes``) → stays ``under_review``,
    unless it's been sitting for ≥``STALE_TICKS`` with no rejects, in
    which case the accepts win (prevents permanent deadlock when a
    reviewer never re-reviews after request_changes).

Author precondition is enforced in the YAML.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from worldseed.autoresearch.handlers._common import (
    emit,
    get_action_params,
    get_agent_id,
)
from worldseed.autoresearch.paper_renderer import render_paper
from worldseed.autoresearch.paths import papers_dir

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.state_store import StateStore
    from worldseed.persistence import NullRecorder, RunRecorder


_VALID_VERDICTS = {"accept", "request_changes", "reject"}

# If a paper has been in under_review for this many ticks with only accepts
# and request_changes (no rejects), the accepts win — unblocks papers that
# got stuck on an early request_changes the reviewer never followed up on.
STALE_TICKS = 30


def handle(
    store: StateStore,
    event_log: EventLog,
    ctx: dict[str, Any],
    tick: int,
) -> None:
    params = get_action_params(ctx)
    agent_id = get_agent_id(ctx)

    paper_id = str(params.get("paper_id") or "").strip()
    verdict = str(params.get("verdict") or "").strip()
    reasoning = str(params.get("reasoning") or "").strip()

    if verdict not in _VALID_VERDICTS:
        emit(
            event_log,
            tick,
            agent_id,
            "action_error",
            f"review_paper rejected — invalid verdict {verdict!r}",
            scope="admin",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    paper = store.get(paper_id)
    if paper is None or paper.type != "paper":
        emit(
            event_log,
            tick,
            agent_id,
            "action_error",
            f"review_paper rejected — paper {paper_id!r} not found",
            scope="admin",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    status = paper.data.get("status", "draft")
    # Reviewers can only act on under_review papers — verifying / contested /
    # accepted / rejected are all closed states for the review action.
    # (verifying = engine still re-running method_commit to confirm claim;
    # contested = verify failed, paper is dead-on-arrival; accepted/rejected
    # are terminal.)
    if status != "under_review":
        emit(
            event_log,
            tick,
            agent_id,
            "action_error",
            f"review_paper rejected — paper {paper_id} is {status}, not under_review",
            scope="admin",
            target=agent_id,
            push=True,
            recorder=ctx.get("recorder"),
        )
        return

    reviews: list[dict[str, Any]] = list(paper.data.get("reviews") or [])

    # Find existing review from this reviewer and replace if present.
    existing_idx = next(
        (i for i, r in enumerate(reviews) if r.get("reviewer") == agent_id),
        None,
    )
    new_entry = {
        "reviewer": agent_id,
        "verdict": verdict,
        "reasoning": reasoning,
        "tick": tick,
    }
    if existing_idx is not None:
        reviews[existing_idx] = new_entry
    else:
        reviews.append(new_entry)

    store.update_property(paper_id, "reviews", reviews)

    # Status transitions
    new_status = _next_status(reviews, tick)
    if new_status != status:
        store.update_property(paper_id, "status", new_status)
        if new_status == "accepted":
            emit(
                event_log,
                tick,
                agent_id,
                "paper_accepted",
                f"{paper_id} accepted",
                scope="global",
                push=True,
                highlight=True,
                recorder=ctx.get("recorder"),
            )
            _bump_corpus_counter(store, "papers_accepted")
        elif new_status == "rejected":
            emit(
                event_log,
                tick,
                agent_id,
                "paper_rejected",
                f"{paper_id} rejected",
                scope="global",
                push=True,
                recorder=ctx.get("recorder"),
            )
            _bump_corpus_counter(store, "papers_rejected")
            # Forward-lock: mark any paper that cites this one as having a
            # dead cite. We DO NOT change the citing paper's status — once a
            # paper is accepted it stays accepted, preserving forward
            # progress and avoiding thrash.
            _mark_dead_cites(store, event_log, tick, rejected_paper_id=paper_id, recorder=ctx.get("recorder"))

    # Re-render markdown with the new review(s) and status
    try:
        render_paper(paper, store, papers_dir())
    except OSError:
        pass


def _next_status(reviews: list[dict[str, Any]], tick: int) -> str:
    """Decide the next status based on the current review set.

    Only each unique reviewer's LATEST verdict counts — reviewers can
    update their review, and updates replace (not append), so this is
    already enforced by how ``reviews`` is mutated.

    When 2+ reviews are present:
    - majority accept AND 0 reject → accepted immediately
    - majority reject AND 0 accept → rejected immediately
    - mixed accepts + request_changes (no rejects), oldest review is
      older than STALE_TICKS → accept wins (prevents permanent stall
      when a reviewer never comes back to re-review)
    - anything else → stays under_review
    """
    if len(reviews) == 0:
        return "draft"
    if len(reviews) == 1:
        return "under_review"

    accepts = sum(1 for r in reviews if r.get("verdict") == "accept")
    rejects = sum(1 for r in reviews if r.get("verdict") == "reject")
    rc_count = sum(1 for r in reviews if r.get("verdict") == "request_changes")

    if accepts >= 2 and rejects == 0 and rc_count == 0:
        return "accepted"
    if rejects >= 2 and accepts == 0:
        return "rejected"

    # Stale unblock — accepts majority, no rejects, oldest is old enough
    if accepts >= 2 and rejects == 0 and rc_count > 0:
        oldest = min((int(r.get("tick", 0)) for r in reviews), default=tick)
        if tick - oldest >= STALE_TICKS:
            return "accepted"

    return "under_review"


def _bump_corpus_counter(store: StateStore, field: str) -> None:
    corpus = store.get("corpus")
    if corpus is None:
        return
    current = int(corpus.data.get(field, 0) or 0)
    store.update_property("corpus", field, current + 1)


def _mark_dead_cites(
    store: StateStore,
    event_log: EventLog,
    tick: int,
    *,
    rejected_paper_id: str,
    recorder: RunRecorder | NullRecorder | None = None,
) -> None:
    """When paper X is rejected, flag any paper that cites X with dead_cite=true.

    Does NOT change the citing paper's status — forward progress is
    preserved. The dead cite is visible in the rendered markdown so readers
    know the underlying claim was later retracted.
    """
    from worldseed.autoresearch.paper_renderer import render_paper
    from worldseed.autoresearch.paths import papers_dir

    affected = []
    for paper in store.query_by_type("paper"):
        cite_snapshots = paper.data.get("cite_snapshots") or {}
        if rejected_paper_id in cite_snapshots and not cite_snapshots[rejected_paper_id].get("dead"):
            cite_snapshots[rejected_paper_id]["dead"] = True
            store.update_property(paper.id, "cite_snapshots", cite_snapshots)
            affected.append(paper.id)
            try:
                render_paper(paper, store, papers_dir())
            except OSError:
                pass

    if affected:
        emit(
            event_log,
            tick,
            "system",
            "cite_dead_cascade",
            f"{rejected_paper_id} rejected; marked dead_cite on: {affected}",
            scope="global",
            recorder=recorder,
        )
