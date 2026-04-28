"""Locked evaluation harness for autoresearch.

Agents must NOT modify this file. It is the ground truth for val_loss
measurement on the TinyStories val slice. Engine greps ``val_loss: {number}``
from stdout.
"""

from __future__ import annotations

import numpy as np
import torch
import torch.nn.functional as F

# Fixed eval config. Agents cannot override these.
EVAL_BATCHES = 100
EVAL_BATCH_SIZE = 32
EVAL_BLOCK_SIZE = 512
EVAL_SEED = 1337  # fixed so verify re-runs sample the same validation batches


def detect_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


@torch.no_grad()
def evaluate(
    model: torch.nn.Module,
    val_data: np.ndarray,
    device: str | None = None,
) -> float:
    """Compute mean cross-entropy val loss on fixed TinyStories val batches.

    Prints in parseable format: ``val_loss: {float}`` so the engine can grep.
    Returns val_loss as a float (natural log, lower is better).
    """
    if device is None:
        device = detect_device()

    model = model.to(device)
    model.eval()

    rng = np.random.default_rng(EVAL_SEED)
    losses = []
    for _ in range(EVAL_BATCHES):
        ix = rng.integers(0, len(val_data) - EVAL_BLOCK_SIZE - 1, size=EVAL_BATCH_SIZE)
        x = np.stack([val_data[i : i + EVAL_BLOCK_SIZE].astype(np.int64) for i in ix])
        y = np.stack([val_data[i + 1 : i + 1 + EVAL_BLOCK_SIZE].astype(np.int64) for i in ix])
        tx = torch.from_numpy(x).to(device, non_blocking=True)
        ty = torch.from_numpy(y).to(device, non_blocking=True)

        use_amp = device == "cuda"
        amp_dtype = torch.bfloat16 if use_amp else torch.float32
        with torch.amp.autocast(device_type=device, dtype=amp_dtype, enabled=use_amp):
            logits, _ = model(tx)
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), ty.view(-1))
        losses.append(loss.item())

    val_loss = float(np.mean(losses))
    print(f"val_loss: {val_loss:.4f}")
    return val_loss
