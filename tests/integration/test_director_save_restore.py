"""Director state must survive pause/resume across the full WorldEngine path.

Audit gap: only DirectorQueue in isolation was round-tripped. End-to-end
save_state → load_state with live signals + DM requests + checkpoint cursor
was untested.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from worldseed.engine.inbox import InboxEvent
from worldseed.models.config_schema import (
    DirectorCheckpointConfig,
    DirectorConfig,
)
from worldseed.persistence import RunRecorder
from worldseed.scene.config import load_config
from worldseed.world import WorldEngine

CONFIGS_DIR = Path(__file__).parent.parent.parent / "configs"


def _make_engine(tmp_path: Path, run_id: str) -> WorldEngine:
    cfg = load_config(CONFIGS_DIR / "teahouse.yaml")
    cfg.director = DirectorConfig(
        enabled=True,
        dm_mode="signal",
        max_pending_dm=8,
        checkpoint=DirectorCheckpointConfig(
            every_events=2, every_minutes=None, every_ticks=None, on_event_types=[]
        ),
    )
    recorder = RunRecorder(
        run_id=run_id,
        config_path=CONFIGS_DIR / "teahouse.yaml",
        scene_id="teahouse",
        dm_model="none",
    )
    engine = WorldEngine(config=cfg, recorder=recorder)
    engine.register_from_config()
    return engine


class TestSaveRestoreDirectorQueue:
    @pytest.mark.asyncio
    async def test_pending_signals_survive(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WORLDSEED_HOME", str(tmp_path / ".worldseed"))

        engine = _make_engine(tmp_path, run_id="save_restore_signals")
        # Trigger an urgent signal
        target = next(a for a in engine.get_registered_agents() if a != "narrator")
        other = next(
            a for a in engine.get_registered_agents() if a != target and a != "narrator"
        )
        engine.inbox_manager.get_or_create(target).append_event(
            InboxEvent(tick=0, type="alert", source=other, detail="x", push=True)
        )
        engine._observe_attention()

        urgents_before = engine.peek_director_signals(types=["urgent"])
        assert len(urgents_before) == 1

        snapshot = engine._collect_transient()

        engine2 = _make_engine(tmp_path, run_id="save_restore_signals_2")
        engine2._restore_transient(snapshot)
        urgents_after = engine2.peek_director_signals(types=["urgent"])
        assert len(urgents_after) == 1
        assert urgents_after[0].target_agent_id == target

    @pytest.mark.asyncio
    async def test_pending_dm_request_survives(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORLDSEED_HOME", str(tmp_path / ".worldseed"))

        engine = _make_engine(tmp_path, run_id="save_restore_dm")
        from worldseed.models.action import ActionSubmission

        actor = next(a for a in engine.get_registered_agents() if a != "narrator")
        engine._queue.submit(
            ActionSubmission(
                agent_id=actor,
                action_type="attempt",
                params={"description": "looks around"},
            )
        )
        await engine.step_async()

        pending_before = engine.peek_director_signals(types=["dm_request"])
        assert len(pending_before) == 1
        request_id = pending_before[0].refs["dm_request_id"]
        assert engine.director_runtime().pending_dm_count() == 1

        snapshot = engine._collect_transient()

        engine2 = _make_engine(tmp_path, run_id="save_restore_dm_2")
        engine2._restore_transient(snapshot)

        # _dm_to_signal index rebuilt → resolve still finds the linked signal.
        ok, reason = engine2.resolve_director_dm_request(
            request_id, narrative="resolved post-restore", effects_raw=[]
        )
        assert ok, reason
        assert engine2.peek_director_signals(types=["dm_request"]) == []
        assert engine2.director_runtime().pending_dm_count() == 0


class TestCheckpointCursorAcrossResume:
    """The cursor uses EventLog.total_appended; if not seeded on restore, the
    saved cursor may exceed the freshly-restored counter and silently suppress
    cadence. Verify this regression is fixed."""

    def test_cursor_seeded_so_cadence_resumes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("WORLDSEED_HOME", str(tmp_path / ".worldseed"))

        engine = _make_engine(tmp_path, run_id="cursor_resume")
        # Append several events to advance total_appended, then expire most.
        from worldseed.models.event import Event

        for i in range(50):
            engine.event_log.append(
                Event(tick=0, type="say", source="x", detail=f"e{i}", ttl=1, scope="global")
            )
        engine.event_log.cleanup(current_tick=10)  # most events expire
        engine._observe_attention()

        before_total = engine.event_log.total_appended
        snapshot = engine._collect_transient()

        engine2 = _make_engine(tmp_path, run_id="cursor_resume_2")
        engine2._restore_transient(snapshot)

        # The restored counter must be at least the saved one — otherwise
        # the director cursor would point past the live log and miscount.
        assert engine2.event_log.total_appended >= before_total
