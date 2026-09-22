"""Shim: the two-pass core moved to smarter_dev.bot.proactive.models."""

from smarter_dev.bot.proactive.models import KIMI_OPENROUTER_MODEL_ID  # noqa: F401
from smarter_dev.bot.proactive.models import build_twopass_model  # noqa: F401
from smarter_dev.bot.proactive.models import build_watcher_runner  # noqa: F401
from smarter_dev.bot.proactive.models import ensure_openrouter_key_alias  # noqa: F401
from smarter_dev.bot.proactive.models import resolve_agent_model_id  # noqa: F401
