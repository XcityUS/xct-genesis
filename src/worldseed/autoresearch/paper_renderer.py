"""Render paper entities to ``papers/paper_XXX.md`` files.

Engine owns this file — agents see papers only via perception (paper entity
properties). When a paper's state mutates (write_paper, review_paper, or the
internal verify worker on completion), the handler calls ``render_paper`` to
regenerate the markdown.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from worldseed.autoresearch.paths import main_worktree

if TYPE_CHECKING:
    from worldseed.engine.state_store import StateStore
    from worldseed.models.entity import Entity


def _source_at_commit(commit: str) -> str | None:
    """Read train_gpt.py at a specific git commit from the shared worktree.

    Included in rendered papers so agents who want to builds_on a paper
    can see the actual code they're patching against. Without this they
    craft patches against the original baseline and get "find not found"
    errors because the real base has already been modified.
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


def render_paper(paper: Entity, store: StateStore, papers_dir: Path) -> Path:
    """Write ``papers/{paper.id}.md`` from the paper entity. Returns the path.

    The entity is expected to have these properties (populated by write_paper
    and mutated by review_paper or the internal verify worker):
      title, author, claim, abstract, method_commit,
      evidence_experiments (list[str]),
      cites (list[str]),
      status, verified (bool),
      reviews (list of dicts),
      created_tick
    """
    data = paper.data
    lines: list[str] = []

    title = data.get("title", "(untitled)")
    author = data.get("author", "(unknown)")
    status = data.get("status", "draft")
    verified = bool(data.get("verified", False))
    created_tick = data.get("created_tick", "?")

    status_str = f"{status} (verified)" if verified else status
    lines.append(f"# {paper.id}: {title}")
    lines.append("")
    lines.append(f"**Author:** {author} · **Status:** {status_str} · **Submitted:** tick {created_tick}")
    lines.append("")

    abstract = data.get("abstract", "")
    if abstract:
        lines.append("## Abstract")
        lines.append("")
        lines.append(abstract)
        lines.append("")

    claim = data.get("claim", "")
    if claim:
        lines.append("## Claim")
        lines.append("")
        lines.append(claim)
        lines.append("")

    # Hypothesis snapshot — published hypothesis content (only present when
    # the author bound the paper to one of their previously-private hypotheses).
    hyp = data.get("hypothesis")
    if hyp:
        lines.append("## Hypothesis")
        lines.append("")
        lines.append(f"**Claim:** {hyp.get('claim', '?')}")
        lines.append("")
        lines.append(f"**Rationale:** {hyp.get('rationale', '?')}")
        lines.append("")
        builds_on_hyp = hyp.get("builds_on")
        if builds_on_hyp:
            lines.append(f"**Builds on:** {builds_on_hyp}")
            lines.append("")
        proposed_tick = hyp.get("first_proposed_tick")
        if proposed_tick is not None:
            lines.append(f"*First proposed at tick {proposed_tick} (private until this paper).*")
            lines.append("")

    method_commit = data.get("method_commit", "")
    if method_commit:
        lines.append("## Method")
        lines.append("")
        lines.append(f"Commit: `{method_commit}`")
        lines.append("")

        # Include the full train_gpt.py source at method_commit so future
        # agents who want to builds_on this paper can see the actual code
        # they're patching against. Without this, agents craft patches
        # against the baseline and fail with "find not found" errors.
        source = _source_at_commit(method_commit)
        if source is not None:
            lines.append("### train_gpt.py at method_commit")
            lines.append("")
            lines.append(
                "<details><summary>click to expand full source "
                "(use this as the base for patches if you `builds_on` "
                "this paper)</summary>"
            )
            lines.append("")
            lines.append("```python")
            lines.append(source.rstrip())
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

    # Engine auto-verify result — present once the paper transitions out of
    # `verifying`. Reviewers should compare verify_val_loss to claim val_loss
    # AND compare evidence experiment description to abstract claim.
    verify_val_loss = data.get("verify_val_loss")
    verify_delta = data.get("verify_delta")
    if verify_val_loss is not None:
        lines.append("## Verify (engine auto-rerun)")
        lines.append("")
        delta_str = f"{verify_delta:.4f}" if isinstance(verify_delta, float) else str(verify_delta)
        verdict = "within tolerance ✓" if verified else "outside tolerance ⚠︎"
        lines.append(f"- Re-ran method_commit: val_loss = **{verify_val_loss:.4f}**")
        lines.append(f"- Δ from claim: {delta_str} ({verdict})")
        lines.append("")

    evidence_ids = data.get("evidence_experiments", []) or []
    if evidence_ids:
        lines.append("## Evidence")
        lines.append("")
        lines.append("| experiment | val_loss | wall_time | description |")
        lines.append("|-|-|-|-|")
        for exp_id in evidence_ids:
            exp = store.get(exp_id)
            if exp is None:
                lines.append(f"| {exp_id} | (missing) | (missing) | (missing) |")
                continue
            val_loss = exp.data.get("val_loss", "?")
            wall = exp.data.get("wall_time", "?")
            desc = exp.data.get("description", "")
            val_str = f"{val_loss:.4f}" if isinstance(val_loss, float) else str(val_loss)
            wall_str = f"{wall:.0f}s" if isinstance(wall, (int, float)) else str(wall)
            # Truncate description aggressively to keep table readable
            desc_short = (desc[:80] + "…") if len(desc) > 80 else desc
            # Pipe escaping for table cell
            desc_short = desc_short.replace("|", "\\|")
            lines.append(f"| {exp_id} | {val_str} | {wall_str} | {desc_short} |")
        lines.append("")

    cites = data.get("cites", []) or []
    cite_snapshots = data.get("cite_snapshots") or {}
    if cites:
        lines.append("## Citations")
        lines.append("")
        for cite_id in cites:
            cited = store.get(cite_id)
            snap = cite_snapshots.get(cite_id, {})
            status_at_cite = snap.get("status_at_cite", "")
            is_dead = snap.get("dead", False)
            if cited is None:
                lines.append(f"- {cite_id}: (missing)")
                continue
            c_title = cited.data.get("title", "(untitled)")
            c_author = cited.data.get("author", "(unknown)")
            note = ""
            if is_dead:
                note = " ⚠︎ *this citation was later rejected; retained for historical provenance*"
            elif status_at_cite and status_at_cite != "accepted":
                note = f" *(cited while `{status_at_cite}`)*"
            lines.append(f'- {cite_id}: "{c_title}" by {c_author}{note}')
        lines.append("")

    reviews = data.get("reviews", []) or []
    if reviews:
        lines.append("## Reviews")
        lines.append("")
        for r in reviews:
            reviewer = r.get("reviewer", "?")
            verdict = r.get("verdict", "?")
            reasoning = r.get("reasoning", "")
            tick = r.get("tick", "?")
            lines.append(f"- **{reviewer}** (tick {tick}): **{verdict}** — {reasoning}")
        lines.append("")

    papers_dir.mkdir(parents=True, exist_ok=True)
    out_path = papers_dir / f"{paper.id}.md"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    return out_path
