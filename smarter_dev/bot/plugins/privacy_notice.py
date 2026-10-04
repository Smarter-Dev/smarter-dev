"""``/privacy`` — the short version of the privacy notice and a link to it.

Read-only and open to everyone. It is not an opt-out: there is no opt-out
yet, and when there is it will live on the dashboard, not in a command. The
wording comes from :mod:`smarter_dev.shared.privacy_notice`, so the command,
the site page and the channel post say the same thing.
"""

from __future__ import annotations

import hikari
import lightbulb

from smarter_dev.shared.privacy_notice import command_response
from smarter_dev.shared.privacy_notice import privacy_url

plugin = lightbulb.Plugin("privacy_notice")


@plugin.command
@lightbulb.command("privacy", "How Smarter Dev handles your data, with a link")
@lightbulb.implements(lightbulb.SlashCommand)
async def privacy(ctx: lightbulb.Context) -> None:
    await ctx.respond(
        command_response(privacy_url()),
        flags=hikari.MessageFlag.EPHEMERAL,
    )


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
