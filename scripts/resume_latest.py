"""Boot-time auto-resume — keep the story going across redeploys.

Run in the background by docker-entrypoint.sh just before the server launches.
Once the server is healthy, it finds the most recent run that has resumable
saved state and POSTs /api/world/resume so the world continues from where it
left off (same tick, entities, events) instead of sitting in an empty lobby.

Before resuming, it syncs the run's frozen ``scene.max_ticks`` with the current
on-disk scene config, so config changes (e.g. raising/removing the tick cap)
take effect on the resumed run instead of being pinned to the value baked in
when the run first started.

No-ops safely when:
  - there is no resumable run yet (first boot / fresh volume) -> stays in lobby
  - a world is already active (someone started/resumed one) -> leaves it alone

Requires a persistent volume mounted at the runs directory (~/.worldseed),
otherwise run data is wiped on every redeploy and there is nothing to resume.
"""

from __future__ import annotations

import glob
import json
import os
import time
import urllib.request

_PORT = os.environ.get("PORT") or os.environ.get("LISTEN_PORT") or "8000"
_BASE = f"http://127.0.0.1:{_PORT}"
_KEEP = object()  # sentinel: live scene config not found -> leave run config alone


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


def _live_max_ticks(scene_id: str) -> object:
    """max_ticks from the current on-disk scene config whose scene.id matches.

    Returns the value (int or None) or the _KEEP sentinel when no matching
    config is found, so we never blank out a cap we can't confirm.
    """
    import yaml

    for d in (os.environ.get("WORLDSEED_CONFIGS_DIR"), "/app/configs", "configs"):
        if not d or not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.yaml")) + glob.glob(os.path.join(d, "*.yml"))):
            try:
                cfg = yaml.safe_load(open(path, encoding="utf-8"))
            except Exception:
                continue
            scene = (cfg or {}).get("scene", {}) if isinstance(cfg, dict) else {}
            if scene.get("id") == scene_id:
                return scene.get("max_ticks", _KEEP)
    return _KEEP


def _sync_max_ticks(run_id: str, scene_id: str) -> None:
    """Patch the resumable run's frozen scene.max_ticks to match the live config."""
    import yaml

    from worldseed.persistence import run_dir

    live = _live_max_ticks(scene_id)
    if live is _KEEP:
        return
    cfg_path = run_dir(run_id) / "config.yaml"
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or "scene" not in data:
            return
        if data["scene"].get("max_ticks") == live:
            return
        old = data["scene"].get("max_ticks")
        data["scene"]["max_ticks"] = live
        cfg_path.write_text(yaml.dump(data, default_flow_style=False, allow_unicode=True), encoding="utf-8")
        print(f"[resume] synced scene.max_ticks {old} -> {live} for run {run_id}", flush=True)
    except Exception as exc:  # noqa: BLE001 — best-effort; resume with frozen config on failure
        print(f"[resume] max_ticks sync skipped for {run_id}: {exc}", flush=True)


def main() -> None:
    from worldseed.persistence import list_runs, load_run

    # Latest run (list_runs is sorted newest-first) that still has saved state.
    target = next((r["run_id"] for r in list_runs() if load_run(r["run_id"])), None)
    if not target:
        print("[resume] no resumable run — staying in lobby", flush=True)
        return

    run_data = load_run(target) or {}
    scene_id = (run_data.get("meta") or {}).get("scene_id", "")
    if scene_id:
        _sync_max_ticks(target, scene_id)

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
