from __future__ import annotations

import httpx
import pytest

from worldseed.autoresearch.ec2_runner import EC2Runner
from worldseed.autoresearch.worker import AutoresearchWorker
from worldseed.engine.event_log import EventLog
from worldseed.engine.state_store import StateStore


def _job_payload(status: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "job_id": "job-1",
        "gpu_id": 0,
        "status": status,
        "created_at": 1.0,
        "started_at": None,
        "completed_at": None,
        "wall_time": 0.0,
        "val_loss": None,
        "crash": None,
        "stdout_tail": "",
        "cancel_requested": False,
    }
    payload.update(extra)
    return payload


@pytest.mark.asyncio
async def test_run_training_uses_job_api_and_polls_to_success() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/jobs":
            return httpx.Response(200, json=_job_payload("queued"))
        if request.method == "GET" and request.url.path == "/jobs/job-1":
            return httpx.Response(
                200,
                json=_job_payload(
                    "succeeded",
                    val_loss=2.3456,
                    wall_time=123.0,
                    stdout_tail="val_loss: 2.3456",
                ),
            )
        return httpx.Response(404)

    runner = EC2Runner(
        base_url="http://daemon",
        auth_token="token",
        poll_interval=0.001,
        job_timeout=1.0,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await runner.run_training("print('train')", gpu_id=0)
    finally:
        await runner.close()

    assert result.crash_reason is None
    assert result.val_loss == 2.3456
    assert ("POST", "/train") not in calls


@pytest.mark.asyncio
async def test_run_training_reconciles_poll_timeout_before_success() -> None:
    poll_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_count
        if request.method == "POST" and request.url.path == "/jobs":
            return httpx.Response(200, json=_job_payload("queued"))
        if request.method == "GET" and request.url.path == "/jobs/job-1":
            poll_count += 1
            if poll_count == 1:
                raise httpx.ReadTimeout("poll timed out", request=request)
            return httpx.Response(200, json=_job_payload("succeeded", val_loss=2.1, wall_time=10.0))
        return httpx.Response(404)

    runner = EC2Runner(
        base_url="http://daemon",
        auth_token="token",
        poll_interval=0.001,
        job_timeout=1.0,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await runner.run_training("print('train')", gpu_id=0)
    finally:
        await runner.close()

    assert result.crash_reason is None
    assert result.val_loss == 2.1
    assert poll_count == 2


@pytest.mark.asyncio
async def test_run_training_falls_back_to_legacy_train_for_old_daemon() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/jobs":
            return httpx.Response(404)
        if request.method == "POST" and request.url.path == "/train":
            return httpx.Response(
                200,
                json={"val_loss": 2.5, "wall_time": 100.0, "crash": None, "stdout_tail": ""},
            )
        return httpx.Response(404)

    runner = EC2Runner(
        base_url="http://daemon",
        auth_token="token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await runner.run_training("print('train')", gpu_id=0)
    finally:
        await runner.close()

    assert result.crash_reason is None
    assert result.val_loss == 2.5
    assert calls == [("POST", "/jobs"), ("POST", "/train")]


@pytest.mark.asyncio
async def test_run_training_cancels_remote_job_when_status_never_reconciles() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path == "/jobs":
            return httpx.Response(200, json=_job_payload("queued"))
        if request.method == "GET" and request.url.path == "/jobs/job-1":
            return httpx.Response(200, json=_job_payload("running"))
        if request.method == "POST" and request.url.path == "/jobs/job-1/cancel":
            return httpx.Response(200, json=_job_payload("cancelled", crash="cancelled"))
        return httpx.Response(404)

    runner = EC2Runner(
        base_url="http://daemon",
        auth_token="token",
        poll_interval=0.001,
        job_timeout=0.005,
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await runner.run_training("print('train')", gpu_id=0)
    finally:
        await runner.close()

    assert result.crash_reason == "job_status_timeout"
    assert ("POST", "/jobs/job-1/cancel") in calls


def test_worker_retries_only_infrastructure_failures() -> None:
    worker = AutoresearchWorker(StateStore(), EventLog(), gpu_count=1)

    assert worker._is_retryable_infra_failure("http_timeout")
    assert worker._is_retryable_infra_failure("http_error:ConnectError")
    assert worker._is_retryable_infra_failure("daemon_503")
    assert worker._is_retryable_infra_failure("job_status_timeout")
    assert worker._is_retryable_infra_failure("daemon_restarted")

    assert not worker._is_retryable_infra_failure("timeout")
    assert not worker._is_retryable_infra_failure("exit_1")
    assert not worker._is_retryable_infra_failure("syntax_error: line 1")
