"""Challenge input delivery: ``gif64:`` inputs arrive as .gif attachments."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock
from unittest.mock import Mock

import hikari
import pytest

from smarter_dev.bot.plugins import events

GIF_A = b"GIF89a-first"
GIF_B = b"GIF89a-second"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _event(custom_id: str, input_data: str) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = {
        "input_data": input_data,
        "challenge": {"title": "Power On!"},
    }
    api_client = Mock()
    api_client.get = AsyncMock(return_value=response)
    service = Mock(_api_client=api_client)

    event = Mock()
    event.app.d._services = {"challenges": service}
    event.interaction = Mock(spec=hikari.ComponentInteraction)
    event.interaction.guild_id = 30
    event.interaction.user.id = 40
    event.interaction.custom_id = custom_id
    event.interaction.create_initial_response = AsyncMock()
    return event


async def _files(event: Mock) -> list[tuple[str, bytes]]:
    event.interaction.create_initial_response.assert_awaited_once()
    kwargs = event.interaction.create_initial_response.await_args.kwargs
    return [(f.filename, await f.read()) for f in kwargs["attachments"]]


async def test_confirm_sends_a_single_gif64_input_as_a_gif():
    event = _event("confirm_get_input:abc", "gif64:" + _b64(GIF_A))
    await events.handle_challenge_confirm_get_input_interaction(event)
    assert await _files(event) == [("power_on_.gif", GIF_A)]


async def test_existing_input_sends_a_gif64_list_as_numbered_gifs():
    event = _event("get_input:abc", "gif64:" + json.dumps([_b64(GIF_A), _b64(GIF_B)]))
    response = Mock(status_code=200)
    response.json.return_value = {"exists": True}
    first_get = event.app.d._services["challenges"]._api_client.get
    input_response = first_get.return_value
    first_get.side_effect = [response, input_response]

    await events.handle_challenge_get_input_interaction(event)
    assert await _files(event) == [
        ("power_on__1.gif", GIF_A),
        ("power_on__2.gif", GIF_B),
    ]


async def test_plain_input_is_still_a_txt_file():
    event = _event("confirm_get_input:abc", "line one\nline two")
    await events.handle_challenge_confirm_get_input_interaction(event)
    assert await _files(event) == [("power_on_.txt", b"line one\nline two")]


@pytest.mark.parametrize(
    "payload",
    [
        "gif64:not base64!",
        "gif64:[]",
        "gif64:[1]",
        "gif64:",  # decodes to no bytes at all
        "gif64:" + _b64(b"PNG not a gif"),
        "gif64:" + json.dumps([_b64(GIF_A), "AA=="]),  # one good item, one 1-byte item
    ],
)
async def test_broken_gif64_input_reports_a_failure(payload):
    event = _event("confirm_get_input:abc", payload)
    await events.handle_challenge_confirm_get_input_interaction(event)
    kwargs = event.interaction.create_initial_response.await_args.kwargs
    assert "attachments" not in kwargs
    assert "Failed to get challenge input" in kwargs["content"]
