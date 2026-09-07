"""/tldr must answer the member even when Discord refuses the history fetch.

gather_message_context raises RuntimeError on a Discord-side failure. The
command owns the deferred interaction, so it must turn that failure into a
reply; any other exception is a programming error and propagates.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from smarter_dev.bot.plugins import llm


def _context() -> MagicMock:
    ctx = MagicMock()
    ctx.respond = AsyncMock()
    ctx.edit_last_response = AsyncMock()
    ctx.options = SimpleNamespace(count=None)
    ctx.channel_id = 1234
    ctx.guild_id = 777
    ctx.user = SimpleNamespace(id=5001, display_name="Member", username="member")
    return ctx


class TestTldrCommandContextFetchFailure:
    async def test_discord_failure_is_reported_to_the_member(self):
        ctx = _context()

        with patch.object(
            llm,
            "gather_message_context",
            new=AsyncMock(side_effect=RuntimeError("Failed to gather context for channel 1234")),
        ), patch.object(llm, "generate_tldr_summary", new=AsyncMock()) as summarise:
            await llm.tldr_command.callback(ctx)

        ctx.edit_last_response.assert_awaited_once()
        reply = ctx.edit_last_response.await_args.args[0]
        assert "Couldn't Read This Channel" in reply
        assert "No Messages to Summarize" not in reply
        summarise.assert_not_awaited()

    async def test_programming_error_propagates(self):
        ctx = _context()

        with patch.object(
            llm, "gather_message_context", new=AsyncMock(side_effect=TypeError("broken"))
        ), patch.object(llm, "generate_tldr_summary", new=AsyncMock()) as summarise, pytest.raises(
            TypeError, match="broken"
        ):
            await llm.tldr_command.callback(ctx)

        ctx.edit_last_response.assert_not_awaited()
        summarise.assert_not_awaited()

    async def test_empty_channel_still_gets_the_no_messages_reply(self):
        ctx = _context()

        with patch.object(
            llm, "gather_message_context", new=AsyncMock(return_value=[])
        ), patch.object(llm, "generate_tldr_summary", new=AsyncMock()) as summarise:
            await llm.tldr_command.callback(ctx)

        ctx.edit_last_response.assert_awaited_once()
        assert "No Messages to Summarize" in ctx.edit_last_response.await_args.args[0]
        summarise.assert_not_awaited()
