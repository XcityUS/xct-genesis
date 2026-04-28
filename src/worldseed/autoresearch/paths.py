"""Filesystem paths for the autoresearch workspace.

Layout under ``AUTORESEARCH_WORKSPACE`` (defaults to ``~/autoresearch``):

    <workspace>/
    ├── .git/                      # shared git object store
    ├── main/                      # engine-owned worktree (papers, results.tsv)
    │   ├── papers/                # rendered paper_XXX.md files
    │   ├── results.tsv            # all experiments (including crashes)
    │   └── train_gpt.py           # baseline + accepted improvements
    └── worktrees/
        ├── alex_opt/              # per-agent worktree (Claude session cwd)
        ├── blair_arch/
        └── casey_reg/

The ``AUTORESEARCH_WORKSPACE`` env var overrides the default root so tests
can point at a tmp directory.
"""

from __future__ import annotations

import os
from pathlib import Path


def workspace_root() -> Path:
    """Return the autoresearch workspace root. Env-overridable."""
    raw = os.environ.get("AUTORESEARCH_WORKSPACE", "~/autoresearch")
    return Path(raw).expanduser().resolve()


def main_worktree() -> Path:
    """Engine-owned main worktree. Papers and results live here."""
    return workspace_root() / "main"


def papers_dir() -> Path:
    """Where rendered ``paper_XXX.md`` files live."""
    return main_worktree() / "papers"


def results_tsv() -> Path:
    """TSV log of every experiment (including crashes and rejected)."""
    return main_worktree() / "results.tsv"


def agent_worktree(agent_id: str) -> Path:
    """Per-agent worktree. Each agent's Claude session cwd is here."""
    return workspace_root() / "worktrees" / agent_id


def bare_git_dir() -> Path:
    """Shared bare git directory, seen by all worktrees."""
    return workspace_root() / ".git"
