"""``/privacy`` — a short summary of the privacy notice and a link to it.

Open to everyone. Under the summary is one button that opens the AI
assistant opt-out (:mod:`smarter_dev.bot.privacy.opt_out`, #92); opting
out is separate from having data deleted. The
wording comes from :mod:`smarter_dev.shared.privacy_notice`, so the command,
the site page and the channel post say the same thing.
"""

from __future__ import annotations

import hikari
import lightbulb

from smarter_dev.bot.privacy.opt_out import open_button
from smarter_dev.shared.privacy_notice import command_response
from smarter_dev.shared.privacy_notice import privacy_url

plugin = lightbulb.Plugin("privacy_notice")


@plugin.command
@lightbulb.command("privacy", "How Smarter Dev handles your data, with a link")
@lightbulb.implements(lightbulb.SlashCommand)
async def privacy(ctx: lightbulb.Context) -> None:
    await ctx.respond(
        command_response(privacy_url()),
        components=[open_button(ctx.author.id)],
        flags=hikari.MessageFlag.EPHEMERAL,
    )


def load(bot: lightbulb.BotApp) -> None:
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    bot.remove_plugin(plugin)
