"""Boot-time auto-resume — keep the story going across redeploys.

Run in the background by docker-entrypoint.sh just before the server launches.
Once the server is healthy, it finds the most recent run that has resumable
saved state and POSTs /api/world/resume so the world continues from where it
left off (same tick, entities, events) instead of sitting in an empty lobby.

No-ops safely when:
  - there is no resumable run yet (first boot / fresh volume) -> stays in lobby
  - a world is already active (someone started/resumed one) -> leaves it alone

Requires a persistent volume mounted at the runs directory (~/.worldseed),
otherwise run data is wiped on every redeploy and there is nothing to resume.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

_PORT = os.environ.get("PORT") or os.environ.get("LISTEN_PORT") or "8000"
_BASE = f"http://127.0.0.1:{_PORT}"


def _get_json(path: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{_BASE}{path}", timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def _wait_healthy(attempts: int = 120) -> bool:
    for _ in range(attempts):
        if _get_json("/health", timeout=2.0) is not None:
            return True
        time.sleep(1.0)
    return False


def main() -> None:
    from worldseed.persistence import list_runs, load_run

    # Latest run (list_runs is sorted newest-first) that still has saved state.
    target = next((r["run_id"] for r in list_runs() if load_run(r["run_id"])), None)
    if not target:
        print("[resume] no resumable run — staying in lobby", flush=True)
        return

    if not _wait_healthy():
        print("[resume] server health timeout — skipping auto-resume", flush=True)
        return

    # Don't clobber a world that's already active.
    health = _get_json("/health")
    if health and health.get("status") not in (None, "lobby"):
        print(f"[resume] world already active ({health.get('status')}) — skipping", flush=True)
        return

    data = json.dumps({"run_id": target}).encode()
    req = urllib.request.Request(
        f"{_BASE}/api/world/resume",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"[resume] resumed run {target}: HTTP {r.status}", flush=True)
    except Exception as exc:  # noqa: BLE001 — best-effort; lobby remains usable on failure
        print(f"[resume] resume failed for {target}: {exc}", flush=True)


if __name__ == "__main__":
    main()
