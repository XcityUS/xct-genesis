"""Baseline training script for autoresearch — small GPT on TinyStories.

Agents CAN modify this file freely to explore improvements. Must end by
calling ``evaluate(model, val_data)`` from the locked ``evaluate.py`` — the
engine greps the printed ``val_loss:`` line to capture the result.

Baseline: small GPT (n_layer=4, n_head=4, n_embd=256, ~5M params) on a
pre-tokenized TinyStories slice (SentencePiece BPE vocab=8192).
- Optimizer: AdamW β=(0.9, 0.95), wd=0.1, cosine LR schedule w/ warmup
- Block size 512, batch 512, grad accum 1 (effective batch 262144 tokens/step)
- 1500 steps × 262k tokens/step ≈ 393M training tokens total
- Expected baseline val_loss: ~2.50 on A100 80GB in ~3.5 min wall-clock
- Data path: ``FINEWEB_DATA_DIR`` env var (default ``/data/tinystories``)
  expects ``train.bin`` (uint16 tokens) and ``val.bin``
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from evaluate import detect_device, evaluate

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = Path(os.environ.get("FINEWEB_DATA_DIR", "/data/tinystories"))

VOCAB_SIZE = 8192  # SentencePiece BPE trained on TinyStories
BLOCK_SIZE = 512
N_LAYER = 4
N_HEAD = 4
N_EMBD = 256
DROPOUT = 0.0

BATCH_SIZE = 512
GRAD_ACCUM = 1  # effective batch = 512 * 1 * 512 = 262144 tokens/step
TOTAL_STEPS = 1500
WARMUP_STEPS = 150
LR_MAX = 3e-4
LR_MIN = 3e-5
WEIGHT_DECAY = 0.1
BETA1 = 0.9
BETA2 = 0.95
GRAD_CLIP = 1.0


# ---------------------------------------------------------------------------
# Model — a minimal GPT (nanoGPT-style, ~5M params)
# ---------------------------------------------------------------------------


class CausalSelfAttention(nn.Module):
    def __init__(self, n_embd: int, n_head: int):
        super().__init__()
        assert n_embd % n_head == 0
        self.n_head = n_head
        self.head_dim = n_embd // n_head
        self.qkv = nn.Linear(n_embd, 3 * n_embd, bias=False)
        self.proj = nn.Linear(n_embd, n_embd, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        B, T, C = x.shape
        q, k, v = self.qkv(x).split(C, dim=2)
        q = q.view(B, T, self.n_head, self.head_dim).transpose(1, 2)
        k = k.view(B, T, self.n_head, self.head_dim).transpose(1, 2)
        v = v.view(B, T, self.n_head, self.head_dim).transpose(1, 2)
        y = F.scaled_dot_product_attention(q, k, v, is_causal=True)
        y = y.transpose(1, 2).contiguous().view(B, T, C)
        return self.proj(y)


class MLP(nn.Module):
    def __init__(self, n_embd: int):
        super().__init__()
        self.fc = nn.Linear(n_embd, 4 * n_embd, bias=False)
        self.proj = nn.Linear(4 * n_embd, n_embd, bias=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.proj(F.gelu(self.fc(x)))


class Block(nn.Module):
    def __init__(self, n_embd: int, n_head: int):
        super().__init__()
        self.ln1 = nn.LayerNorm(n_embd)
        self.attn = CausalSelfAttention(n_embd, n_head)
        self.ln2 = nn.LayerNorm(n_embd)
        self.mlp = MLP(n_embd)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + self.attn(self.ln1(x))
        x = x + self.mlp(self.ln2(x))
        return x


class GPT(nn.Module):
    def __init__(
        self,
        vocab_size: int = VOCAB_SIZE,
        n_layer: int = N_LAYER,
        n_head: int = N_HEAD,
        n_embd: int = N_EMBD,
        block_size: int = BLOCK_SIZE,
    ):
        super().__init__()
        self.block_size = block_size
        self.tok_emb = nn.Embedding(vocab_size, n_embd)
        self.pos_emb = nn.Embedding(block_size, n_embd)
        self.blocks = nn.ModuleList([Block(n_embd, n_head) for _ in range(n_layer)])
        self.ln_f = nn.LayerNorm(n_embd)
        self.head = nn.Linear(n_embd, vocab_size, bias=False)
        self.head.weight = self.tok_emb.weight  # tied
        self.apply(self._init_weights)

    @staticmethod
    def _init_weights(m: nn.Module) -> None:
        if isinstance(m, nn.Linear):
            nn.init.normal_(m.weight, mean=0.0, std=0.02)
            if m.bias is not None:
                nn.init.zeros_(m.bias)
        elif isinstance(m, nn.Embedding):
            nn.init.normal_(m.weight, mean=0.0, std=0.02)

    def forward(
        self, idx: torch.Tensor, targets: torch.Tensor | None = None
    ) -> tuple[torch.Tensor, torch.Tensor | None]:
        _, T = idx.shape
        pos = torch.arange(T, device=idx.device)
        x = self.tok_emb(idx) + self.pos_emb(pos)[None, :, :]
        for block in self.blocks:
            x = block(x)
        x = self.ln_f(x)
        logits = self.head(x)
        loss = None
        if targets is not None:
            loss = F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
        return logits, loss


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def load_memmap(path: Path) -> np.ndarray:
    if not path.exists():
        raise FileNotFoundError(
            f"TinyStories token file missing: {path}. Set FINEWEB_DATA_DIR or run prep_data.py on the training host."
        )
    return np.memmap(path, dtype=np.uint16, mode="r")


def get_batch(
    data: np.ndarray,
    batch_size: int,
    block_size: int,
    device: str,
    rng: np.random.Generator,
) -> tuple[torch.Tensor, torch.Tensor]:
    ix = rng.integers(0, len(data) - block_size - 1, size=batch_size)
    x = np.stack([data[i : i + block_size].astype(np.int64) for i in ix])
    y = np.stack([data[i + 1 : i + 1 + block_size].astype(np.int64) for i in ix])
    tx = torch.from_numpy(x).to(device, non_blocking=True)
    ty = torch.from_numpy(y).to(device, non_blocking=True)
    return tx, ty


# ---------------------------------------------------------------------------
# LR schedule
# ---------------------------------------------------------------------------


def get_lr(step: int) -> float:
    if step < WARMUP_STEPS:
        return LR_MAX * step / WARMUP_STEPS
    progress = (step - WARMUP_STEPS) / max(1, TOTAL_STEPS - WARMUP_STEPS)
    progress = min(1.0, progress)
    return LR_MIN + 0.5 * (LR_MAX - LR_MIN) * (1 + math.cos(math.pi * progress))


# ---------------------------------------------------------------------------
# Training loop
# ---------------------------------------------------------------------------


def train(
    model: nn.Module,
    train_data: np.ndarray,
    device: str,
    seed: int = 42,
) -> None:
    model = model.to(device)
    use_amp = device == "cuda"
    amp_dtype = torch.bfloat16 if use_amp else torch.float32

    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=LR_MAX,
        betas=(BETA1, BETA2),
        weight_decay=WEIGHT_DECAY,
        fused=(device == "cuda"),
    )

    rng = np.random.default_rng(seed)
    model.train()
    t0 = time.time()
    running_loss = 0.0
    running_steps = 0

    for step in range(1, TOTAL_STEPS + 1):
        lr = get_lr(step)
        for g in optimizer.param_groups:
            g["lr"] = lr

        optimizer.zero_grad(set_to_none=True)
        for _ in range(GRAD_ACCUM):
            x, y = get_batch(train_data, BATCH_SIZE, BLOCK_SIZE, device, rng)
            with torch.amp.autocast(device_type=device, dtype=amp_dtype, enabled=use_amp):
                _, loss = model(x, y)
                loss = loss / GRAD_ACCUM
            loss.backward()
            running_loss += loss.item()  # already scaled; sum over accum = mean real loss

        torch.nn.utils.clip_grad_norm_(model.parameters(), GRAD_CLIP)
        optimizer.step()
        running_steps += 1

        if step % 50 == 0 or step == 1:
            avg = running_loss / running_steps
            print(
                f"step {step}/{TOTAL_STEPS}  loss={avg:.4f}  lr={lr:.2e}  time={time.time() - t0:.1f}s",
                flush=True,
            )
            running_loss = 0.0
            running_steps = 0


def main() -> None:
    device = detect_device()
    print(f"device: {device}")
    torch.manual_seed(42)

    train_data = load_memmap(DATA_DIR / "train.bin")
    val_data = load_memmap(DATA_DIR / "val.bin")
    print(f"train tokens: {len(train_data) / 1e6:.1f}M  val tokens: {len(val_data) / 1e6:.1f}M")

    model = GPT()
    n_params = sum(p.numel() for p in model.parameters())
    print(f"params: {n_params / 1e6:.2f}M")

    t_train_start = time.time()
    train(model, train_data, device=device)
    print(f"train_time: {time.time() - t_train_start:.1f}s")

    t_eval_start = time.time()
    evaluate(model, val_data, device=device)
    print(f"eval_time: {time.time() - t_eval_start:.1f}s")
    print(f"wall_time: {time.time() - t_train_start:.1f}s")


if __name__ == "__main__":
    main()
