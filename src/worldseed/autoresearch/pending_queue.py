"""In-memory pending work queue for autoresearch async actions.

``run_experiment`` enqueues an ``ExperimentRequest`` from its synchronous
effect handler. ``write_paper`` internally enqueues a ``VerifyRequest`` so
each published paper is auto-reproduced before review. The async worker
(see ``worker.py``) drains the queue with a single GPU mutex so jobs run
serially.

This is a process-local queue — if the engine restarts, pending items are
lost. MVP-level simplification; can be swapped for a persisted queue later.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal


@dataclass
class ExperimentRequest:
    """An agent wants to run `train_gpt.py` with a new file contents."""

    kind: Literal["run_experiment"] = "run_experiment"
    agent_id: str = ""
    experiment_id: str = ""
    new_train_py: str = ""
    description: str = ""
    submitted_tick: int = 0
    # Optional link to one of the agent's private hypotheses. When set, the
    # worker advances that hypothesis to status="testing" on completion.
    hypothesis_id: str | None = None


@dataclass
class VerifyRequest:
    """Engine-internal request to reproduce a paper's experiment.

    No longer a user action — emitted automatically by ``write_paper`` so
    every published paper is verified before peer review opens. agent_id
    here is the paper's author (the one whose write_paper triggered this);
    it's used only for event attribution.
    """

    kind: Literal["verify_paper"] = "verify_paper"
    agent_id: str = ""
    paper_id: str = ""
    method_commit: str = ""
    expected_val_loss: float = 0.0
    submitted_tick: int = 0


WorkItem = ExperimentRequest | VerifyRequest


class PendingQueue:
    """FIFO queue of pending work.

    DSL effect handlers run synchronously under the tick loop but the worker
    drains the queue from its own async task. ``enqueue_sync`` is the sync
    path; ``dequeue`` is async with a short poll loop (no condition variable
    since we can't reliably ``notify`` across the sync/async boundary).

    Rate-limit tracking: the queue tracks per-agent ``inflight`` items
    (dequeued but not yet completed). Handlers use
    ``has_outstanding_for_agent`` to check pending + inflight together —
    prevents an agent from flooding submissions while the worker is still
    training a previous one.
    """

    def __init__(self) -> None:
        self._items: list[WorkItem] = []
        # item id (run_experiment experiment_id or verify paper_id) → agent_id
        # Tracks items currently being processed by the worker.
        self._inflight: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Queue operations
    # ------------------------------------------------------------------

    def enqueue_sync(self, item: WorkItem) -> None:
        """Sync-safe enqueue for calling from DSL effect handlers."""
        self._items.append(item)

    async def dequeue(self) -> WorkItem:
        """Poll until an item is available. Atomically moves item from
        queue → inflight so rate-limit checks see it as still occupying the
        agent's slot until ``mark_done`` is called.
        """
        while not self._items:
            await asyncio.sleep(0.1)
        item = self._items.pop(0)
        self._inflight[_item_key(item)] = getattr(item, "agent_id", "") or ""
        return item

    def mark_done(self, item: WorkItem) -> None:
        """Worker calls this after an inflight item finishes (success or crash)."""
        self._inflight.pop(_item_key(item), None)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def pending_count(self) -> int:
        return len(self._items)

    def snapshot(self) -> list[WorkItem]:
        """Return a copy of current pending items (for perception / debugging)."""
        return list(self._items)

    def outstanding_for_agent(self, agent_id: str) -> int:
        """Number of items for ``agent_id`` that are queued OR inflight.

        Used by DSL handlers for rate-limit checks — ensures an agent
        cannot submit faster than their previous experiment finishes.
        """
        pending = sum(1 for i in self._items if getattr(i, "agent_id", None) == agent_id)
        inflight = sum(1 for a in self._inflight.values() if a == agent_id)
        return pending + inflight

    def has_outstanding_for_agent(self, agent_id: str) -> bool:
        return self.outstanding_for_agent(agent_id) > 0


def _item_key(item: WorkItem) -> str:
    """Stable id for inflight tracking."""
    if isinstance(item, ExperimentRequest):
        return f"exp:{item.experiment_id}"
    if isinstance(item, VerifyRequest):
        return f"verify:{item.paper_id}"
    return f"unknown:{id(item)}"


_singleton: PendingQueue | None = None


def get_queue() -> PendingQueue:
    """Module-level singleton. Created on first access."""
    global _singleton
    if _singleton is None:
        _singleton = PendingQueue()
    return _singleton
