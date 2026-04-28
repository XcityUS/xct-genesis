"""Async worker that drains the pending queue and dispatches training runs.

Runs as a single asyncio task owned by the engine. Each dequeued item is
spawned as its own task and claims one GPU from a ``GPUPool`` before
calling the remote training daemon on EC2. Multiple experiments can run
in parallel — one per free GPU.

Flow for an ``ExperimentRequest``:
1. Local git: create ``experiment/<agent>/<exp_id>`` branch from baseline,
   write the agent's ``train_gpt.py`` payload, commit
2. Acquire a GPU id from the pool
3. POST source + gpu_id to the EC2 daemon; parse ``val_loss`` from result
4. Mutate store: create ``experiment`` entity, bump corpus counters
5. Emit ``experiment_completed`` / ``experiment_crashed``
6. Release the GPU

Flow for a ``VerifyRequest``:
1. Read ``train_gpt.py`` at the paper's ``method_commit`` via git show
2. Acquire GPU → POST source → parse val_loss
3. Compare to ``expected_val_loss`` within ``VERIFY_TOLERANCE``; emit outcome
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from worldseed.autoresearch.ec2_runner import EC2Runner, GPUPool, TrainingResult
from worldseed.autoresearch.handlers._common import emit
from worldseed.autoresearch.paths import main_worktree, results_tsv
from worldseed.autoresearch.pending_queue import (
    ExperimentRequest,
    VerifyRequest,
    WorkItem,
    get_queue,
)
from worldseed.models.entity import Entity

if TYPE_CHECKING:
    from worldseed.engine.event_log import EventLog
    from worldseed.engine.state_store import StateStore
    from worldseed.persistence import NullRecorder, RunRecorder

log = structlog.get_logger()


# Wall-clock ceiling per experiment, seconds. Baseline ~3 min on A100 40GB
# (5M-param GPT, vocab 8192, block 512). 600s caps runs that could spiral
# (e.g. an agent doubling TOTAL_STEPS or inflating the model).
EXPERIMENT_BUDGET_SEC = 600
# val_loss within ±0.02 (absolute) of the paper's claim counts as reproducible.
# LM training has ~±0.01 seed variance at this scale; 0.02 gives a small margin.
VERIFY_TOLERANCE = 0.02
# Default for p4d.24xlarge (8× A100). Override with AUTORESEARCH_GPU_COUNT env
# for smaller boxes (g5.12xlarge has 4× A10G, g5.xlarge has 1).
DEFAULT_GPU_COUNT = int(os.environ.get("AUTORESEARCH_GPU_COUNT", "8"))
# Baseline source file lives under main_worktree() with this name.
TRAIN_FILENAME = "train_gpt.py"


class AutoresearchWorker:
    """Drains the pending queue, dispatches training to the EC2 daemon."""

    def __init__(
        self,
        store: StateStore,
        event_log: EventLog,
        recorder: RunRecorder | NullRecorder | None = None,
        runner: EC2Runner | None = None,
        gpu_count: int = DEFAULT_GPU_COUNT,
    ) -> None:
        self._store = store
        self._event_log = event_log
        self._recorder = recorder
        self._runner = runner if runner is not None else EC2Runner()
        self._gpu_pool = GPUPool(gpu_count)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        # Serializes git operations on the shared main worktree. Multiple
        # experiments running in parallel each need to checkout/commit on
        # the same dir, which races without a lock — observed crashes:
        # "git_commit_failed" on tight bursts. Lock is held only for the
        # ~1s git phase; training (the long part) runs unlocked in parallel.
        self._git_lock = asyncio.Lock()

    def start(self) -> None:
        """Launch the worker as a background task."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="autoresearch-worker")
        if not self._runner.enabled:
            log.warning(
                "ec2_daemon_not_configured",
                msg=(
                    "AUTORESEARCH_DAEMON_URL / AUTORESEARCH_DAEMON_TOKEN not set; "
                    "every experiment will crash with 'daemon_not_configured'"
                ),
            )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for t in list(self._inflight):
            t.cancel()
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)
        await self._runner.close()

    async def _loop(self) -> None:
        queue = get_queue()
        while self._running:
            try:
                item = await queue.dequeue()
            except asyncio.CancelledError:
                break
            task = asyncio.create_task(
                self._run_with_gpu(item),
                name=f"train-{_item_tag(item)}",
            )
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)

    async def _run_with_gpu(self, item: WorkItem) -> None:
        gpu_id = await self._gpu_pool.acquire()
        try:
            await self._execute(item, gpu_id)
        except Exception as exc:  # noqa: BLE001
            log.exception("worker_error", item=_item_tag(item), error=str(exc))
        finally:
            self._gpu_pool.release(gpu_id)
            # Release the per-agent rate-limit slot — handler can now accept
            # this agent's next submission. Must run regardless of outcome.
            get_queue().mark_done(item)

    async def _execute(self, item: WorkItem, gpu_id: int) -> None:
        if isinstance(item, ExperimentRequest):
            await self._run_experiment(item, gpu_id)
        elif isinstance(item, VerifyRequest):
            await self._run_verify(item, gpu_id)

    # ------------------------------------------------------------------
    # ExperimentRequest
    # ------------------------------------------------------------------

    async def _run_experiment(self, req: ExperimentRequest, gpu_id: int) -> None:
        wt = main_worktree()
        branch = f"experiment/{req.agent_id}/{req.experiment_id}"

        # Serialize git phase only (~1s). Training (~210s) runs unlocked
        # below so other GPUs can train in parallel.
        async with self._git_lock:
            commit = await self._checkout_and_commit(wt, branch, req.new_train_py, f"{req.agent_id}: {req.description}")
        if commit is None:
            self._emit_experiment_crashed(req, reason="git_commit_failed")
            return

        result = await self._runner.run_training(req.new_train_py, gpu_id)
        if result.crash_reason is not None or result.val_loss is None:
            self._record_experiment(
                req,
                commit=commit,
                branch=branch,
                val_loss=0.0,
                wall_time=result.wall_time,
                status="crashed",
                description=req.description,
            )
            self._emit_experiment_crashed(
                req,
                reason=result.crash_reason or "no_val_loss",
                stdout_tail=result.stdout_tail,
            )
            return

        self._record_experiment(
            req,
            commit=commit,
            branch=branch,
            val_loss=result.val_loss,
            wall_time=result.wall_time,
            status="ok",
            description=req.description,
        )
        emit(
            self._event_log,
            req.submitted_tick,
            req.agent_id,
            "experiment_completed",
            f"{req.experiment_id} by {req.agent_id}: "
            f"val_loss={result.val_loss:.4f}, wall_time={result.wall_time:.0f}s "
            f"({req.description})",
            scope="global",
            push=True,
            highlight=True,
            recorder=self._recorder,
        )

    def _emit_experiment_crashed(
        self,
        req: ExperimentRequest,
        *,
        reason: str,
        stdout_tail: str = "",
    ) -> None:
        # Agents need to see WHY their code crashed to fix it. Append the
        # last ~400 chars of subprocess stdout (which includes stderr since
        # the daemon merges them) if available — truncate aggressively to
        # keep the event detail readable in wake summaries.
        detail = f"{req.experiment_id} by {req.agent_id} crashed: {reason}"
        if stdout_tail:
            tail = stdout_tail.strip()
            if len(tail) > 400:
                tail = "…" + tail[-400:]
            detail += f"\n--- last output ---\n{tail}"
        emit(
            self._event_log,
            req.submitted_tick,
            req.agent_id,
            "experiment_crashed",
            detail,
            scope="global",
            push=True,
            recorder=self._recorder,
        )

    def _record_experiment(
        self,
        req: ExperimentRequest,
        *,
        commit: str,
        branch: str,
        val_loss: float,
        wall_time: float,
        status: str,
        description: str,
    ) -> None:
        """Create the experiment entity + append row to results.tsv."""
        exp = Entity(
            id=req.experiment_id,
            type="experiment",
            _data={
                "author": req.agent_id,
                "commit": commit,
                "branch": branch,
                "val_loss": val_loss,
                "wall_time": wall_time,
                "status": status,
                # Persist the hypothesis this experiment was bound to (if any)
                # so write_paper can enforce that papers claiming hypothesis X
                # cite experiments that actually tested hypothesis X. Prevents
                # the fake-paper pattern where an agent publishes RoPE result
                # under a SwiGLU claim.
                "hypothesis_id": req.hypothesis_id,
                "description": description,
                "submitted_tick": req.submitted_tick,
            },
        )
        if self._store.get(req.experiment_id) is None:
            self._store.add(exp)

        corpus = self._store.get("corpus")
        if corpus is not None:
            total = int(corpus.data.get("experiments_total", 0) or 0)
            self._store.update_property("corpus", "experiments_total", total + 1)
            if status == "crashed":
                crashed = int(corpus.data.get("experiments_crashed", 0) or 0)
                self._store.update_property("corpus", "experiments_crashed", crashed + 1)
            else:
                # Lower loss is better. best_val_loss unset (None) → any success wins.
                current_best = corpus.data.get("best_val_loss")
                if current_best is None or val_loss < float(current_best):
                    self._store.update_property("corpus", "best_val_loss", val_loss)

        self._append_results_tsv(
            commit=commit,
            val_loss=val_loss,
            wall_time=wall_time,
            status=status,
            description=description,
            author=req.agent_id,
        )

    def _append_results_tsv(
        self,
        *,
        commit: str,
        val_loss: float,
        wall_time: float,
        status: str,
        description: str,
        author: str,
    ) -> None:
        path = results_tsv()
        path.parent.mkdir(parents=True, exist_ok=True)
        header = "commit\tauthor\tval_loss\twall_time\tstatus\tdescription\n"
        row = f"{commit}\t{author}\t{val_loss:.4f}\t{wall_time:.1f}\t{status}\t{description}\n"
        if not path.exists():
            path.write_text(header, encoding="utf-8")
        with path.open("a", encoding="utf-8") as f:
            f.write(row)

    # ------------------------------------------------------------------
    # VerifyRequest
    # ------------------------------------------------------------------

    async def _run_verify(self, req: VerifyRequest, gpu_id: int) -> None:
        wt = main_worktree()

        # git show is read-only but cheap — still serialize to be safe.
        async with self._git_lock:
            source = await self._git_show(wt, req.method_commit, TRAIN_FILENAME)
        if source is None:
            emit(
                self._event_log,
                req.submitted_tick,
                req.agent_id,
                "verify_failed",
                f"verify of {req.paper_id} failed: git show {req.method_commit[:7]}:{TRAIN_FILENAME} failed",
                scope="global",
                push=True,
                recorder=self._recorder,
            )
            return

        result = await self._runner.run_training(source, gpu_id)
        if result.crash_reason is not None or result.val_loss is None:
            emit(
                self._event_log,
                req.submitted_tick,
                req.agent_id,
                "verify_failed",
                f"verify of {req.paper_id} crashed: {result.crash_reason}",
                scope="global",
                push=True,
                recorder=self._recorder,
            )
            return

        val_loss = result.val_loss
        delta = abs(val_loss - req.expected_val_loss)
        within = delta <= VERIFY_TOLERANCE

        # Verify is now triggered automatically by write_paper, so paper is
        # always in "verifying" state when this runs. Transition based on
        # outcome:
        #   within tolerance  → status=under_review (peer review opens)
        #   outside tolerance → status=contested (closed, no review)
        # Also update verify-related fields on the paper for reviewers to see.
        paper = self._store.get(req.paper_id)
        is_first_verify = paper is not None and paper.data.get("status") == "verifying"

        self._store.update_property(req.paper_id, "verified", within)
        self._store.update_property(req.paper_id, "verify_val_loss", val_loss)
        self._store.update_property(req.paper_id, "verify_delta", delta)

        if within:
            if is_first_verify:
                self._store.update_property(req.paper_id, "status", "under_review")
                # Author's hypothesis (if any) transitions publishing → published.
                self._finalize_hypothesis_status(req.paper_id, status="published")
            emit(
                self._event_log,
                req.submitted_tick,
                req.agent_id,
                "verify_completed",
                f"{req.paper_id} verify ok: val_loss={val_loss:.4f} "
                f"(claim {req.expected_val_loss:.4f}, Δ{delta:.4f}) — opening for review",
                scope="global",
                push=True,
                highlight=True,
                recorder=self._recorder,
            )
        else:
            self._store.update_property(req.paper_id, "status", "contested")
            self._bump_corpus_counter("papers_contested")
            # If the paper was bound to a hypothesis, mark it refuted.
            self._finalize_hypothesis_status(req.paper_id, status="refuted")
            emit(
                self._event_log,
                req.submitted_tick,
                req.agent_id,
                "paper_contested",
                f"{req.paper_id} contested — verify got val_loss={val_loss:.4f} "
                f"vs claim {req.expected_val_loss:.4f} (Δ{delta:.4f})",
                scope="global",
                push=True,
                highlight=True,
                recorder=self._recorder,
            )

    def _finalize_hypothesis_status(self, paper_id: str, *, status: str) -> None:
        """When a paper-bound hypothesis transitions, update the author's
        private hypothesis list. Looks up the paper's hypothesis snapshot,
        finds the owning agent, and updates the matching hypothesis entry.
        """
        paper = self._store.get(paper_id)
        if paper is None:
            return
        hyp_snap = paper.data.get("hypothesis")
        if not hyp_snap:
            return
        author_id = paper.data.get("author")
        if not author_id:
            return
        author = self._store.get(author_id)
        if author is None:
            return
        own_hyps = list(author.data.get("hypotheses") or [])
        hyp_id = hyp_snap.get("id")
        for h in own_hyps:
            if h.get("id") == hyp_id:
                h["status"] = status
                break
        self._store.update_property(author_id, "hypotheses", own_hyps)

    def _bump_corpus_counter(self, field: str) -> None:
        corpus = self._store.get("corpus")
        if corpus is None:
            return
        current = int(corpus.data.get(field, 0) or 0)
        self._store.update_property("corpus", field, current + 1)

    # ------------------------------------------------------------------
    # Git helpers — all local ops on the main worktree.
    # ------------------------------------------------------------------

    async def _checkout_and_commit(
        self,
        wt: Path,
        branch: str,
        new_train_py: str,
        commit_msg: str,
    ) -> str | None:
        """Create ``branch`` from baseline, write train_gpt.py, commit. Returns hash."""
        await self._git(wt, "reset", "--hard", "HEAD")
        await self._git(wt, "checkout", "baseline")
        await self._git(wt, "branch", "-D", branch, check=False)
        ok = await self._git(wt, "checkout", "-b", branch)
        if not ok:
            return None

        (wt / TRAIN_FILENAME).write_text(new_train_py, encoding="utf-8")

        ok = await self._git(wt, "add", TRAIN_FILENAME)
        if not ok:
            return None
        ok = await self._git(wt, "commit", "--allow-empty", "-m", commit_msg)
        if not ok:
            return None

        result = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=wt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, _ = await result.communicate()
        return stdout.decode().strip() or None

    async def _git_show(self, wt: Path, commit: str, filename: str) -> str | None:
        """``git show <commit>:<filename>`` without touching the working tree."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            "show",
            f"{commit}:{filename}",
            cwd=wt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            log.warning(
                "git_show_failed",
                commit=commit,
                filename=filename,
                stderr=stderr.decode(errors="replace"),
            )
            return None
        return stdout.decode(errors="replace")

    async def _git(self, wt: Path, *args: str, check: bool = True) -> bool:
        """Run ``git <args>`` in ``wt``. Returns True on success."""
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=wt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if check and proc.returncode != 0:
            log.warning("git_failed", args=args, stderr=stderr.decode())
            return False
        return proc.returncode == 0


def _item_tag(item: WorkItem) -> str:
    if isinstance(item, ExperimentRequest):
        return item.experiment_id or "experiment"
    if isinstance(item, VerifyRequest):
        return f"verify-{item.paper_id}"
    return "unknown"


__all__ = [
    "AutoresearchWorker",
    "EXPERIMENT_BUDGET_SEC",
    "VERIFY_TOLERANCE",
    "TrainingResult",
]
