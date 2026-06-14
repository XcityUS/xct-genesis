"""Model-id routing for LiteLLM call sites (DM judge + gazette generator).

Centralizes the xcity tokenhub first-class provider mapping so every place that
calls LiteLLM resolves credentials identically. Without this, only the call site
that remembered to route ``xcity/<id>`` would work — the gazette generator hits
LiteLLM with an unknown ``xcity/`` provider and fails.
"""

from __future__ import annotations

import os
from typing import Any

TOKENHUB_DEFAULT_BASE = "https://tokenhub.xcity.one/v1"


def xcity_credentials() -> tuple[str, str] | None:
    """Return (api_key, base_url) when the xcity tokenhub provider is configured.

    Independent of ``OPENAI_API_KEY`` so stock OpenAI can be configured alongside.
    """
    key = os.environ.get("XCT_TOKENHUB_API_KEY", "").strip()
    if not key:
        return None
    base = os.environ.get("XCT_TOKENHUB_API_BASE", "").strip() or TOKENHUB_DEFAULT_BASE
    return key, base


def resolve_litellm_call(model: str) -> tuple[str, dict[str, Any]]:
    """Map a worldseed model id to (litellm_model_id, extra_kwargs).

    ``xcity/<id>`` is rewritten to ``openai/<id>`` and routed with explicit
    ``api_key`` / ``api_base`` from ``XCT_TOKENHUB_*`` env vars, so the user's
    ``OPENAI_API_KEY`` stays reserved for stock OpenAI. Other model ids are
    returned unchanged, letting LiteLLM resolve credentials from its own env.
    """
    if model.startswith("xcity/"):
        creds = xcity_credentials()
        # Fall back to env even when the key is empty, so callers get a clear
        # auth error from the gateway rather than a confusing provider error.
        key = creds[0] if creds else os.environ.get("XCT_TOKENHUB_API_KEY", "")
        base = creds[1] if creds else TOKENHUB_DEFAULT_BASE
        return "openai/" + model[len("xcity/") :], {"api_key": key, "api_base": base}
    return model, {}
