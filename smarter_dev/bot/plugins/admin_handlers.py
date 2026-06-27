"""Admin slash command — talk to the admin author and create an admin handler.

Replaces the old structured `/routine`. `/adminhandler create request:<text>` is
text-only: the admin describes the behavior, the admin author writes the script
and decides the trigger + channel scope, the admin judge reviews, and it's
installed via the admin-handlers API. Admin-gated (ADMINISTRATOR).
"""

from __future__ import annotations

import logging
from typing import Any

import hikari
import lightbulb

from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("admin_handlers")


def is_admin(permissions: hikari.Permissions) -> bool:
    return bool(permissions & hikari.Permissions.ADMINISTRATOR)


async def _deny_if_not_admin(ctx: lightbulb.Context) -> bool:
    if not isinstance(ctx.member, hikari.InteractionMember):
        await ctx.respond("This command only works in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return True
    if not is_admin(lightbulb.utils.permissions_for(ctx.member)):
        await ctx.respond(
            "You need the Administrator permission to manage admin handlers.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return True
    return False


def _api_client():
    from smarter_dev.bot.services.api_client import APIClient

    settings = get_settings()
    return APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)


async def _list_guild_channels(ctx: lightbulb.Context) -> list[dict]:
    """Text channels in the guild (name + id) for the admin author to resolve scope."""
    try:
        channels = await ctx.bot.rest.fetch_guild_channels(ctx.guild_id)
        out: list[dict] = []
        for ch in channels:
            if isinstance(ch, hikari.GuildTextChannel):
                out.append({"id": str(ch.id), "name": ch.name})
        return out
    except Exception:  # noqa: BLE001 — channels are advisory for the author
        logger.debug("could not fetch guild channels", exc_info=True)
        return []


@plugin.command
@lightbulb.command("adminhandler", "Admin handlers (admin only)")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def adminhandler_group(ctx: lightbulb.Context) -> None:
    pass


@adminhandler_group.child
@lightbulb.option(
    "request",
    "Describe what the admin handler should do",
    type=str,
)
@lightbulb.command(
    "create", "Describe an admin handler; the author builds it", pass_options=True
)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def create_admin_handler(ctx: lightbulb.Context, request: str) -> None:
    if await _deny_if_not_admin(ctx):
        return
    await ctx.respond(
        hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL
    )

    from smarter_dev.bot.agents.handler_authoring import run_admin_creation_pipeline

    result = await run_admin_creation_pipeline(
        request=request,
        channel_lister=lambda: _list_guild_channels(ctx),
    )
    if not result.ok:
        await ctx.edit_last_response(f"Couldn't create it — {result.error}")
        return

    api = _api_client()
    resp = await api.post(
        "/admin/handlers",
        json_data={
            "guild_id": str(ctx.guild_id),
            "trigger_type": result.trigger_type,
            "settings": result.settings or {},
            "channel_ids": result.channel_ids or [],
            "description": request,
            "script": result.script,
            "created_by_admin": str(ctx.author.id),
        },
    )
    if resp.status_code >= 400:
        await ctx.edit_last_response(f"Failed to install: {resp.text[:300]}")
        return
    data = resp.json()
    scope = "all channels" if not data["channel_ids"] else f"{len(data['channel_ids'])} channel(s)"
    await ctx.edit_last_response(
        f"Created admin handler `{data['handler_id']}` "
        f"({data['trigger_type']}, {scope})."
    )


@adminhandler_group.child
@lightbulb.command("list", "List admin handlers in this server")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def list_admin_handlers(ctx: lightbulb.Context) -> None:
    if await _deny_if_not_admin(ctx):
        return
    api = _api_client()
    resp = await api.get("/admin/handlers", params={"guild_id": str(ctx.guild_id)})
    rows = resp.json() if resp.status_code < 400 else []
    if not rows:
        await ctx.respond("No admin handlers.", flags=hikari.MessageFlag.EPHEMERAL)
        return
    lines = "\n".join(
        f"- `{r['handler_id']}` [{r['trigger_type']}] {r['description'][:60]}"
        for r in rows
    )
    await ctx.respond(lines, flags=hikari.MessageFlag.EPHEMERAL)


@adminhandler_group.child
@lightbulb.option("handler_id", "Admin handler id to delete", type=str)
@lightbulb.command("delete", "Delete an admin handler", pass_options=True)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def delete_admin_handler(ctx: lightbulb.Context, handler_id: str) -> None:
    if await _deny_if_not_admin(ctx):
        return
    api = _api_client()
    resp = await api.delete(f"/admin/handlers/{handler_id}")
    if resp.status_code >= 400:
        await ctx.respond(f"Failed: {resp.text[:200]}", flags=hikari.MessageFlag.EPHEMERAL)
        return
    await ctx.respond(f"Deleted admin handler `{handler_id}`.", flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)
    logger.info("Admin handlers plugin loaded (admin author)")


def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
