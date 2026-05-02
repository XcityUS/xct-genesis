"""HTTP client for the always-on EC2 training daemon.

The daemon (``infra/autoresearch_daemon/server.py``) lives on a p4d.24xlarge
instance in us-east-1. New daemon versions expose a job API:

    POST /jobs -> {job_id}
    GET  /jobs/{job_id} -> queued/running/succeeded/failed/timed_out/cancelled

The old synchronous ``POST /train`` endpoint is still supported as a
compatibility fallback.

Config via env vars:
    AUTORESEARCH_DAEMON_URL   — e.g. http://3.82.x.y:8765  (required to enable)
    AUTORESEARCH_DAEMON_TOKEN — shared secret for auth     (required to enable)
    AUTORESEARCH_JOB_TIMEOUT  — max seconds to reconcile one job before cancel

If either is unset, ``EC2Runner.enabled`` is False and the worker emits
``experiment_crashed`` with reason ``daemon_not_configured`` for every
submitted experiment — there is no local training fallback.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

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
        request_timeout: float = 30.0,
        job_timeout: float | None = None,
        poll_interval: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url or os.environ.get("AUTORESEARCH_DAEMON_URL", "")
        self._token = auth_token or os.environ.get("AUTORESEARCH_DAEMON_TOKEN", "")
        self._timeout = request_timeout
        self._job_timeout = job_timeout or float(os.environ.get("AUTORESEARCH_JOB_TIMEOUT", "900"))
        self._poll_interval = poll_interval
        self._transport = transport
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
                transport=self._transport,
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
        """Submit source to the daemon, then poll until terminal result.

        A transient timeout while polling no longer means the experiment
        failed; it only means this client missed a status read. We keep
        reconciling against the daemon-owned job until the job reaches a
        terminal state or until the local reconcile budget expires, at which
        point we try to cancel the remote job before returning an infra error.
        """
        if not self.enabled:
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason="daemon_not_configured",
            )

        submit = await self._submit_job(source, gpu_id)
        if submit == "legacy":
            return await self._run_training_legacy(source, gpu_id)
        if isinstance(submit, TrainingResult):
            return submit

        job_id = submit
        deadline = time.monotonic() + self._job_timeout
        last_error = ""
        while time.monotonic() < deadline:
            polled = await self._poll_job(job_id)
            if isinstance(polled, TrainingResult):
                return polled
            if isinstance(polled, str):
                last_error = polled
            await asyncio.sleep(self._poll_interval)

        await self._cancel_job(job_id)
        return TrainingResult(
            val_loss=None,
            wall_time=self._job_timeout,
            crash_reason="job_status_timeout",
            stdout_tail=f"job_id={job_id}; last_poll_error={last_error}",
        )

    async def _submit_job(self, source: str, gpu_id: int) -> str | TrainingResult:
        """Return job_id, ``"legacy"`` fallback marker, or terminal failure."""
        try:
            client = await self._ensure_client()
            r = await client.post(
                "/jobs",
                json={"source": source, "gpu_id": gpu_id},
            )
        except httpx.TimeoutException:
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason="http_timeout",
                stdout_tail="submit /jobs timed out before job_id was returned",
            )
        except Exception as exc:  # noqa: BLE001
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason=f"http_error:{type(exc).__name__}",
            )

        if r.status_code in (404, 405):
            return "legacy"
        failure = self._http_failure(r)
        if failure is not None:
            return failure

        data = r.json()
        job_id = str(data.get("job_id") or "")
        if not job_id:
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason="daemon_bad_response",
                stdout_tail=r.text[-500:],
            )
        terminal = self._training_result_from_job(data)
        if terminal is not None:
            return terminal
        return job_id

    async def _poll_job(self, job_id: str) -> TrainingResult | str | None:
        """Return terminal result, transient error string, or None if running."""
        try:
            client = await self._ensure_client()
            r = await client.get(f"/jobs/{job_id}")
        except httpx.TimeoutException:
            return "http_timeout"
        except Exception as exc:  # noqa: BLE001
            return f"http_error:{type(exc).__name__}"

        failure = self._http_failure(r)
        if failure is not None:
            return failure
        data = r.json()
        return self._training_result_from_job(data)

    async def _cancel_job(self, job_id: str) -> None:
        try:
            client = await self._ensure_client()
            await client.post(f"/jobs/{job_id}/cancel")
        except Exception as exc:  # noqa: BLE001
            log.warning("ec2_daemon_cancel_failed", job_id=job_id, error=str(exc))

    def _training_result_from_job(self, data: dict[str, Any]) -> TrainingResult | None:
        status = data.get("status")
        if status in {"queued", "running"}:
            return None
        if status == "succeeded":
            return TrainingResult(
                val_loss=data.get("val_loss"),
                wall_time=float(data.get("wall_time", 0.0) or 0.0),
                crash_reason=None,
                stdout_tail=str(data.get("stdout_tail") or ""),
            )
        if status in {"failed", "timed_out", "cancelled"}:
            return TrainingResult(
                val_loss=None,
                wall_time=float(data.get("wall_time", 0.0) or 0.0),
                crash_reason=str(data.get("crash") or status),
                stdout_tail=str(data.get("stdout_tail") or ""),
            )
        return TrainingResult(
            val_loss=None,
            wall_time=float(data.get("wall_time", 0.0) or 0.0),
            crash_reason=f"unknown_job_status:{status}",
            stdout_tail=str(data)[-500:],
        )

    def _http_failure(self, r: httpx.Response) -> TrainingResult | None:
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
        if r.status_code == 404:
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason="job_not_found",
                stdout_tail=r.text[-500:],
            )
        if r.status_code != 200:
            return TrainingResult(
                val_loss=None,
                wall_time=0.0,
                crash_reason=f"http_{r.status_code}",
                stdout_tail=r.text[-500:],
            )
        return None

    async def _run_training_legacy(
        self,
        source: str,
        gpu_id: int,
    ) -> TrainingResult:
        """Compatibility path for older daemons that only expose POST /train."""
        legacy_timeout = float(os.environ.get("AUTORESEARCH_LEGACY_TRAIN_TIMEOUT", "900"))
        try:
            client = await self._ensure_client()
            r = await client.post(
                "/train",
                json={"source": source, "gpu_id": gpu_id},
                timeout=legacy_timeout,
            )
        except httpx.TimeoutException:
            return TrainingResult(
                val_loss=None,
                wall_time=legacy_timeout,
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
