"""Per-channel proactive-bot settings API.

Mirrors the model-override controller's shape (one row per channel, PUT
upserts) for the two-pass proactive bot:

- ``GET    /api/guilds/{guild_id}/channels/{channel_id}/proactive-settings`` → row or 404.
- ``PUT    /api/guilds/{guild_id}/channels/{channel_id}/proactive-settings`` → upsert, 200.
- ``POST   /api/guilds/{guild_id}/channels/{channel_id}/proactive-settings/usage``
  → append one wake's per-model spend to the usage ledger, 200.

The bot treats a GET 404 as "disabled, no addendum" — the default for every
channel — so no DELETE is needed; disabling writes ``enabled=false``.
"""

from __future__ import annotations

from decimal import Decimal

from litestar import Controller, get, post, put
from litestar.status_codes import HTTP_200_OK
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission

from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.chat_conversations import _normalized_model_identity
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    nested_not_found_error,
)
from smarter_dev.web.api_native.schemas import (
    ProactiveChannelSettingsRead,
    ProactiveChannelSettingsWrite,
    ProactiveWakeUsageRead,
    ProactiveWakeUsageWrite,
)
from smarter_dev.web.crud import (
    get_proactive_channel_settings,
    upsert_proactive_channel_settings,
)
from smarter_dev.web.llm_pricing import calc_cost
from smarter_dev.web.models import UsageCostRow

BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE — Skrift's auth_guard inspects
# route_handler.guards for the APIKeyOnly marker (see model_overrides.py).
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


class ProactiveChannelSettingsController(Controller):
    """Per-channel proactive-bot settings (one row per channel, PUT upserts)."""

    path = "/api/guilds/{guild_id:str}/channels/{channel_id:str}/proactive-settings"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @get(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_settings(
        self,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
    ) -> ProactiveChannelSettingsRead:
        """Return the channel's proactive settings, or 404 if never configured."""
        record = await get_proactive_channel_settings(
            db_session, guild_id, channel_id
        )
        if record is None:
            raise nested_not_found_error(
                f"Proactive settings with identifier '{channel_id}' not found"
            )
        return ProactiveChannelSettingsRead.model_validate(record)

    @put(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def put_settings(
        self,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
        data: ProactiveChannelSettingsWrite,
    ) -> ProactiveChannelSettingsRead:
        """Upsert the channel's proactive settings and return the stored row."""
        record = await upsert_proactive_channel_settings(
            db_session,
            guild_id=guild_id,
            channel_id=channel_id,
            enabled=data.enabled,
            watch_addendum=data.watch_addendum,
        )
        # Serialize before commit (Skrift-injected session; see model_overrides).
        response = ProactiveChannelSettingsRead.model_validate(record)
        await db_session.commit()
        return response

    @post("/usage", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def record_wake_usage(
        self,
        db_session: AsyncSession,
        guild_id: str,
        channel_id: str,
        data: ProactiveWakeUsageWrite,
    ) -> ProactiveWakeUsageRead:
        """Append one wake's per-model spend to the usage ledger.

        Rows are keyed by the bot-generated wake id, so a retried report
        writes nothing the second time. Cost is priced server-side at each
        model's list price, mirroring the chat-turn ledger writes.
        """
        recorded = 0
        for entry in data.entries:
            operation_key = (
                f"proactive:{data.wake_id}:{entry.operation}:{entry.model_id}"
            )
            existing = await db_session.scalar(
                select(UsageCostRow).where(
                    UsageCostRow.operation_key == operation_key
                )
            )
            if existing is not None:
                continue
            provider, catalog_key, wire_id = _normalized_model_identity(
                entry.model_id
            )
            db_session.add(
                UsageCostRow(
                    operation_key=operation_key,
                    product_mode="discord",
                    operation_type=f"proactive-{entry.operation}",
                    provider_key=provider,
                    catalog_model_key=catalog_key,
                    model_id=wire_id,
                    input_tokens=entry.input_tokens,
                    output_tokens=entry.output_tokens,
                    cache_read_tokens=entry.cache_read_tokens,
                    cache_write_tokens=0,
                    cost_usd=calc_cost(
                        entry.input_tokens,
                        entry.output_tokens,
                        entry.model_id,
                        entry.cache_read_tokens,
                    ),
                    overage_cost_usd=Decimal("0"),
                    metered_at=data.metered_at,
                    details={
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "passive": data.passive,
                        "responses": data.responses,
                    },
                )
            )
            recorded += 1
        await db_session.commit()
        return ProactiveWakeUsageRead(recorded=recorded)
