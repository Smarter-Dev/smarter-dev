"""Admin slash command — talk to the admin author to create or edit admin handlers.

Replaces the old structured `/routine`. `/adminhandler create request:<text>` is
text-only: the admin describes the behavior, the admin author sees the guild's
existing named handlers and decides whether to edit one or create a new one
(picking its name, trigger, and channel scope), the admin judge reviews, and
it's installed via the admin-handlers API. Admin-gated (ADMINISTRATOR).
"""

from __future__ import annotations

import logging
from typing import Any

import hikari
import lightbulb

from smarter_dev.bot.plugins.admin_gate import deny_if_not_admin
from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("admin_handlers")

ADMIN_DENIAL_MESSAGE = (
    "You need the Administrator permission to manage admin handlers."
)


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


async def _guild_admin_handlers_with_scripts(api: Any, guild_id: str) -> list[dict]:
    """The guild's admin handlers, scripts included, for the author's
    edit-vs-create decision. Best-effort: a failure means the author sees an
    empty list (it will create rather than edit)."""
    try:
        resp = await api.get(
            "/admin/handlers",
            params={"guild_id": guild_id, "include_scripts": "true"},
        )
        if resp.status_code < 400:
            return list(resp.json())
    except Exception:  # noqa: BLE001
        logger.debug("could not load existing admin handlers", exc_info=True)
    return []


async def install_admin_result(api: Any, guild_id: str, admin_id: str, result: Any) -> str:
    """Persist an approved admin plan (create or edit); return the user-facing line."""
    if result.action == "edit":
        resp = await api.put(
            f"/admin/handlers/{result.target_handler_id}",
            json_data={
                "description": result.description,
                "script": result.script,
                "settings": result.settings or {},
                "channel_ids": result.channel_ids or [],
            },
        )
        if resp.status_code >= 400:
            return f"Failed to update: {resp.text[:300]}"
        data = resp.json()
        scope = (
            "all channels"
            if not data["channel_ids"]
            else f"{len(data['channel_ids'])} channel(s)"
        )
        return (
            f"Updated admin handler **{data['name']}** "
            f"({data['trigger_type']}, {scope}): {data['description']}"
        )

    resp = await api.post(
        "/admin/handlers",
        json_data={
            "guild_id": guild_id,
            "name": result.name,
            "trigger_type": result.trigger_type,
            "settings": result.settings or {},
            "channel_ids": result.channel_ids or [],
            "description": result.description,
            "script": result.script,
            "created_by_admin": admin_id,
        },
    )
    if resp.status_code >= 400:
        return f"Failed to install: {resp.text[:300]}"
    data = resp.json()
    scope = (
        "all channels"
        if not data["channel_ids"]
        else f"{len(data['channel_ids'])} channel(s)"
    )
    return (
        f"Created admin handler **{data['name']}** "
        f"({data['trigger_type']}, {scope})."
    )


@adminhandler_group.child
@lightbulb.option(
    "request",
    "Describe what the admin handler should do",
    type=str,
)
@lightbulb.command(
    "create", "Describe an admin handler; the author builds or edits one", pass_options=True
)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def create_admin_handler(ctx: lightbulb.Context, request: str) -> None:
    if await deny_if_not_admin(ctx, ADMIN_DENIAL_MESSAGE):
        return
    await ctx.respond(
        hikari.ResponseType.DEFERRED_MESSAGE_CREATE, flags=hikari.MessageFlag.EPHEMERAL
    )

    from smarter_dev.bot.agents.handler_authoring import run_admin_creation_pipeline

    api = _api_client()
    existing_handlers = await _guild_admin_handlers_with_scripts(api, str(ctx.guild_id))
    result = await run_admin_creation_pipeline(
        request=request,
        existing_handlers=existing_handlers,
        channel_lister=lambda: _list_guild_channels(ctx),
    )
    if not result.ok:
        await ctx.edit_last_response(f"Couldn't do it — {result.error}")
        return

    line = await install_admin_result(api, str(ctx.guild_id), str(ctx.author.id), result)
    await ctx.edit_last_response(line)


@adminhandler_group.child
@lightbulb.command("list", "List admin handlers in this server")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def list_admin_handlers(ctx: lightbulb.Context) -> None:
    if await deny_if_not_admin(ctx, ADMIN_DENIAL_MESSAGE):
        return
    api = _api_client()
    resp = await api.get("/admin/handlers", params={"guild_id": str(ctx.guild_id)})
    rows = resp.json() if resp.status_code < 400 else []
    if not rows:
        await ctx.respond("No admin handlers.", flags=hikari.MessageFlag.EPHEMERAL)
        return
    lines = "\n".join(
        f"- **{r['name']}** (`{r['handler_id']}`) [{r['trigger_type']}] {r['description'][:60]}"
        for r in rows
    )
    await ctx.respond(lines, flags=hikari.MessageFlag.EPHEMERAL)


@adminhandler_group.child
@lightbulb.option("handler_id", "Admin handler id to delete", type=str)
@lightbulb.command("delete", "Delete an admin handler", pass_options=True)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def delete_admin_handler(ctx: lightbulb.Context, handler_id: str) -> None:
    if await deny_if_not_admin(ctx, ADMIN_DENIAL_MESSAGE):
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
