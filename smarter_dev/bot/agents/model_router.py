"""Compatibility re-export of :mod:`smarter_dev.shared.model_router`.

This module was a byte-identical copy of the shared router until 2026-08-13.
Two copies meant a routing change had to be made twice to take effect, and
missing one would apply it to only half the callers — the bot's summarizer,
rule matcher and dream job import from here, while chat jobs, the document
stream and the handler agent import from :mod:`smarter_dev.shared.model_router`.
That is exactly the failure mode OpenRouter endpoint constraints cannot afford:
a request that loses its ``provider`` block does not error, it just quietly gets
whichever endpoint is cheapest, which is the most quantized one.

Import from :mod:`smarter_dev.shared.model_router` in new code. Patch targets in
tests must name the shared module too — patching a name here would rebind only
this alias, leaving the real implementation untouched.
"""

from __future__ import annotations

from smarter_dev.shared.model_router import DIGITALOCEAN_API_KEY_ENV_VAR
from smarter_dev.shared.model_router import OPENCODE_ZEN_API_KEY_ENV_VAR
from smarter_dev.shared.model_router import OPENROUTER_API_KEY_ENV_VAR
from smarter_dev.shared.model_router import OPENROUTER_API_KEY_LEGACY_ENV_VAR
from smarter_dev.shared.model_router import build_model_for
from smarter_dev.shared.model_router import model_settings_for

__all__ = [
    "DIGITALOCEAN_API_KEY_ENV_VAR",
    "OPENCODE_ZEN_API_KEY_ENV_VAR",
    "OPENROUTER_API_KEY_ENV_VAR",
    "OPENROUTER_API_KEY_LEGACY_ENV_VAR",
    "build_model_for",
    "model_settings_for",
]
