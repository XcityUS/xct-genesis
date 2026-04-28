"""Workspace bootstrap for autoresearch.

Creates the git-backed workspace structure that the engine's worker drives:

    $AUTORESEARCH_WORKSPACE/
    ├── .git/                  # shared git history
    ├── train_gpt.py           # seeded from baseline_template/, agents modify
    ├── evaluate.py            # LOCKED — agents cannot change
    ├── pyproject.toml         # LOCKED — dependencies pinned
    ├── .gitignore
    └── papers/                # engine-owned output dir

The ``baseline`` branch always points at the seeded template. Each
experiment submitted by an agent creates a new branch forked from
``baseline``, so ``git log baseline..experiment/...`` shows exactly what
the agent changed.

TinyStories token files (train.bin / val.bin) are NOT managed here —
they live on the EC2 training host under ``$FINEWEB_DATA_DIR`` (typically
``/data/tinystories``) and are baked into the AMI or prepared via
``infra/autoresearch_daemon/prep_data.py``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import structlog

from worldseed.autoresearch.paths import (
    main_worktree,
    papers_dir,
    workspace_root,
)

log = structlog.get_logger()


def baseline_template_dir() -> Path:
    """Path to the read-only template that seeds every workspace."""
    return Path(__file__).parent / "baseline_template"


def bootstrap_workspace(*, force_reset: bool = False) -> Path:
    """Set up (or reuse) the autoresearch workspace. Returns the workspace root.

    If the workspace already has a ``.git`` and a ``baseline`` branch, this
    function is a no-op (unless ``force_reset`` is True). Otherwise it:

    1. Creates the workspace directory tree
    2. Copies the baseline template files
    3. ``git init``, commits everything on ``baseline`` branch
    4. Creates the ``papers/`` directory
    """
    root = workspace_root()
    main = main_worktree()

    if force_reset and root.exists():
        log.warning("autoresearch_workspace_reset", path=str(root))
        shutil.rmtree(root)

    main.mkdir(parents=True, exist_ok=True)

    # Seed the baseline files if missing or when forcing reset
    template = baseline_template_dir()
    baseline_marker = main / "train_gpt.py"
    if not baseline_marker.exists():
        for src in template.iterdir():
            if src.name == "__pycache__":
                continue
            dst = main / src.name
            if src.is_file():
                shutil.copy2(src, dst)

    papers_dir().mkdir(parents=True, exist_ok=True)

    # Initialize git in the main worktree if not already a repo
    if not (main / ".git").exists():
        _git(main, "init", "-b", "baseline")
        _git(main, "config", "user.name", "autoresearch")
        _git(main, "config", "user.email", "autoresearch@worldseed.local")

    # Make sure the baseline commit exists
    status = subprocess.run(
        ["git", "log", "-1", "--oneline"],
        cwd=main,
        capture_output=True,
    )
    if status.returncode != 0 or not status.stdout.strip():
        _git(main, "add", ".")
        _git(main, "commit", "-m", "baseline: seed from autoresearch template")

    log.info("autoresearch_workspace_ready", path=str(root))
    return root


def _git(cwd: Path, *args: str) -> None:
    """Run a git command. Raises on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.decode(errors='replace').strip()}")
