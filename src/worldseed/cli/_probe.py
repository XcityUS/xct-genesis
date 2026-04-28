"""Shared HTTP probes for CLI subcommands."""

from __future__ import annotations

import time

import httpx


def wait_for_health(base_url: str, *, attempts: int = 60, delay: float = 0.25) -> None:
    """Block until ``GET {base_url}/health`` responds 200, or raise RuntimeError."""
    last_error: Exception | None = None
    for _ in range(attempts):
        try:
            r = httpx.get(f"{base_url}/health", timeout=1)
            if r.status_code == 200:
                return
        except Exception as exc:  # noqa: BLE001 - startup probe
            last_error = exc
        time.sleep(delay)
    raise RuntimeError(f"server did not start at {base_url}: {last_error}")
