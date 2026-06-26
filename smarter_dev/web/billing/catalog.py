"""Sudo offering catalog, sourced from Stripe.

Stripe is the single source of truth for the two sudo offerings. Each is a
Product tagged with ``metadata.sudo_role`` (``hacker`` / ``founder``), carrying
its perks in ``marketing_features``, its price in a single active Price, and
its Discord projection role IDs in metadata. Seed/refresh with
``scripts/seed_stripe_catalog.py``.

* **hacker** — recurring monthly Price ($8/mo).
* **founder** — one-time, pay-what-you-want Price (``custom_unit_amount`` with a
  $256 minimum).

Reads are served from an in-process cache with a stale-while-revalidate policy:
a request always gets the current cached catalog immediately; if the cache is
older than ``_TTL_SECONDS`` it triggers a single background refresh. Force a
refresh by restarting the pods.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import anyio

from smarter_dev.web.billing.client import get_stripe

_TTL_SECONDS = 15 * 60

_cache: list[dict[str, Any]] | None = None
_fetched_at: float = 0.0
_refreshing = False


def _select_price(prices: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the offering's active Price: prefer recurring, else one-time."""
    recurring = next((p for p in prices if p.get("recurring")), None)
    if recurring:
        return recurring
    return next((p for p in prices if p["type"] == "one_time"), None)


def _fetch_catalog_sync() -> list[dict[str, Any]]:
    """Pull offerings from Stripe. Runs the blocking SDK calls off the loop."""
    stripe = get_stripe()
    offerings: list[dict[str, Any]] = []
    for product in stripe.Product.list(limit=100, active=True).auto_paging_iter():
        # StripeObject's attribute access is hostile to dict ops; the JSON repr
        # is the reliable way to get a plain nested dict.
        data = json.loads(str(product))
        meta = data.get("metadata") or {}
        role = meta.get("sudo_role")
        if role not in ("hacker", "founder"):
            continue

        prices = json.loads(str(stripe.Price.list(product=data["id"], active=True)))
        price = _select_price(prices["data"])
        if not price:
            continue

        recurring = price.get("recurring") or None
        cua = price.get("custom_unit_amount") or None
        if cua:
            price_cents = int(cua.get("preset") or cua.get("minimum") or 0)
            min_cents = int(cua.get("minimum") or 0)
        else:
            price_cents = int(price.get("unit_amount") or 0)
            min_cents = price_cents

        offerings.append(
            {
                "id": role,
                "role": role,
                "name": data.get("name", ""),
                "desc": data.get("description") or "",
                "feats": [f["name"] for f in (data.get("marketing_features") or [])],
                "cta_label": meta.get("cta_label", ""),
                "hero": meta.get("hero") == "true",
                "order": int(meta.get("order", "0") or 0),
                "price_id": price["id"],
                "price_cents": price_cents,
                "min_cents": min_cents,
                "recurring": recurring is not None,
                "interval": recurring.get("interval") if recurring else None,
                "pay_what_you_want": cua is not None,
                # Discord projection IDs. Each product carries the same guild +
                # base role, redundantly, so any one read is sufficient.
                # ``discord_role_ids`` is a CSV of the roles to grant for this
                # offering on top of the base role.
                "discord_guild_id": meta.get("discord_guild_id", "") or None,
                "discord_base_role_id": meta.get("discord_base_role_id", "") or None,
                "discord_role_ids": [
                    p.strip()
                    for p in (meta.get("discord_role_ids") or "").split(",")
                    if p.strip()
                ],
            }
        )

    offerings.sort(key=lambda o: o["order"])
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
        offerings = await anyio.to_thread.run_sync(_fetch_catalog_sync)
        _cache = offerings
        _fetched_at = time.monotonic()
    finally:
        _refreshing = False


async def get_offerings() -> list[dict[str, Any]]:
    """Return the sudo offerings, cached with background refresh.

    On a cold cache this blocks on Stripe once. Afterwards it always returns
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
