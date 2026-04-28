"""HTTP client for the always-on EC2 training daemon.

The daemon (``infra/autoresearch_daemon/server.py``) lives on a p4d.24xlarge
instance in us-east-1. It exposes a single POST /train endpoint that
receives a ``train_gpt.py`` source string + a target GPU id, runs it under
``CUDA_VISIBLE_DEVICES=<gpu_id>`` with the locked ``evaluate.py`` already
bundled, and returns the parsed ``val_loss``.

Config via env vars:
    AUTORESEARCH_DAEMON_URL   — e.g. http://3.82.x.y:8765  (required to enable)
    AUTORESEARCH_DAEMON_TOKEN — shared secret for auth     (required to enable)

If either is unset, ``EC2Runner.enabled`` is False and the worker emits
``experiment_crashed`` with reason ``daemon_not_configured`` for every
submitted experiment — there is no local training fallback.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger()


@dataclass
class TrainingResult:
    val_loss: float | None
    wall_time: float
    crash_reason: str | None  # None on success
    stdout_tail: str = ""


class GPUPool:
    """Allocates GPU ids 0..n-1 to concurrent training requests.

    FIFO queue; ``acquire()`` blocks until a GPU frees up, ``release()`` is
    sync and non-blocking. Safe because asyncio is cooperatively scheduled —
    between ``get`` and ``put_nowait`` there are no other awaits.
    """

    def __init__(self, n: int) -> None:
        self._available: asyncio.Queue[int] = asyncio.Queue()
        for i in range(n):
            self._available.put_nowait(i)
        self._n = n

    @property
    def size(self) -> int:
        return self._n

    async def acquire(self) -> int:
        return await self._available.get()

    def release(self, gpu_id: int) -> None:
        self._available.put_nowait(gpu_id)


class EC2Runner:
    """Thin HTTP client to the remote training daemon.

    One instance per worker. ``enabled`` is False if config is missing —
    callers should fall back to local execution in that case.
    """

    def __init__(
        self,
        base_url: str | None = None,
        auth_token: str | None = None,
        request_timeout: float = 900.0,
    ) -> None:
        self._base_url = base_url or os.environ.get("AUTORESEARCH_DAEMON_URL", "")
        self._token = auth_token or os.environ.get("AUTORESEARCH_DAEMON_TOKEN", "")
        self._timeout = request_timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._base_url and self._token)

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health_check(self) -> bool:
        try:
            client = await self._ensure_client()
            r = await client.get("/health", timeout=10.0)
            return r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            log.warning("ec2_daemon_health_failed", error=str(exc))
            return False

    async def run_training(
        self,
        source: str,
        gpu_id: int,
    ) -> TrainingResult:
        """Send source to daemon, wait for result.

        The daemon handles timeout / crash internally and responds 200 with
        ``crash`` set. HTTP-level errors (network drop, 5xx) map to
        crash_reason="http_error".
        """
        try:
            client = await self._ensure_client()
            r = await client.post(
                "/train",
                json={"source": source, "gpu_id": gpu_id},
            )
        except httpx.TimeoutException:
            return TrainingResult(
                val_loss=None,
                wall_time=self._timeout,
                crash_reason="http_timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason=f"http_error:{type(exc).__name__}",
            )

        if r.status_code == 401 or r.status_code == 403:
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason=f"auth_failed_{r.status_code}",
            )
        if r.status_code >= 500:
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason=f"daemon_{r.status_code}",
                stdout_tail=r.text[-500:],
            )
        if r.status_code != 200:
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason=f"http_{r.status_code}",
                stdout_tail=r.text[-500:],
            )

        data = r.json()
        return TrainingResult(
            val_loss=data.get("val_loss"),
            wall_time=float(data.get("wall_time", 0.0)),
            crash_reason=data.get("crash"),
            stdout_tail=data.get("stdout_tail", ""),
        )
