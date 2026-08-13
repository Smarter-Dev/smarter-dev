"""Tests for the media-outage fallback used by every error response."""

from __future__ import annotations

from unittest.mock import AsyncMock

import hikari
import pytest

from smarter_dev.bot.services.media_client import MediaServiceUnavailableError
from smarter_dev.bot.services.media_client import RenderedMedia
from smarter_dev.bot.utils import error_responses
from smarter_dev.bot.utils.error_responses import respond_with_card
from smarter_dev.bot.utils.error_responses import respond_with_error_card
from smarter_dev.bot.utils.image_embeds import EmbedImageGenerator

PNG = b"\x89PNG\r\n\x1a\nfake"


@pytest.fixture
def media_client(monkeypatch) -> AsyncMock:
    client = AsyncMock()
    client.create_error_embed.return_value = RenderedMedia(
        data=PNG, filename="embed.png", mime_type="image/png"
    )
    monkeypatch.setattr(
        error_responses, "get_generator", lambda: EmbedImageGenerator(client)
    )
    return client


async def test_error_card_is_sent_as_an_attachment(media_client):
    respond = AsyncMock()

    await respond_with_error_card(
        respond, "nope", flags=hikari.MessageFlag.EPHEMERAL
    )

    respond.assert_awaited_once()
    kwargs = respond.await_args.kwargs
    assert kwargs["flags"] == hikari.MessageFlag.EPHEMERAL
    assert bytes(await kwargs["attachment"].read()) == PNG
    media_client.create_error_embed.assert_awaited_once_with(message="nope")


async def test_positional_arguments_are_forwarded(media_client):
    respond = AsyncMock()

    await respond_with_error_card(
        respond, "nope", hikari.ResponseType.MESSAGE_CREATE, components=[]
    )

    assert respond.await_args.args == (hikari.ResponseType.MESSAGE_CREATE,)
    assert respond.await_args.kwargs["components"] == []


async def test_media_outage_falls_back_to_plain_text(media_client):
    media_client.create_error_embed.side_effect = MediaServiceUnavailableError("down")
    respond = AsyncMock()

    await respond_with_error_card(
        respond, "Transfer failed.", flags=hikari.MessageFlag.EPHEMERAL
    )

    respond.assert_awaited_once_with(
        content="Transfer failed.", flags=hikari.MessageFlag.EPHEMERAL
    )


async def test_the_card_is_rendered_before_discord_is_called(media_client):
    """A failed render must not leave a half-sent Discord request behind."""
    media_client.create_error_embed.side_effect = MediaServiceUnavailableError("down")
    respond = AsyncMock()

    await respond_with_error_card(respond, "boom")

    assert "attachment" not in respond.await_args.kwargs


async def test_respond_with_card_falls_back_for_any_card(media_client):
    generator = EmbedImageGenerator(media_client)
    media_client.create_cooldown_embed.side_effect = MediaServiceUnavailableError("x")
    respond = AsyncMock()

    await respond_with_card(
        respond,
        generator.create_cooldown_embed("Slow down", None),
        "Slow down",
        flags=hikari.MessageFlag.EPHEMERAL,
    )

    respond.assert_awaited_once_with(
        content="Slow down", flags=hikari.MessageFlag.EPHEMERAL
    )
