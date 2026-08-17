"""Shim: the two-pass core moved to smarter_dev.bot.proactive.models."""

from smarter_dev.bot.proactive.models import (  # noqa: F401
    KIMI_OPENROUTER_MODEL_ID,
    build_twopass_model,
    ensure_openrouter_key_alias,
    resolve_agent_model_id,
)
