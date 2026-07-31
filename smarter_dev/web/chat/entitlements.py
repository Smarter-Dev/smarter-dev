"""Role-based Chat entitlement and spend-tier resolution.

Do not replace these checks with ``Permission('chat')`` at product boundaries:
Skrift intentionally lets administrators bypass permission guards, while Chat
is available only to the explicitly approved paid/internal roles.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

CHAT_ROLES = frozenset(
    {
        "sudo-hacker",
        "sudo-r",
        "sudo-rw",
        "sudo-rwx",
        "sudo-founder",
    }
)
ULTRA_CHAT_ROLES = frozenset({"sudo-rw", "sudo-rwx", "sudo-founder"})
SPEND_TIERS = ("hacker", "r", "rw", "rwx")


def _role_names(value: Any) -> frozenset[str]:
    """Normalize a Skrift permission result, user role objects, or strings."""
    roles = getattr(value, "roles", value)
    if roles is None:
        return frozenset()
    if isinstance(roles, str):
        return frozenset({roles})
    names: set[str] = set()
    for role in roles:
        name = getattr(role, "name", role)
        if name:
            names.add(str(name))
    return frozenset(names)


def has_chat(roles: Iterable[str] | Any) -> bool:
    return bool(_role_names(roles) & CHAT_ROLES)


def has_ultra_chat(roles: Iterable[str] | Any) -> bool:
    return bool(_role_names(roles) & ULTRA_CHAT_ROLES)


def resolve_spend_tier(roles: Iterable[str] | Any) -> str | None:
    """Resolve the highest applicable tier.

    Founder maps to ``rw`` unless ``sudo-rwx`` is also assigned. This ordering
    is intentionally independent from role iteration order.
    """
    names = _role_names(roles)
    if "sudo-rwx" in names:
        return "rwx"
    if "sudo-rw" in names or "sudo-founder" in names:
        return "rw"
    if "sudo-r" in names:
        return "r"
    if "sudo-hacker" in names:
        return "hacker"
    return None
