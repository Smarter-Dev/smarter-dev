"""Model resolution for the two-pass bot's watcher and agent."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic_ai.models import Model

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from smarter_dev.bot.agents.chat_agent import build_agent_model  # noqa: E402
from smarter_dev.bot.agents.model_router import build_model_for  # noqa: E402
from smarter_dev.shared.model_catalog import (  # noqa: E402
    CatalogModel,
    ModelProvider,
)

KIMI_OPENROUTER_MODEL_ID = "moonshotai/kimi-k3"


def ensure_openrouter_key_alias() -> None:
    """The local .env spells the key OPEN_ROUTER_API_KEY; the router reads
    OPENROUTER_API_KEY (or legacy OPEN_ROUTER). Bridge the gap."""
    if not os.getenv("OPENROUTER_API_KEY") and os.getenv("OPEN_ROUTER_API_KEY"):
        os.environ["OPENROUTER_API_KEY"] = os.environ["OPEN_ROUTER_API_KEY"]


def _openrouter_key_present() -> bool:
    return bool(
        os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPEN_ROUTER")
        or os.getenv("OPEN_ROUTER_API_KEY")
    )


def resolve_agent_model_id(requested: str) -> str:
    """Route kimi-k3 through OpenRouter when no OpenCode Zen key is set."""
    if requested != "kimi-k3" or os.getenv("OPENCODE_ZEN_API_KEY"):
        return requested
    if _openrouter_key_present():
        print(
            "No OPENCODE_ZEN_API_KEY — routing Kimi K3 via OpenRouter as "
            f"{KIMI_OPENROUTER_MODEL_ID}",
            file=sys.stderr,
        )
        return KIMI_OPENROUTER_MODEL_ID
    raise SystemExit(
        "kimi-k3 needs OPENCODE_ZEN_API_KEY or an OpenRouter key "
        "(OPENROUTER_API_KEY / OPEN_ROUTER_API_KEY) in the environment."
    )


def build_twopass_model(model_id: str) -> Model:
    if model_id == KIMI_OPENROUTER_MODEL_ID:
        return build_model_for(
            CatalogModel(
                key="kimi-k3-openrouter",
                label="Kimi K3 (OpenRouter)",
                family="Kimi",
                provider=ModelProvider.OPENROUTER,
                model_id=KIMI_OPENROUTER_MODEL_ID,
            )
        )
    return build_agent_model(model_id)
