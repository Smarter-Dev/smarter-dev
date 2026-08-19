"""Per-channel chat-bot configuration, reachable from `/configure bot`.

The first per-channel admin page in the bot dashboard, and the first that a
guild moderator (rather than a site administrator) can open: it authorizes
on the signed link the slash command minted, because the site's own admin
auth is a global Skrift permission with no per-guild notion.

It consolidates what the configuration slash commands write today — which
bot serves the channel (proactive or legacy), plus the model, reasoning
level, budgets and behaviour flags — into one form.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from litestar import Controller, Request, get, post
from litestar.response import Redirect, Template as TemplateResponse
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.flash import flash_error, flash_success, get_flash_messages

from smarter_dev.shared.config_links import (
    CONFIG_LINK_PATH,
    ConfigLinkPayload,
    verify_config_link,
)
from smarter_dev.shared.model_catalog import (
    MODEL_CATALOG,
    ReasoningLevel,
    is_valid_model_key,
)
from smarter_dev.web.crud import (
    get_channel_model_override,
    get_proactive_channel_settings,
    upsert_channel_model_override,
    upsert_proactive_channel_settings,
)

logger = logging.getLogger(__name__)

MAX_INT32 = 2_147_483_647
_VALID_REASONING = {level.value for level in ReasoningLevel}


@dataclass(frozen=True)
class ChannelConfigForm:
    """One channel's settings as the form supplies them."""

    proactive_enabled: bool
    model_key: str | None
    reasoning_level: str | None
    daily_token_budget: int
    hourly_token_budget: int
    auto_respond: bool
    fallback_model_key: str | None
    response_filter: str | None
    drafter_model: str | None


def _optional_text(form, field: str) -> str | None:
    value = str(form.get(field) or "").strip()
    return value or None


def _budget(form, field: str) -> int:
    raw = str(form.get(field) or "0").strip() or "0"
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{field.replace('_', ' ')} must be a whole number")
    if value < 0 or value > MAX_INT32:
        raise ValueError(
            f"{field.replace('_', ' ')} must be between 0 and {MAX_INT32}"
        )
    return value


def _model_key(form, field: str, label: str) -> str | None:
    key = _optional_text(form, field)
    if key is not None and not is_valid_model_key(key):
        raise ValueError(f"Unknown {label} model {key!r}")
    return key


def parse_channel_config_form(form) -> ChannelConfigForm:
    """Validate the submitted form. Raises ``ValueError`` with a message the
    page shows the moderator verbatim."""
    bot_kind = str(form.get("bot_kind") or "legacy").strip()
    if bot_kind not in ("proactive", "legacy"):
        raise ValueError(f"Unknown bot type {bot_kind!r}")

    reasoning_level = _optional_text(form, "reasoning_level")
    if reasoning_level is not None and reasoning_level not in _VALID_REASONING:
        raise ValueError(f"Unknown reasoning level {reasoning_level!r}")

    return ChannelConfigForm(
        proactive_enabled=bot_kind == "proactive",
        model_key=_model_key(form, "model_key", "chat"),
        reasoning_level=reasoning_level,
        daily_token_budget=_budget(form, "daily_token_budget"),
        hourly_token_budget=_budget(form, "hourly_token_budget"),
        auto_respond=str(form.get("auto_respond") or "") == "on",
        fallback_model_key=_model_key(
            form, "fallback_model_key", "fallback"
        ),
        response_filter=_optional_text(form, "response_filter"),
        drafter_model=_model_key(form, "drafter_model", "drafter"),
    )


class ChannelConfigController(Controller):
    """The signed-link configuration page for one channel.

    Deliberately carries no ``auth_guard``: a guild moderator has no site
    login, and the signed link — minted only after Discord confirmed their
    permissions, expiring in 15 minutes, scoped to one channel — is the
    authorization. Every request re-verifies it, so an edited, forwarded or
    stale link gets nothing. The unguessable token in the path is also what
    makes the POST safe from cross-site submission.
    """

    path = CONFIG_LINK_PATH

    @staticmethod
    def _payload(token: str) -> ConfigLinkPayload | None:
        return verify_config_link(token)

    @get("/{token:str}")
    async def show(
        self, request: Request, db_session: AsyncSession, token: str
    ) -> TemplateResponse:
        payload = self._payload(token)
        if payload is None:
            return TemplateResponse(
                "admin/bot/channel_config_invalid.html",
                context={},
                status_code=403,
            )
        override = await get_channel_model_override(
            db_session, payload.guild_id, payload.channel_id
        )
        proactive = await get_proactive_channel_settings(
            db_session, payload.guild_id, payload.channel_id
        )
        return TemplateResponse(
            "admin/bot/channel_config.html",
            context={
                "token": token,
                "guild_id": payload.guild_id,
                "channel_id": payload.channel_id,
                "override": override,
                "proactive": proactive,
                "models": MODEL_CATALOG,
                "reasoning_levels": sorted(_VALID_REASONING),
                "flash_messages": get_flash_messages(request),
            },
        )

    @post("/{token:str}")
    async def save(
        self, request: Request, db_session: AsyncSession, token: str
    ) -> Redirect | TemplateResponse:
        payload = self._payload(token)
        if payload is None:
            return TemplateResponse(
                "admin/bot/channel_config_invalid.html",
                context={},
                status_code=403,
            )
        back = f"{CONFIG_LINK_PATH}/{token}"
        try:
            parsed = parse_channel_config_form(await request.form())
        except ValueError as error:
            flash_error(request, str(error))
            return Redirect(path=back)

        await upsert_channel_model_override(
            db_session,
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            model_key=parsed.model_key,
            reasoning_level=parsed.reasoning_level,
            daily_token_budget=parsed.daily_token_budget,
            hourly_token_budget=parsed.hourly_token_budget,
            auto_respond=parsed.auto_respond,
            fallback_model_key=parsed.fallback_model_key,
            response_filter=parsed.response_filter,
            drafter_model=parsed.drafter_model,
        )
        existing = await get_proactive_channel_settings(
            db_session, payload.guild_id, payload.channel_id
        )
        await upsert_proactive_channel_settings(
            db_session,
            guild_id=payload.guild_id,
            channel_id=payload.channel_id,
            enabled=parsed.proactive_enabled,
            # The agent owns its watch instructions; the form never touches
            # them.
            watch_addendum=existing.watch_addendum if existing else "",
        )
        await db_session.commit()
        logger.info(
            "channel config saved guild=%s channel=%s by discord user=%s "
            "(proactive=%s)",
            payload.guild_id,
            payload.channel_id,
            payload.discord_user_id,
            parsed.proactive_enabled,
        )
        flash_success(request, "Channel configuration saved.")
        return Redirect(path=back)
