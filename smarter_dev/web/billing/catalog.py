"""Sudo offering catalog, sourced from Polar.

Polar is the single source of truth for the two sudo offerings. Each is a
Product tagged with ``metadata.sudo_role`` (``hacker`` / ``founder``), carrying
its perks in numbered ``metadata.feature_<n>`` keys, its price as a single
active Price, and its Discord projection role IDs in metadata. Seed/refresh
with ``scripts/seed_polar_catalog.py``.

* **hacker** — recurring monthly product ($8/mo).
* **founder** — one-time, pay-what-you-want product (custom price with a
  $256 minimum).

Reads are served from an in-process cache with a stale-while-revalidate policy:
a request always gets the current cached catalog immediately; if the cache is
older than ``_TTL_SECONDS`` it triggers a single background refresh. Force a
refresh by restarting the pods.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from smarter_dev.web.billing.client import get_polar

_TTL_SECONDS = 15 * 60

_cache: list[dict[str, Any]] | None = None
_fetched_at: float = 0.0
_refreshing = False


def _price_shape(price: Any) -> tuple[int, int, bool] | None:
    """Return ``(price_cents, min_cents, pay_what_you_want)`` for a Polar price.

    Polar prices are one of ``fixed`` (``price_amount``), ``custom``
    (pay-what-you-want, with ``minimum_amount`` / ``preset_amount``), or
    ``free``. Returns ``None`` for free / unpriceable shapes.
    """
    if getattr(price, "minimum_amount", None) is not None:
        min_cents = int(price.minimum_amount or 0)
        price_cents = int(getattr(price, "preset_amount", None) or min_cents)
        return price_cents, min_cents, True
    if getattr(price, "price_amount", None) is not None:
        amount = int(price.price_amount)
        return amount, amount, False
    return None


def _select_price(prices: list[Any]) -> Any | None:
    """Pick the offering's active, priceable Price (skips archived / free)."""
    for price in prices:
        if getattr(price, "is_archived", False):
            continue
        if _price_shape(price) is not None:
            return price
    return None


def _features(meta: dict[str, Any]) -> list[str]:
    """Collect numbered ``feature_<n>`` metadata keys, ordered by index."""
    numbered: list[tuple[int, str]] = []
    for key, value in meta.items():
        if not key.startswith("feature_"):
            continue
        try:
            index = int(key[len("feature_"):])
        except ValueError:
            continue
        text = str(value).strip()
        if text:
            numbered.append((index, text))
    numbered.sort(key=lambda pair: pair[0])
    return [text for _, text in numbered]


def _offering_from_product(product: Any) -> dict[str, Any] | None:
    """Build an offering dict from a Polar product, or ``None`` if not a sudo
    offering / has no priceable price."""
    meta = dict(product.metadata or {})
    role = meta.get("sudo_role")
    if role not in ("hacker", "founder"):
        return None

    price = _select_price(list(product.prices or []))
    if price is None:
        return None
    price_cents, min_cents, pay_what_you_want = _price_shape(price)

    recurring = bool(getattr(product, "is_recurring", False))
    # ``recurring_interval`` is a SubscriptionRecurringInterval enum on the SDK
    # model; unwrap to its plain value ("month") for display.
    interval = getattr(product, "recurring_interval", None)
    interval = getattr(interval, "value", interval)
    discord_role_ids = [
        part.strip()
        for part in str(meta.get("discord_role_ids") or "").split(",")
        if part.strip()
    ]
    return {
        "id": role,
        "role": role,
        "name": product.name or "",
        "desc": product.description or "",
        "feats": _features(meta),
        "cta_label": str(meta.get("cta_label", "")),
        "hero": str(meta.get("hero")) == "true",
        "order": int(meta.get("order", "0") or 0),
        "price_id": price.id,
        "price_cents": price_cents,
        "min_cents": min_cents,
        "recurring": recurring,
        "interval": str(interval) if interval else None,
        "pay_what_you_want": pay_what_you_want,
        # Discord projection IDs. Each product carries the same guild + base
        # role, redundantly, so any one read is sufficient. ``discord_role_ids``
        # is a CSV of the roles to grant for this offering on top of the base.
        "product_id": product.id,
        "discord_guild_id": str(meta.get("discord_guild_id") or "") or None,
        "discord_base_role_id": str(meta.get("discord_base_role_id") or "") or None,
        "discord_role_ids": discord_role_ids,
    }


async def _fetch_catalog() -> list[dict[str, Any]]:
    """Pull the sudo offerings from Polar (async, over the SDK's httpx client)."""
    offerings: list[dict[str, Any]] = []
    async with get_polar() as polar:
        page = 1
        while True:
            response = await polar.products.list_async(
                is_archived=False, limit=100, page=page
            )
            if response is None:
                break
            items = list(response.result.items)
            for product in items:
                offering = _offering_from_product(product)
                if offering is not None:
                    offerings.append(offering)
            if len(items) < 100:
                break
            page += 1

    offerings.sort(key=lambda offering: offering["order"])
    return offerings


async def get_discord_config() -> dict[str, Any] | None:
    """Return the Discord projection config derived from product metadata.

    Shape: ``{"guild_id": str, "base_role_id": str, "role_ids_by_role":
    {"hacker": [...], "founder": [...]}}``, where each list is the extra roles
    granted on top of the base role. Returns ``None`` if the guild or base role
    is missing — converge then skips the Discord step (the Skrift role
    projection still runs).
    """
    offerings = await get_offerings()
    if not offerings:
        return None

    guild_id: str | None = None
    base_role_id: str | None = None
    role_ids_by_role: dict[str, list[str]] = {}
    for offering in offerings:
        if offering.get("discord_guild_id") and not guild_id:
            guild_id = offering["discord_guild_id"]
        if offering.get("discord_base_role_id") and not base_role_id:
            base_role_id = offering["discord_base_role_id"]
        role_ids_by_role[offering["role"]] = list(offering.get("discord_role_ids") or [])

    if not guild_id or not base_role_id:
        return None
    return {
        "guild_id": guild_id,
        "base_role_id": base_role_id,
        "role_ids_by_role": role_ids_by_role,
    }


async def _refresh() -> None:
    global _cache, _fetched_at, _refreshing
    try:
        _cache = await _fetch_catalog()
        _fetched_at = time.monotonic()
    finally:
        _refreshing = False


async def get_offerings() -> list[dict[str, Any]]:
    """Return the sudo offerings, cached with background refresh.

    On a cold cache this blocks on Polar once. Afterwards it always returns
    the cached catalog immediately and refreshes in the background past TTL.
    """
    global _refreshing

    if _cache is None:
        await _refresh()
        return _cache or []

    if time.monotonic() - _fetched_at > _TTL_SECONDS and not _refreshing:
        _refreshing = True
        asyncio.create_task(_refresh())

    return _cache


def get_offering(offerings: list[dict[str, Any]], role: str) -> dict[str, Any] | None:
    """Find an offering by role within an already-fetched catalog."""
    return next((o for o in offerings if o["role"] == role), None)
