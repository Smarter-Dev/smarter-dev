"""Project sudo entitlements onto Discord roles.

``converge(session, user_id)`` is the single source of truth for "what
Discord roles should this user hold right now": it reads the user's
active entitlement + their linked Discord ID, computes the desired
managed-role set, diffs against what Discord currently reports, and
applies the deltas via REST.

Properties:
- Idempotent. Running twice in a row is a no-op the second time.
- Restricted to the **managed role set** (base + r/w/x tier roles). The
  diff is intersected with the managed set so converge never touches
  any other role the user holds.
- Serialized per Discord user via an asyncio lock so two concurrent
  triggers can't race.
- Discord failures are logged, never raised — webhooks, the daily sweep,
  and member-join all heal drift on the next trigger.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.billing import catalog
from smarter_dev.web.models import SudoMembership

logger = logging.getLogger(__name__)


# Per-Discord-user lock registry, so two concurrent converges for the
# same user serialize. Keys are Discord user IDs (strings).
_USER_LOCKS: dict[str, asyncio.Lock] = {}
_REGISTRY_LOCK = asyncio.Lock()


async def _lock_for(discord_user_id: str) -> asyncio.Lock:
    async with _REGISTRY_LOCK:
        lock = _USER_LOCKS.get(discord_user_id)
        if lock is None:
            lock = asyncio.Lock()
            _USER_LOCKS[discord_user_id] = lock
        return lock


async def _get_active_entitlement(
    session: AsyncSession, user_id: UUID
) -> Optional[SudoMembership]:
    now = datetime.now(tz=timezone.utc)
    result = await session.execute(
        select(SudoMembership)
        .where(SudoMembership.user_id == user_id)
        .where(SudoMembership.revoked_reason.is_(None))
        .where(SudoMembership.expires_at > now)
        .order_by(SudoMembership.expires_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_discord_user_id(
    session: AsyncSession, user_id: UUID
) -> Optional[str]:
    """Return the linked Discord ``provider_account_id`` for this user, if
    any. Skrift's ``oauth_accounts`` table stores the Discord ID under
    ``provider='discord'``."""
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT provider_account_id FROM skrift.oauth_accounts "
            "WHERE user_id = :uid AND provider = 'discord' "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"uid": str(user_id)},
    )
    row = result.first()
    return row[0] if row else None


def _bot_token() -> Optional[str]:
    return os.environ.get("DISCORD_BOT_TOKEN")


def _audit_reason(reason: str) -> str:
    """Discord REST limits X-Audit-Log-Reason to 512 chars and rejects
    non-ASCII. Truncate + sanitize defensively."""
    text = "".join(c if 32 <= ord(c) < 127 else "?" for c in reason)
    return text[:500]


async def _fetch_member_roles(
    client: httpx.AsyncClient, guild_id: str, discord_user_id: str
) -> Optional[set[str]]:
    """GET /guilds/{g}/members/{uid}. Returns the user's current Discord
    role IDs as a set, or ``None`` if the member isn't in the guild (404).
    Other errors raise."""
    r = await client.get(f"/guilds/{guild_id}/members/{discord_user_id}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    body = r.json()
    return set(body.get("roles") or [])


async def _add_role(
    client: httpx.AsyncClient, guild_id: str, discord_user_id: str,
    role_id: str, reason: str,
) -> None:
    r = await client.put(
        f"/guilds/{guild_id}/members/{discord_user_id}/roles/{role_id}",
        headers={"X-Audit-Log-Reason": _audit_reason(reason)},
    )
    if r.status_code not in (204, 200):
        r.raise_for_status()


async def _remove_role(
    client: httpx.AsyncClient, guild_id: str, discord_user_id: str,
    role_id: str, reason: str,
) -> None:
    r = await client.delete(
        f"/guilds/{guild_id}/members/{discord_user_id}/roles/{role_id}",
        headers={"X-Audit-Log-Reason": _audit_reason(reason)},
    )
    if r.status_code not in (204, 200, 404):
        r.raise_for_status()


async def converge(session: AsyncSession, user_id: UUID) -> dict[str, list[str]]:
    """Bring the user's Discord managed-role set in line with their active
    entitlement.

    Returns a dict ``{"added": [...], "removed": [...]}`` describing the
    role IDs touched, for tests and logging. Never raises on Discord
    failures — the next trigger heals.
    """
    cfg = await catalog.get_discord_config()
    token = _bot_token()
    if cfg is None or not token:
        logger.info("converge: Discord projection not configured; skipping.")
        return {"added": [], "removed": []}

    guild_id = cfg["guild_id"]
    base_role_id = cfg["base_role_id"]
    role_ids_by_tier: dict[str, str] = cfg["role_ids_by_tier"]
    extras_by_tier: dict[str, list[str]] = cfg.get("extra_role_ids_by_tier") or {}
    # Managed set = base + every tier role + every extra. Roles outside
    # this set are never touched by converge.
    managed_ids: set[str] = {base_role_id, *role_ids_by_tier.values()}
    for extras in extras_by_tier.values():
        managed_ids.update(extras)

    discord_user_id = await _get_discord_user_id(session, user_id)
    if not discord_user_id:
        # No linked Discord account; nothing to project. The link/unlink
        # flows will re-trigger converge when the ID arrives.
        return {"added": [], "removed": []}

    entitlement = await _get_active_entitlement(session, user_id)

    desired: set[str] = set()
    reason_parts: list[str] = []
    if entitlement is not None:
        # Base role for "any active sudo member", plus the tier role,
        # plus any extras the tier bundles (Founder, 0day Founder).
        desired.add(base_role_id)
        tier_role = role_ids_by_tier.get(entitlement.tier)
        if tier_role is not None:
            desired.add(tier_role)
            reason_parts.append(f"tier={entitlement.tier}")
        for extra in extras_by_tier.get(entitlement.tier, ()):
            desired.add(extra)
        reason_parts.append(f"expires={entitlement.expires_at.isoformat()}")
    else:
        reason_parts.append("no active entitlement")

    reason = "sudo converge: " + ", ".join(reason_parts)

    async with await _lock_for(discord_user_id):
        async with httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            headers={"Authorization": f"Bot {token}"},
            timeout=10.0,
        ) as client:
            try:
                current = await _fetch_member_roles(client, guild_id, discord_user_id)
            except httpx.HTTPError as exc:
                logger.warning(
                    "converge: failed to fetch member %s in guild %s: %s",
                    discord_user_id, guild_id, exc,
                )
                return {"added": [], "removed": []}

            if current is None:
                # User is not in the guild yet. GUILD_MEMBER_ADD will
                # re-trigger converge when they join. No-op for now.
                return {"added": [], "removed": []}

            current_managed = current & managed_ids
            to_add = (desired - current_managed) & managed_ids
            to_remove = current_managed - desired

            added: list[str] = []
            removed: list[str] = []
            for role_id in to_add:
                try:
                    await _add_role(client, guild_id, discord_user_id, role_id, reason)
                    added.append(role_id)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "converge: add role %s to %s failed: %s",
                        role_id, discord_user_id, exc,
                    )
            for role_id in to_remove:
                try:
                    await _remove_role(client, guild_id, discord_user_id, role_id, reason)
                    removed.append(role_id)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "converge: remove role %s from %s failed: %s",
                        role_id, discord_user_id, exc,
                    )

            if added or removed:
                logger.info(
                    "converge: user=%s discord=%s added=%s removed=%s",
                    user_id, discord_user_id, added, removed,
                )
            return {"added": added, "removed": removed}


async def strip_discord_roles(discord_user_id: str) -> dict[str, list[str]]:
    """Strip every managed sudo role from a Discord member.

    Used by the unlink flow: when a user disconnects their Discord
    account, we don't have a site user to converge against any more, so
    we forcibly remove all managed roles from the abandoned Discord ID.
    Roles outside the managed set are untouched. Returns ``{"removed": [...]}``.
    """
    cfg = await catalog.get_discord_config()
    token = _bot_token()
    if cfg is None or not token:
        return {"removed": []}

    guild_id = cfg["guild_id"]
    managed_ids: set[str] = {cfg["base_role_id"], *cfg["role_ids_by_tier"].values()}
    for extras in (cfg.get("extra_role_ids_by_tier") or {}).values():
        managed_ids.update(extras)

    reason = _audit_reason("sudo converge: link unlinked")
    async with await _lock_for(discord_user_id):
        async with httpx.AsyncClient(
            base_url="https://discord.com/api/v10",
            headers={"Authorization": f"Bot {token}"},
            timeout=10.0,
        ) as client:
            try:
                current = await _fetch_member_roles(client, guild_id, discord_user_id)
            except httpx.HTTPError as exc:
                logger.warning(
                    "strip_discord_roles: failed to fetch member %s: %s",
                    discord_user_id, exc,
                )
                return {"removed": []}
            if current is None:
                return {"removed": []}

            removed: list[str] = []
            for role_id in current & managed_ids:
                try:
                    await _remove_role(client, guild_id, discord_user_id, role_id, reason)
                    removed.append(role_id)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "strip_discord_roles: remove %s from %s failed: %s",
                        role_id, discord_user_id, exc,
                    )
            if removed:
                logger.info(
                    "strip_discord_roles: discord=%s removed=%s",
                    discord_user_id, removed,
                )
            return {"removed": removed}
