"""Director-signal API routes.

External main agents (Codex / Claude / custom watcher subagents) read
attention signals and resolve DM requests over HTTP. The engine itself
remains runtime-neutral — these endpoints are the only seam.

Endpoints:
  GET  /api/director/signals?timeout_s=&limit=  — long-poll pending signals
  POST /api/director/signals/{id}/ack           — ack urgent / checkpoint
  GET  /api/director/dm/{id}                    — fetch a PendingDMRequest
  POST /api/director/dm/{id}/resolve            — apply DM judgment

`signals` GET does NOT drain. Callers ack urgent / checkpoint explicitly,
or resolve dm_request via the dedicated endpoint.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, FastAPI, HTTPException

from worldseed.server.models import (
    DirectorDMResolveRequest,
    DirectorSignalAckRequest,
)
from worldseed.server.routes._shared import _eng
from worldseed.server.websocket import ConnectionManager

log = structlog.get_logger()


_VALID_SIGNAL_TYPES = {"dm_request", "urgent", "checkpoint"}


def create_director_router(app: FastAPI, ws_manager: ConnectionManager) -> APIRouter:
    router = APIRouter()

    @router.get("/api/director/signals")
    async def get_signals(
        timeout_s: float = 30.0,
        limit: int | None = None,
        types: str | None = None,
    ) -> dict[str, Any]:
        """Long-poll for pending director signals.

        Returns immediately with whatever pending signals exist; otherwise
        polls every 0.5s for up to `timeout_s`. Returns `{signals: []}`
        on timeout. `types` is a comma-separated subset of
        dm_request,urgent,checkpoint.
        """
        type_filter: list[str] | None = None
        if types:
            type_filter = [t.strip() for t in types.split(",") if t.strip()]
            invalid = set(type_filter) - _VALID_SIGNAL_TYPES
            if invalid:
                raise HTTPException(400, detail=f"Invalid signal types: {sorted(invalid)}")

        deadline = asyncio.get_running_loop().time() + max(0.0, timeout_s)
        while True:
            eng = _eng(app)
            if not eng.director_enabled():
                # Director disabled — no point polling.
                return {"signals": []}
            pending = eng.peek_director_signals(limit=limit, types=type_filter)
            if pending:
                return {"signals": [s.to_dict() for s in pending]}
            if asyncio.get_running_loop().time() >= deadline:
                return {"signals": []}
            await asyncio.sleep(0.5)

    @router.post("/api/director/signals/{signal_id}/ack")
    async def ack_signal(
        signal_id: str,
        _req: DirectorSignalAckRequest | None = None,
    ) -> dict[str, Any]:
        eng = _eng(app)
        if not eng.director_enabled():
            raise HTTPException(409, detail="Director runtime disabled")
        sig = eng.director_runtime().get_signal(signal_id)
        if sig is None:
            raise HTTPException(404, detail=f"Signal '{signal_id}' not found")
        if sig.type == "dm_request":
            raise HTTPException(
                409,
                detail=f"Signal '{signal_id}' is a dm_request — use /api/director/dm/{{id}}/resolve",
            )
        if sig.status != "pending":
            raise HTTPException(409, detail=f"Signal '{signal_id}' is {sig.status}")
        eng.ack_director_signal(signal_id)
        return {"acked": True, "signal_id": signal_id}

    @router.get("/api/director/dm/{request_id}")
    async def get_dm_request(request_id: str) -> dict[str, Any]:
        eng = _eng(app)
        if not eng.director_enabled():
            raise HTTPException(409, detail="Director runtime disabled")
        req = eng.get_director_dm_request(request_id)
        if req is None:
            raise HTTPException(404, detail=f"DM request '{request_id}' not found")
        return req.to_dict()

    @router.post("/api/director/dm/{request_id}/resolve")
    async def resolve_dm_request(
        request_id: str,
        body: DirectorDMResolveRequest,
    ) -> dict[str, Any]:
        eng = _eng(app)
        if not eng.director_enabled():
            raise HTTPException(409, detail="Director runtime disabled")
        existing = eng.get_director_dm_request(request_id)
        if existing is None:
            raise HTTPException(404, detail=f"DM request '{request_id}' not found")
        if existing.status != "pending":
            raise HTTPException(409, detail=f"DM request is {existing.status}")
        ok, reason = eng.resolve_director_dm_request(
            request_id,
            narrative=body.narrative,
            effects_raw=body.effects,
        )
        if not ok:
            log.warning(
                "director_dm_resolve_failed",
                request_id=request_id,
                reason=reason,
            )
            raise HTTPException(400, detail=reason)
        log.info(
            "director_dm_resolved",
            request_id=request_id,
            narrative_len=len(body.narrative),
            effects_count=len(body.effects),
        )
        return {"resolved": True, "request_id": request_id}

    return router
