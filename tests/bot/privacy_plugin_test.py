"""``/privacy`` answers with the notice's short version and its link (task #71)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import hikari

from smarter_dev.bot import client
from smarter_dev.bot.plugins import privacy_notice as privacy_plugin
from smarter_dev.shared import privacy_notice
from smarter_dev.shared.privacy_notice import discord_short_version

run_privacy = privacy_plugin.privacy.callback


async def test_privacy_replies_with_the_link_only_to_the_caller(monkeypatch):
    monkeypatch.setattr(
        privacy_notice.get_settings(), "site_base_url", "https://smarter.dev"
    )
    ctx = SimpleNamespace(respond=AsyncMock(), author=SimpleNamespace(id=1))

    await run_privacy(ctx)

    ctx.respond.assert_awaited_once()
    body = ctx.respond.await_args.args[0]
    assert "https://smarter.dev/privacy" in body
    for point in discord_short_version():
        assert point in body
    assert ctx.respond.await_args.kwargs["flags"] == hikari.MessageFlag.EPHEMERAL


def test_privacy_takes_no_options_and_is_not_an_opt_out():
    command = privacy_plugin.privacy
    assert command.name == "privacy"
    assert not command.options
    assert "opt" not in command.description.lower()


def test_load_plugins_loads_the_privacy_notice_plugin():
    bot = Mock()
    bot.d = {}
    client.load_plugins(bot)
    loaded = [call.args[0] for call in bot.load_extensions.call_args_list]
    assert "smarter_dev.bot.plugins.privacy_notice" in loaded
    # The purge consumer (#79) is a separate plugin; both load.
    assert "smarter_dev.bot.plugins.privacy" in loaded
    assert privacy_plugin.plugin.name != "privacy"
    assert callable(privacy_plugin.load) and callable(privacy_plugin.unload)
