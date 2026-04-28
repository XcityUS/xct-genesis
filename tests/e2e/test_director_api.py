"""E2E smoke tests for /api/director/* routes.

Director-disabled scenes (default) get a 409 or empty response, never 500.
Director-enabled scenes can read and ack signals through the API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.helpers import CONFIGS_DIR
from worldseed.persistence import RunRecorder
from worldseed.server.app import create_app
from worldseed.world import WorldEngine


@pytest_asyncio.fixture
async def director_disabled_env(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """Engine constructed with director omitted."""
    config_path = CONFIGS_DIR / "teahouse.yaml"
    recorder = RunRecorder(
        run_id="director_disabled",
        config_path=config_path,
        scene_id="teahouse",
        dm_model="none",
    )
    engine = WorldEngine(config_path, recorder=recorder)
    app = create_app(engine, tick_interval=0.1, run_id="director_disabled")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"engine": engine, "client": client}


@pytest_asyncio.fixture
async def director_enabled_env(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """Engine constructed with director enabled programmatically."""
    from worldseed.models.config_schema import (
        DirectorCheckpointConfig,
        DirectorConfig,
    )
    from worldseed.scene.config import load_config

    config_path = CONFIGS_DIR / "teahouse.yaml"
    cfg = load_config(config_path)
    cfg.director = DirectorConfig(
        enabled=True,
        dm_mode="signal",
        max_pending_dm=8,
        checkpoint=DirectorCheckpointConfig(every_events=2),
    )
    recorder = RunRecorder(
        run_id="director_enabled",
        config_path=config_path,
        scene_id="teahouse",
        dm_model="none",
    )
    engine = WorldEngine(config=cfg, recorder=recorder)
    engine.register_from_config()
    app = create_app(engine, tick_interval=0.1, run_id="director_enabled")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"engine": engine, "client": client}


class TestDirectorDisabled:
    @pytest.mark.asyncio
    async def test_signals_returns_empty_immediately(
        self,
        director_disabled_env: dict[str, Any],
    ) -> None:
        client = director_disabled_env["client"]
        r = await client.get("/api/director/signals?timeout_s=0")
        assert r.status_code == 200
        assert r.json() == {"signals": []}

    @pytest.mark.asyncio
    async def test_ack_returns_409(self, director_disabled_env: dict[str, Any]) -> None:
        client = director_disabled_env["client"]
        r = await client.post("/api/director/signals/abc/ack", json={})
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_get_dm_returns_409(self, director_disabled_env: dict[str, Any]) -> None:
        client = director_disabled_env["client"]
        r = await client.get("/api/director/dm/abc")
        assert r.status_code == 409


class TestDirectorEnabled:
    @pytest.mark.asyncio
    async def test_signals_empty_when_no_activity(
        self,
        director_enabled_env: dict[str, Any],
    ) -> None:
        client = director_enabled_env["client"]
        r = await client.get("/api/director/signals?timeout_s=0")
        assert r.status_code == 200
        assert r.json() == {"signals": []}

    @pytest.mark.asyncio
    async def test_invalid_type_filter_400(
        self,
        director_enabled_env: dict[str, Any],
    ) -> None:
        client = director_enabled_env["client"]
        r = await client.get("/api/director/signals?timeout_s=0&types=bogus")
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_ack_unknown_signal_404(
        self,
        director_enabled_env: dict[str, Any],
    ) -> None:
        client = director_enabled_env["client"]
        r = await client.post("/api/director/signals/nope/ack", json={})
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_get_unknown_dm_404(
        self,
        director_enabled_env: dict[str, Any],
    ) -> None:
        client = director_enabled_env["client"]
        r = await client.get("/api/director/dm/nope")
        assert r.status_code == 404
