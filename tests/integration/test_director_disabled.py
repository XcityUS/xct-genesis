"""Director omitted in YAML must produce byte-identical engine behavior.

If anything here regresses, the off-switch is leaking — the migration's
core compatibility contract.
"""

from __future__ import annotations

from pathlib import Path

from tests.helpers import make_world
from worldseed.scene.config import load_config

CONFIGS_DIR = Path(__file__).parent.parent.parent / "configs"


class TestDirectorDisabled:
    def test_engine_constructs_with_director_runtime_disabled(self) -> None:
        engine = make_world(CONFIGS_DIR / "teahouse.yaml")
        assert engine.director_enabled() is False

    def test_peek_returns_empty_when_disabled(self) -> None:
        engine = make_world(CONFIGS_DIR / "teahouse.yaml")
        assert engine.peek_director_signals() == []

    def test_step_does_not_enqueue_signals_when_disabled(self) -> None:
        engine = make_world(CONFIGS_DIR / "teahouse.yaml")
        for _ in range(5):
            engine.step()
        assert engine.peek_director_signals() == []

    def test_transient_includes_director_block_disabled(self) -> None:
        engine = make_world(CONFIGS_DIR / "teahouse.yaml")
        snapshot = engine._collect_transient()
        assert "director" in snapshot
        assert snapshot["director"]["enabled"] is False

    def test_director_block_roundtrip(self) -> None:
        engine = make_world(CONFIGS_DIR / "teahouse.yaml")
        snapshot = engine._collect_transient()
        engine2 = make_world(CONFIGS_DIR / "teahouse.yaml")
        engine2._restore_transient(snapshot)
        assert engine2.director_enabled() is False
        assert engine2.peek_director_signals() == []


class TestDirectorEnabledViaConfig:
    """Construct an engine with a programmatic director config to verify the
    hookup, without needing a YAML scene that opts in."""

    def test_enabled_when_config_has_director_enabled(self) -> None:
        cfg = load_config(CONFIGS_DIR / "teahouse.yaml")
        # Mutate the validated config — pydantic models are not frozen.
        from worldseed.models.config_schema import (
            DirectorCheckpointConfig,
            DirectorConfig,
        )

        cfg.director = DirectorConfig(
            enabled=True,
            dm_mode="signal",
            max_pending_dm=8,
            checkpoint=DirectorCheckpointConfig(every_events=2),
        )
        from worldseed.dm.providers.mock import MockDMProvider
        from worldseed.world import WorldEngine

        engine = WorldEngine(config=cfg, dm_provider=MockDMProvider())
        engine.register_from_config()
        assert engine.director_enabled() is True
        assert engine.peek_director_signals() == []
