"""Smarter Dev web Chat product.

This package intentionally does not import the worker runtime at module import
so web-only processes stay free of provider SDK initialization.
"""

from .entitlements import has_chat
from .entitlements import has_ultra_chat
from .entitlements import resolve_spend_tier
from .policy import IntelligenceMode
from .policy import policy_for

__all__ = [
    "IntelligenceMode",
    "has_chat",
    "has_ultra_chat",
    "policy_for",
    "resolve_spend_tier",
]
