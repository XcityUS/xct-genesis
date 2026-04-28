"""Director-signal layer: queue + checkpoint cadence + runtime facade.

Engine remains runtime-neutral: it produces dm_request / urgent / checkpoint
signals; main agents (Codex, Claude, custom) read them via the HTTP API.
"""

from __future__ import annotations

from worldseed.engine.director.checkpoint import evaluate as evaluate_checkpoint
from worldseed.engine.director.models import (
    CheckpointPolicy,
    CheckpointState,
    DirectorSignal,
    PendingDMRequest,
)
from worldseed.engine.director.queue import DirectorQueue, new_id
from worldseed.engine.director.runtime import DirectorRuntime

__all__ = [
    "CheckpointPolicy",
    "CheckpointState",
    "DirectorQueue",
    "DirectorRuntime",
    "DirectorSignal",
    "PendingDMRequest",
    "evaluate_checkpoint",
    "new_id",
]
