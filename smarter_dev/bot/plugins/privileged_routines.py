"""Admin-only slash command for privileged routines (separate, immutable tier).

This is the *only* way privileged routines are created. It gates on Discord's
ADMINISTRATOR permission and talks to the dedicated ``/admin/routines`` API — a
completely separate path from the chatbot's handler tools, which cannot read,
create, or modify these.

Pure helpers (``is_admin``, ``build_action``, ``build_settings``) are factored
out so the gating and spec-building logic is unit-testable without a live
Discord context.
"""

from __future__ import annotations

import logging
from typing import Any

import hikari
import lightbulb

from smarter_dev.shared.config import get_settings

logger = logging.getLogger(__name__)

plugin = lightbulb.Plugin("privileged_routines")

_ACTION_CHOICES = ("timeout", "kick", "ban", "delete")
_TRIGGER_CHOICES = ("schedule", "timer")


def is_admin(permissions: hikari.Permissions) -> bool:
    """Whether a member's permissions include ADMINISTRATOR."""
    return bool(permissions & hikari.Permissions.ADMINISTRATOR)


def build_action(
    kind: str,
    target_user_id: str | None,
    duration_minutes: int | None,
    channel_id: str | None,
    message_id: str | None,
    reason: str | None,
) -> dict:
    """Assemble a structured action spec from slash-command options."""
    action: dict[str, Any] = {"kind": kind}
    if kind in ("timeout", "kick", "ban"):
        action["target_user_id"] = target_user_id
    if kind == "timeout" and duration_minutes:
        action["duration_seconds"] = int(duration_minutes) * 60
    if kind == "delete":
        action["channel_id"] = channel_id
        action["message_id"] = message_id
    if reason:
        action["reason"] = reason
    return action


def build_settings(trigger_type: str, when_minutes: int | None, daily_time: str | None) -> dict:
    """Assemble time-trigger settings from slash-command options."""
    if trigger_type == "timer":
        return {"delay_seconds": int(when_minutes or 0) * 60}
    if daily_time:
        return {"daily_time": daily_time}
    return {"interval_seconds": int(when_minutes or 0) * 60}


def _api_client():
    from smarter_dev.bot.services.api_client import APIClient

    settings = get_settings()
    return APIClient(base_url=settings.api_base_url, api_key=settings.bot_api_key)


async def _deny_if_not_admin(ctx: lightbulb.Context) -> bool:
    if not isinstance(ctx.member, hikari.InteractionMember):
        await ctx.respond("This command only works in a server.", flags=hikari.MessageFlag.EPHEMERAL)
        return True
    if not is_admin(lightbulb.utils.permissions_for(ctx.member)):
        await ctx.respond(
            "You need the Administrator permission to manage privileged routines.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return True
    return False


@plugin.command
@lightbulb.command("routine", "Manage privileged moderation routines (admin only)")
@lightbulb.implements(lightbulb.SlashCommandGroup)
async def routine_group(ctx: lightbulb.Context) -> None:
    pass


@routine_group.child
@lightbulb.option("daily_time", "UTC HH:MM for daily schedules", type=str, required=False)
@lightbulb.option("when_minutes", "Timer delay / schedule interval, minutes", type=int, required=False)
@lightbulb.option("reason", "Reason", type=str, required=False)
@lightbulb.option("message_id", "Message id (delete action)", type=str, required=False)
@lightbulb.option("duration_minutes", "Timeout duration, minutes", type=int, required=False)
@lightbulb.option("target", "Target user (timeout/kick/ban)", type=hikari.User, required=False)
@lightbulb.option("trigger", "When it fires", type=str, choices=_TRIGGER_CHOICES)
@lightbulb.option("action", "Moderation action", type=str, choices=_ACTION_CHOICES)
@lightbulb.command("create", "Create a privileged routine", pass_options=True)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def create_routine(
    ctx: lightbulb.Context,
    action: str,
    trigger: str,
    target: hikari.User | None = None,
    duration_minutes: int | None = None,
    message_id: str | None = None,
    reason: str | None = None,
    when_minutes: int | None = None,
    daily_time: str | None = None,
) -> None:
    if await _deny_if_not_admin(ctx):
        return
    action_spec = build_action(
        action,
        str(target.id) if target else None,
        duration_minutes,
        str(ctx.channel_id),
        message_id,
        reason,
    )
    settings = build_settings(trigger, when_minutes, daily_time)
    api = _api_client()
    resp = await api.post(
        "/admin/routines",
        json_data={
            "guild_id": str(ctx.guild_id),
            "channel_id": str(ctx.channel_id),
            "trigger_type": trigger,
            "settings": settings,
            "action": action_spec,
            "created_by_admin": str(ctx.author.id),
        },
    )
    if resp.status_code >= 400:
        await ctx.respond(f"Failed: {resp.text[:200]}", flags=hikari.MessageFlag.EPHEMERAL)
        return
    data = resp.json()
    await ctx.respond(
        f"Created privileged routine `{data['routine_id']}` ({action}).",
        flags=hikari.MessageFlag.EPHEMERAL,
    )


@routine_group.child
@lightbulb.command("list", "List privileged routines")
@lightbulb.implements(lightbulb.SlashSubCommand)
async def list_routines(ctx: lightbulb.Context) -> None:
    if await _deny_if_not_admin(ctx):
        return
    api = _api_client()
    resp = await api.get("/admin/routines", params={"guild_id": str(ctx.guild_id)})
    rows = resp.json() if resp.status_code < 400 else []
    if not rows:
        await ctx.respond("No privileged routines.", flags=hikari.MessageFlag.EPHEMERAL)
        return
    lines = "\n".join(
        f"- `{r['routine_id']}` [{r['trigger_type']}] {r['action'].get('kind')}" for r in rows
    )
    await ctx.respond(lines, flags=hikari.MessageFlag.EPHEMERAL)


@routine_group.child
@lightbulb.option("routine_id", "Routine id to delete", type=str)
@lightbulb.command("delete", "Delete a privileged routine", pass_options=True)
@lightbulb.implements(lightbulb.SlashSubCommand)
async def delete_routine(ctx: lightbulb.Context, routine_id: str) -> None:
    if await _deny_if_not_admin(ctx):
        return
    api = _api_client()
    resp = await api.delete(f"/admin/routines/{routine_id}")
    if resp.status_code >= 400:
        await ctx.respond(f"Failed: {resp.text[:200]}", flags=hikari.MessageFlag.EPHEMERAL)
        return
    await ctx.respond(f"Deleted routine `{routine_id}`.", flags=hikari.MessageFlag.EPHEMERAL)


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)
    logger.info("Privileged routines plugin loaded (admin tier)")


def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
