"""Settings routes: GET /api/settings, PATCH /api/settings, GET /api/models."""

from __future__ import annotations

import os
from typing import Any

import structlog
from fastapi import APIRouter, FastAPI

from worldseed.dm.routing import xcity_credentials
from worldseed.server.websocket import ConnectionManager

log = structlog.get_logger()

DEFAULT_DM_MODEL = ""


def _get_default_dm_model() -> str:
    """Return default DM model from env or built-in fallback."""
    return os.environ.get("WORLDSEED_DM_MODEL", DEFAULT_DM_MODEL)


def _custom_openai_base() -> str:
    """Return a custom OpenAI-compatible base URL if one is configured.

    Honours either OPENAI_BASE_URL or OPENAI_API_BASE. Empty string when pointing
    at stock OpenAI. Kept as a generic escape hatch — prefer ``XCT_TOKENHUB_*``
    when targeting xcity tokenhub.
    """
    return (os.environ.get("OPENAI_BASE_URL") or os.environ.get("OPENAI_API_BASE") or "").strip()


def _list_gateway_models(base: str, api_key: str, prefix: str) -> list[dict[str, Any]]:
    """List models served by an OpenAI-compatible gateway, tagged with ``prefix/<id>``.

    These gateways often host models LiteLLM's static catalog doesn't know about
    and under-report capabilities (e.g. ``function_calling=false`` even when tool
    or JSON-mode calls work), so the usual ``get_valid_models`` +
    ``supports_function_calling`` path returns nothing. We list everything the
    gateway's ``/models`` endpoint serves and let routing decide the handler.
    """
    import httpx

    resp = httpx.get(
        base.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return [{"id": f"{prefix}/{m['id']}"} for m in data if isinstance(m, dict) and m.get("id")]


def _get_available_models() -> dict[str, Any]:
    """Query for DM models available with the user's API keys.

    Sources, in order:
      1. xcity tokenhub (``XCT_TOKENHUB_API_KEY``) — listed as ``xcity/<id>``.
      2. Generic OpenAI-compatible gateway (``OPENAI_API_BASE``) — escape hatch,
         listed as ``openai/<id>``. When set, stock LiteLLM discovery is skipped
         because the gateway replaces OpenAI itself.
      3. Otherwise LiteLLM's stock per-provider discovery, filtered to chat
         models that support function calling.
    """
    try:
        import litellm  # noqa: F401  (import also triggers ./.env loading)
    except ImportError:
        return {"providers": [], "default": _get_default_dm_model()}

    providers: list[dict[str, Any]] = []

    # xcity tokenhub — first-class provider, independent of OPENAI_* env.
    xcity = xcity_credentials()
    if xcity:
        key, base = xcity
        try:
            xcity_models = _list_gateway_models(base, key, "xcity")
            if xcity_models:
                providers.append({"provider": "xcity", "models": xcity_models})
        except Exception:
            log.warning("xcity_model_list_failed", base=base, exc_info=True)

    # Generic OpenAI-compatible gateway escape hatch.
    base = _custom_openai_base()
    if base:
        try:
            openai_models = _list_gateway_models(base, os.environ.get("OPENAI_API_KEY", ""), "openai")
            if openai_models:
                providers.append({"provider": "openai", "models": openai_models})
        except Exception:
            log.warning("openai_compatible_model_list_failed", base=base, exc_info=True)
        return {"providers": providers, "default": _get_default_dm_model()}

    # Stock LiteLLM provider discovery.
    try:
        valid = litellm.get_valid_models(check_provider_endpoint=True)
    except Exception:
        log.warning("litellm_get_valid_models_failed", exc_info=True)
        return {"providers": providers, "default": _get_default_dm_model()}

    by_provider: dict[str, list[dict[str, Any]]] = {}
    for model_id in sorted(valid):
        if not litellm.supports_function_calling(model=model_id):
            continue
        provider = model_id.split("/")[0] if "/" in model_id else "openai"
        by_provider.setdefault(provider, []).append({"id": model_id})

    providers.extend({"provider": p, "models": ms} for p, ms in by_provider.items())

    return {"providers": providers, "default": _get_default_dm_model()}


def create_settings_router(app: FastAPI, ws_manager: ConnectionManager) -> APIRouter:
    router = APIRouter()

    @router.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        """Get current settings."""
        engine = app.state.engine
        return {
            "settings": app.state.settings,
            "running": engine is not None,
            "scene_id": (engine.config.scene.id if engine else None),
            "run_id": app.state.run_id,
            "tick": engine.tick if engine else 0,
        }

    @router.patch("/api/settings")
    async def update_settings(req: dict[str, Any]) -> dict[str, Any]:
        """Update settings. Hot settings apply immediately."""
        tr = app.state.tick_runner
        changed: list[str] = []

        # Hot settings (immediate, no restart)
        if "tick_interval" in req and tr:
            tr.set_interval(float(req["tick_interval"]))
            app.state.settings["tick_interval"] = req["tick_interval"]
            changed.append("tick_interval")

        if "max_ticks" in req:
            app.state.settings["max_ticks"] = req["max_ticks"]
            changed.append("max_ticks")

        if "timeout_min" in req:
            app.state.settings["timeout_min"] = req["timeout_min"]
            changed.append("timeout_min")

        if "max_dm_calls" in req:
            app.state.settings["max_dm_calls"] = req["max_dm_calls"]
            changed.append("max_dm_calls")

        if "language" in req:
            lang = req["language"]
            app.state.settings["language"] = lang
            engine = app.state.engine
            if engine is not None:
                engine.set_language(lang)
            changed.append("language")

        if "narrator_style" in req or "narrator_prompt" in req:
            engine = app.state.engine
            if engine is not None:
                engine.set_narrator_style(
                    style=req.get("narrator_style"),
                    prompt=req.get("narrator_prompt"),
                )
            if "narrator_style" in req:
                app.state.settings["narrator_style"] = req["narrator_style"]
            if "narrator_prompt" in req:
                app.state.settings["narrator_prompt"] = req["narrator_prompt"]
            changed.append("narrator_style")

        # Warm settings (need world restart)
        warm_keys = {"dm_model", "dm_fallback", "config_path"}
        warm_changed = warm_keys & set(req.keys())
        if warm_changed:
            for k in warm_changed:
                app.state.settings[k] = req[k]
                changed.append(k)

        return {"changed": changed, "settings": app.state.settings}

    @router.get("/api/models")
    async def list_models() -> dict[str, Any]:
        """Return available DM models based on configured API keys."""
        return _get_available_models()

    return router
