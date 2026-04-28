"""Autoresearch scene — AI research community pretraining a small GPT on TinyStories.

This module provides the scene-specific glue for the autoresearch scene:
- ``effect_handler``: registers the per-action DSL effect operators
  (``autoresearch_propose_hypothesis``, ``autoresearch_run_experiment``,
  ``autoresearch_write_paper``, ``autoresearch_review_paper``) and the
  shared ``stash_and_dispatch`` wrapper that records ``last_action``.
- ``worker``: async background task that consumes ``ExperimentRequest``
  and ``VerifyRequest`` items from the pending queue, runs training with
  a GPU mutex, and emits result events. ``VerifyRequest`` is enqueued
  internally by ``write_paper`` (no separate verify_paper action).
- ``paper_renderer``: generates ``papers/paper_XXX.md`` from paper entities.
- ``bootstrap``: sets up the shared git workspace + 3 worktrees on scene
  start.

Importing this module registers the per-action effect operators so the
scene config can use them.
"""

from __future__ import annotations

from worldseed.autoresearch import effect_handler  # noqa: F401  # registers operator
