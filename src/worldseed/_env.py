"""Early .env loading.

LiteLLM loads ./.env as an import side-effect, but env vars like
WORLDSEED_DM_MODEL / WORLDSEED_DM_INSTRUCTOR_MODE / OPENAI_API_BASE are read by
the CLI (argparse defaults) and server before LiteLLM is imported. Calling
``load_env()`` at each entry point makes those vars available regardless of
import order. Safe to call repeatedly and when python-dotenv is absent.
"""

from __future__ import annotations


def load_env() -> None:
    """Load variables from a ./.env file into os.environ if available."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()
