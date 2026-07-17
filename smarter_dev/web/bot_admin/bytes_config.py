"""Per-guild bytes economy configuration page for the Skrift admin panel.

Ports ``bytes_config`` from the legacy ``smarter_dev.web.admin.views`` onto a
Skrift-native Litestar controller under ``/admin/bot``. Guild identity comes
from Discord (via :mod:`smarter_dev.web.discord_admin_client`); the economy
settings live in the ``bytes_configs`` table.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Template as TemplateResponse
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.auth.guards import Permission, auth_guard
from skrift.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.shared.redis_client import get_redis_client
from smarter_dev.web.crud import BytesConfigOperations, NotFoundError
from smarter_dev.web.discord_admin_client import (
    DiscordAdminError,
    GuildNotFoundError,
    get_admin_discord_client,
)
from smarter_dev.web.models import BytesConfig

logger = logging.getLogger(__name__)

config_ops = BytesConfigOperations()

# The four scalar settings the form always submits; each must parse as an int.
_NUMERIC_FIELD_DEFAULTS: dict[str, int] = {
    "starting_balance": 100,
    "daily_amount": 10,
    "max_transfer": 1000,
    "transfer_cooldown_hours": 0,
}


def parse_bytes_config_form(form: Mapping[str, str]) -> dict:
    """Turn a submitted bytes-config form into keyword arguments for the model.

    Pure function — no I/O — so the parsing rules can be unit-tested directly.
    Mirrors the legacy view: the four scalar settings are required integers,
    ``streak_<days>_bonus`` fields build the streak-bonus map, and
    ``role_reward_<role_id>`` fields build the role-reward map. Blank or
    non-numeric streak/role values are skipped (matching legacy leniency).

    Raises:
        ValueError: If any of the four scalar settings is not a valid integer.
    """
    config_data: dict = {
        field: int(form.get(field, default))
        for field, default in _NUMERIC_FIELD_DEFAULTS.items()
    }

    streak_bonuses: dict[int, int] = {}
    for key, value in form.items():
        if key.startswith("streak_") and key.endswith("_bonus"):
            days = key[len("streak_") : -len("_bonus")]
            if days.isdigit() and value and str(value).isdigit():
                streak_bonuses[int(days)] = int(value)
    if streak_bonuses:
        config_data["streak_bonuses"] = streak_bonuses

    role_rewards: dict[str, int] = {}
    for key, value in form.items():
        if key.startswith("role_reward_"):
            role_id = key[len("role_reward_") :]
            if role_id and value and str(value).isdigit():
                role_rewards[role_id] = int(value)
    if role_rewards:
        config_data["role_rewards"] = role_rewards

    return config_data


async def load_or_create_config(
    db_session: AsyncSession, guild_id: str
) -> BytesConfig:
    """Return the guild's bytes config, creating a default row if none exists."""
    try:
        return await config_ops.get_config(db_session, guild_id)
    except NotFoundError:
        config = await config_ops.create_config(db_session, guild_id)
        await db_session.commit()
        return config


async def notify_bot_config_update(guild_id: str) -> None:
    """Best-effort Redis pub/sub notification so the bot refreshes its cache.

    Parity with the legacy view: a delivery failure must not fail the save, so
    Redis errors are logged and swallowed here rather than propagated.
    """
    try:
        redis_client = get_redis_client()
        await redis_client.publish(
            f"config_update:{guild_id}",
            json.dumps({"type": "bytes", "guild_id": guild_id}),
        )
    except RedisError as exc:
        logger.warning(
            "Failed to notify bot of bytes config update for guild %s: %s",
            guild_id,
            exc,
        )


class BytesConfigAdminController(Controller):
    """Per-guild bytes economy configuration under ``/admin/bot``."""

    path = "/admin/bot"
    guards = [auth_guard]

    @get(
        "/guilds/{guild_id:str}/bytes",
        guards=[auth_guard, Permission("administrator")],
    )
    async def bytes_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> TemplateResponse:
        """Render the bytes economy configuration form for one guild."""
        ctx = await get_admin_context(request, db_session)

        client = get_admin_discord_client()
        try:
            guild = await client.get_guild(guild_id)
        except GuildNotFoundError:
            return TemplateResponse(
                "admin/bot/guilds/error.html",
                context={
                    "error": f"Guild {guild_id} not found or bot is not a member.",
                    "error_code": 404,
                    "active_page": "bytes",
                    "guild_id": guild_id,
                    **ctx,
                },
                status_code=404,
            )
        except DiscordAdminError as exc:
            return TemplateResponse(
                "admin/bot/guilds/error.html",
                context={
                    "error": f"Discord API error: {exc}",
                    "error_code": 503,
                    "active_page": "bytes",
                    "guild_id": guild_id,
                    **ctx,
                },
                status_code=503,
            )

        config = await load_or_create_config(db_session, guild_id)

        return TemplateResponse(
            "admin/bot/bytes_config/form.html",
            context={
                "guild": guild,
                "config": config,
                "active_page": "bytes",
                "guild_id": guild_id,
                "flash_messages": get_flash_messages(request),
                **ctx,
            },
        )

    @post(
        "/guilds/{guild_id:str}/bytes",
        guards=[auth_guard, Permission("administrator")],
    )
    async def save_bytes_config(
        self, request: Request, db_session: AsyncSession, guild_id: str
    ) -> Redirect:
        """Persist submitted bytes economy settings, then redirect back."""
        redirect = Redirect(path=f"/admin/bot/guilds/{guild_id}/bytes")

        form = await request.form()
        try:
            config_data = parse_bytes_config_form(form)
        except (ValueError, TypeError):
            flash_error(
                request,
                "Invalid configuration values. Please check your input.",
            )
            return redirect

        try:
            await config_ops.update_config(db_session, guild_id, **config_data)
        except NotFoundError:
            await config_ops.create_config(db_session, guild_id, **config_data)
        await db_session.commit()

        await notify_bot_config_update(guild_id)

        flash_success(request, "Configuration updated successfully!")
        return redirect
