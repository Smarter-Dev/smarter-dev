"""The bot's own model runs, for the usage ledger.

- ``POST /api/bot-usage`` → append one run's per-model spend, 200.

A chat turn's spend arrives with the turn (``chat_conversations``) and a
proactive wake's with the wake (``proactive_settings``). This is for the rest:
the bot's side agents, which are nobody's Chat spend. Rows are
``product_mode="discord"`` under the run's own ``operation_type`` (see
``smarter_dev.web.usage_metering``), priced per provider request.
"""

from __future__ import annotations

from litestar import Controller
from litestar import post
from litestar.status_codes import HTTP_200_OK
from skrift.auth.guards import APIKeyOnly
from skrift.auth.guards import Permission
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.web.api_native.auth import bot_api_auth_guard
from smarter_dev.web.api_native.errors import BOT_API_EXCEPTION_HANDLERS
from smarter_dev.web.api_native.schemas import BotUsageOperationRead
from smarter_dev.web.api_native.schemas import BotUsageOperationWrite
from smarter_dev.web.usage_metering import record_stages

BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE — Skrift's auth_guard inspects
# route_handler.guards for the APIKeyOnly marker (see model_overrides.py).
BOT_API_GUARDS = [bot_api_auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


def bot_operation_key(operation_type: str, operation_id: str) -> str:
    """The ledger key of a bot run: its own namespace, so a bot id can never
    collide with a chat, proactive or web search row's key."""
    return f"bot:{operation_type}:{operation_id}"


class BotUsageController(Controller):
    path = "/api/bot-usage"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS

    @post(status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def record_bot_usage(
        self, db_session: AsyncSession, data: BotUsageOperationWrite
    ) -> BotUsageOperationRead:
        details = {
            key: value
            for key, value in (
                ("guild_id", data.guild_id),
                ("channel_id", data.channel_id),
            )
            if value
        }
        if data.failed:
            details["failed"] = True
        recorded = await record_stages(
            db_session,
            [
                (
                    stage.model_name,
                    RequestTokens(
                        stage.input_tokens,
                        stage.output_tokens,
                        stage.cache_read_tokens,
                        stage.cache_write_tokens,
                    ),
                    [
                        RequestTokens(**request.model_dump())
                        for request in stage.requests
                    ],
                )
                for stage in data.stages
            ],
            operation_key=bot_operation_key(data.operation_type, data.operation_id),
            product_mode="discord",
            operation_type=data.operation_type,
            discord_user_id=data.discord_user_id,
            conversation_id=data.engagement_id,
            reasoning_level=data.reasoning_level,
            metered_at=data.metered_at,
            details=details,
        )
        await db_session.commit()
        return BotUsageOperationRead(recorded=recorded)
