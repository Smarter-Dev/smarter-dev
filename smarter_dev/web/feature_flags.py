"""Database-backed feature flags with three operating modes.

Modes:
  * ``enabled``    — everyone sees the feature
  * ``admin_only`` — only users with the ``administrator`` permission
  * ``disabled``   — nobody sees the feature

Reads pass through a small in-process TTL cache to avoid hammering the DB on
hot paths. The admin write path evicts the cached entry for the key.
"""

from __future__ import annotations

import time
from typing import Iterable

from litestar.connection import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.services import get_user_permissions

from smarter_dev.web.models import FeatureFlag


VALID_MODES: tuple[str, ...] = ("enabled", "admin_only", "disabled")

_CACHE_TTL_SECONDS: float = 30.0
_cache: dict[str, tuple[float, str]] = {}


def _cache_get(key: str) -> str | None:
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, mode = entry
    if expires_at < time.monotonic():
        _cache.pop(key, None)
        return None
    return mode


def _cache_set(key: str, mode: str) -> None:
    _cache[key] = (time.monotonic() + _CACHE_TTL_SECONDS, mode)


def invalidate(key: str | None = None) -> None:
    """Drop one entry from the cache, or clear it entirely."""
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


async def get_mode(
    session: AsyncSession,
    key: str,
    *,
    description: str | None = None,
) -> str:
    """Return the flag's current mode, auto-creating with ``disabled`` if absent.

    The ``description`` is stored only when the row is first created — later
    edits go through the admin UI.
    """
    cached = _cache_get(key)
    if cached is not None:
        return cached

    result = await session.execute(
        select(FeatureFlag).where(FeatureFlag.key == key)
    )
    flag = result.scalar_one_or_none()

    if flag is None:
        flag = FeatureFlag(key=key, mode="disabled", description=description)
        session.add(flag)
        await session.commit()

    _cache_set(key, flag.mode)
    return flag.mode


async def is_enabled(
    session: AsyncSession,
    key: str,
    request: Request,
    *,
    description: str | None = None,
) -> bool:
    """Resolve the flag for the request's current user.

    Returns True when the feature should be visible to this request:
      * ``enabled`` → always
      * ``admin_only`` → only when the user has the ``administrator`` permission
      * ``disabled`` → never
    """
    mode = await get_mode(session, key, description=description)
    if mode == "enabled":
        return True
    if mode == "disabled":
        return False

    user_id = request.session.get("user_id") if request.session else None
    if not user_id:
        return False
    perms = await get_user_permissions(session, user_id)
    return "administrator" in perms.permissions


async def set_mode(
    session: AsyncSession,
    key: str,
    mode: str,
    *,
    description: str | None = None,
) -> FeatureFlag:
    """Update (or create) a flag's mode. Invalidates the cache for ``key``."""
    if mode not in VALID_MODES:
        raise ValueError(
            f"Invalid feature flag mode {mode!r}; expected one of {VALID_MODES}"
        )

    result = await session.execute(
        select(FeatureFlag).where(FeatureFlag.key == key)
    )
    flag = result.scalar_one_or_none()
    if flag is None:
        flag = FeatureFlag(key=key, mode=mode, description=description)
        session.add(flag)
    else:
        flag.mode = mode
        if description is not None and flag.description != description:
            flag.description = description

    await session.commit()
    invalidate(key)
    return flag


async def list_flags(session: AsyncSession) -> list[FeatureFlag]:
    """List all flags, ordered by key."""
    result = await session.execute(
        select(FeatureFlag).order_by(FeatureFlag.key)
    )
    return list(result.scalars().all())


async def ensure_flags(
    session: AsyncSession, seeds: Iterable[tuple[str, str]]
) -> None:
    """Idempotently create flags listed in ``seeds`` (key, description).

    Existing rows are not touched. Useful for ensuring known flags show up in
    the admin UI before they've been touched by a runtime read.
    """
    for key, description in seeds:
        result = await session.execute(
            select(FeatureFlag).where(FeatureFlag.key == key)
        )
        if result.scalar_one_or_none() is None:
            session.add(
                FeatureFlag(
                    key=key, mode="disabled", description=description
                )
            )
    await session.commit()
