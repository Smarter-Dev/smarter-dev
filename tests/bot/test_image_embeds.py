"""Tests for the media-service-backed Discord card generator."""

from __future__ import annotations

from unittest.mock import AsyncMock

import hikari
import pytest

from smarter_dev.bot.services.media_client import MediaServiceUnavailableError
from smarter_dev.bot.services.media_client import RenderedMedia
from smarter_dev.bot.utils import image_embeds
from smarter_dev.bot.utils.image_embeds import EmbedImageGenerator

PNG = b"\x89PNG\r\n\x1a\nfake"

# (generator method, every parameter it accepts, filename the service returns).
# The generator must forward each parameter to the identically-named media
# client method as a keyword, unchanged.
DELEGATIONS = [
    (
        "create_simple_embed",
        {"title": "T", "description": "D", "embed_type": "info"},
        "embed.png",
    ),
    ("create_error_embed", {"message": "nope"}, "embed.png"),
    ("create_success_embed", {"title": "T", "description": "D"}, "embed.png"),
    ("create_info_embed", {"title": "T", "description": "D"}, "embed.png"),
    (
        "create_cooldown_embed",
        {"message": "wait", "cooldown_end_timestamp": 1780000000},
        "embed.png",
    ),
    (
        "create_leaderboard_embed",
        {"entries": [], "guild_name": "G", "user_display_names": {}},
        "embed.png",
    ),
    ("create_history_embed", {"transactions": [], "user_id": "1"}, "embed.png"),
    ("create_config_embed", {"config": object(), "guild_name": "G"}, "embed.png"),
    (
        "create_squad_list_embed",
        {
            "squads": [],
            "guild_name": "G",
            "current_squad_id": "abc",
            "guild_roles": {"9": 1},
            "has_active_campaign": True,
        },
        "embed.png",
    ),
    (
        "create_squad_info_embed",
        {"squad": object(), "members": [], "user_member_info": None},
        "embed.png",
    ),
    ("create_squad_members_embed", {"squad": object(), "members": []}, "embed.png"),
    (
        "create_squad_join_selector_embed",
        {
            "user_balance": 5,
            "current_squad_name": "Alpha",
            "available_squads_count": 3,
        },
        "embed.png",
    ),
    (
        "create_balance_embed",
        {
            "username": "zech",
            "balance": 10,
            "streak_count": 2,
            "last_daily": "2026-08-05",
            "total_received": 1,
            "total_sent": 2,
        },
        "balance.png",
    ),
    (
        "create_transfer_success_embed",
        {
            "giver_name": "a",
            "receiver_name": "b",
            "amount": 5,
            "reason": "why",
            "new_balance": 95,
        },
        "transfer_success.png",
    ),
]


def _media_client(filename: str = "embed.png") -> AsyncMock:
    client = AsyncMock()
    for method_name, _, _ in DELEGATIONS:
        getattr(client, method_name).return_value = RenderedMedia(
            data=PNG, filename=filename, mime_type="image/png"
        )
    return client


@pytest.mark.parametrize(
    "method_name,kwargs,filename",
    DELEGATIONS,
    ids=[row[0] for row in DELEGATIONS],
)
async def test_each_method_delegates_to_the_media_client(
    method_name, kwargs, filename
):
    client = _media_client(filename)
    generator = EmbedImageGenerator(client)

    attachment = getattr(generator, method_name)(**kwargs)
    data = await attachment.read()

    assert isinstance(attachment, hikari.files.Bytes)
    assert attachment.filename == filename
    assert bytes(data) == PNG
    getattr(client, method_name).assert_awaited_once_with(**kwargs)


def test_every_generator_card_method_is_covered():
    covered = {row[0] for row in DELEGATIONS}
    declared = {
        name
        for name in dir(EmbedImageGenerator)
        if name.startswith("create_") and name.endswith("_embed")
    }
    assert declared == covered


async def test_the_service_is_only_called_when_the_attachment_is_read():
    client = _media_client()
    generator = EmbedImageGenerator(client)

    generator.create_error_embed("unused")

    client.create_error_embed.assert_not_awaited()


async def test_repeated_reads_render_once():
    client = _media_client()
    generator = EmbedImageGenerator(client)

    attachment = generator.create_error_embed("boom")
    first = await attachment.read()
    second = await attachment.read()

    assert bytes(first) == bytes(second) == PNG
    client.create_error_embed.assert_awaited_once()


async def test_media_failures_propagate_to_the_caller():
    client = AsyncMock()
    client.create_error_embed.side_effect = MediaServiceUnavailableError("down")
    generator = EmbedImageGenerator(client)

    attachment = generator.create_error_embed("boom")

    with pytest.raises(MediaServiceUnavailableError):
        await attachment.read()


def test_get_generator_returns_a_singleton(monkeypatch):
    monkeypatch.setattr(image_embeds, "_generator", None)
    monkeypatch.setattr(
        image_embeds.MediaClient,
        "from_settings",
        classmethod(lambda cls, settings: AsyncMock()),
    )

    first = image_embeds.get_generator()
    second = image_embeds.get_generator()

    assert first is second


async def test_close_generator_disposes_the_media_client(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(image_embeds, "_generator", EmbedImageGenerator(client))

    await image_embeds.close_generator()

    client.aclose.assert_awaited_once()
    assert image_embeds._generator is None
