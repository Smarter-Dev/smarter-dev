"""Tests for the internal media-service HTTP client."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

from smarter_dev.bot.services.media_client import MediaClient
from smarter_dev.bot.services.media_client import MediaRenderError
from smarter_dev.bot.services.media_client import MediaServiceUnavailableError
from smarter_dev.shared.config import Settings

BASE_URL = "http://media.test:8080"
API_KEY = "mk_test_key"
PNG = b"\x89PNG\r\n\x1a\nfake"


def _png_response(
    filename: str = "embed.png", status_code: int = 200
) -> httpx.Response:
    return httpx.Response(
        status_code,
        content=PNG,
        headers={
            "Content-Type": "image/png",
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


def _error_response(status_code: int, code: str, message: str) -> httpx.Response:
    return httpx.Response(
        status_code,
        json={"error": {"code": code, "message": message}},
    )


def _client(
    handler, *, base_url: str = BASE_URL, api_key: str = API_KEY
) -> MediaClient:
    return MediaClient(
        base_url,
        api_key,
        transport=httpx.MockTransport(handler),
    )


def _recorder(response_factory):
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        request.read()
        requests.append(request)
        return response_factory(request)

    return requests, handle


async def test_card_request_sends_bearer_token_and_json_body():
    requests, handle = _recorder(lambda _request: _png_response("error.png"))
    client = _client(handle)

    rendered = await client.create_error_embed("boom")

    assert rendered.data == PNG
    assert rendered.filename == "error.png"
    assert rendered.mime_type == "image/png"
    request = requests[0]
    assert str(request.url) == f"{BASE_URL}/v1/cards/error"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert json.loads(request.content) == {"message": "boom"}


async def test_missing_content_disposition_falls_back_to_route_filename():
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG, headers={"Content-Type": "image/png"})

    client = _client(handle)

    rendered = await client.create_balance_embed("zech", 10)

    assert rendered.filename == "balance.png"


async def test_render_failure_raises_media_render_error_with_code():
    client = _client(
        lambda _request: _error_response(422, "render_failed", "TeX parse error")
    )

    with pytest.raises(MediaRenderError) as excinfo:
        await client.render_latex("\\foo")

    assert excinfo.value.code == "render_failed"
    assert excinfo.value.status_code == 422
    assert "TeX parse error" in str(excinfo.value)


async def test_invalid_request_raises_media_render_error():
    client = _client(
        lambda _request: _error_response(400, "invalid_request", "source is required")
    )

    with pytest.raises(MediaRenderError) as excinfo:
        await client.render_latex("x")

    assert excinfo.value.code == "invalid_request"
    assert excinfo.value.status_code == 400


async def test_server_error_raises_unavailable_error():
    client = _client(
        lambda _request: _error_response(500, "internal_error", "canvas exploded")
    )

    with pytest.raises(MediaServiceUnavailableError):
        await client.render_latex("x")


async def test_non_json_client_error_raises_unavailable_error():
    client = _client(lambda _request: httpx.Response(404, text="<html>nope</html>"))

    with pytest.raises(MediaServiceUnavailableError):
        await client.render_latex("x")


async def test_connect_error_names_the_configured_url():
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handle)

    with pytest.raises(MediaServiceUnavailableError) as excinfo:
        await client.render_latex("x")

    assert BASE_URL in str(excinfo.value)


async def test_connect_error_is_retried_exactly_once():
    attempts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return _png_response("latex.png")

    client = MediaClient(
        BASE_URL,
        API_KEY,
        transport=httpx.MockTransport(handle),
        retry_delay_seconds=0.0,
    )

    rendered = await client.render_latex("x")

    assert rendered.filename == "latex.png"
    assert len(attempts) == 2


async def test_timeout_is_not_retried():
    attempts: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("too slow", request=request)

    client = MediaClient(
        BASE_URL,
        API_KEY,
        transport=httpx.MockTransport(handle),
        retry_delay_seconds=0.0,
    )

    with pytest.raises(MediaServiceUnavailableError):
        await client.render_latex("x")

    assert len(attempts) == 1


async def test_render_latex_posts_source_and_reads_png():
    requests, handle = _recorder(lambda _request: _png_response("latex.png"))
    client = _client(handle)

    rendered = await client.render_latex("E = mc^2")

    assert json.loads(requests[0].content) == {"source": "E = mc^2"}
    assert str(requests[0].url) == f"{BASE_URL}/v1/latex"
    assert rendered.mime_type == "image/png"
    assert rendered.filename == "latex.png"


async def test_transcode_posts_raw_wav_with_bitrate_query():
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"OggS-audio",
            headers={
                "Content-Type": "audio/ogg",
                "Content-Disposition": 'inline; filename="voice-message.ogg"',
            },
        )

    requests, handle = _recorder(respond)
    client = _client(handle)

    ogg = await client.transcode_wav_to_opus_ogg(b"RIFFwave", bitrate="64k")

    assert ogg == b"OggS-audio"
    request = requests[0]
    assert request.url.path == "/v1/audio/opus-ogg"
    assert request.url.params["bitrate"] == "64k"
    assert request.headers["Content-Type"] == "audio/wav"
    assert request.content == b"RIFFwave"


async def test_health_returns_parsed_json():
    client = _client(lambda _request: httpx.Response(200, json={"status": "ok"}))

    assert await client.health() == {"status": "ok"}


async def test_health_failure_raises_unavailable_error():
    client = _client(
        lambda _request: _error_response(503, "not_ready", "MathJax is initialising")
    )

    with pytest.raises(MediaServiceUnavailableError):
        await client.health()


def test_from_settings_requires_a_url():
    settings = Settings(media_service_url="", media_api_key=API_KEY)

    with pytest.raises(ValueError):
        MediaClient.from_settings(settings)


def test_from_settings_requires_an_api_key():
    settings = Settings(media_service_url=BASE_URL, media_api_key="")

    with pytest.raises(ValueError):
        MediaClient.from_settings(settings)


def test_from_settings_strips_a_trailing_slash():
    settings = Settings(
        media_service_url=f"{BASE_URL}/",
        media_api_key=API_KEY,
        media_request_timeout_seconds=7.5,
    )

    client = MediaClient.from_settings(settings)

    assert client.base_url == BASE_URL
    assert client.timeout_seconds == 7.5


@dataclass
class _Entry:
    rank: int
    user_id: str
    balance: int
    streak_count: int


@dataclass
class _Transaction:
    created_at: str | None
    giver_id: str
    giver_username: str
    receiver_id: str
    receiver_username: str
    amount: int
    reason: str | None


@dataclass
class _Config:
    daily_amount: int
    starting_balance: int
    max_transfer: int
    transfer_cooldown_hours: int
    streak_bonuses: dict | None


@dataclass
class _Squad:
    name: str
    description: str | None
    member_count: int
    max_members: int | None
    switch_cost: int
    current_join_cost: int | None
    has_join_sale: bool
    role_id: str | None
    is_default: bool
    is_active: bool


@dataclass
class _Member:
    user_id: str
    username: str | None
    joined_at: str | None


@dataclass
class _MemberInfo:
    member_since: str | None


ENTRY = _Entry(rank=1, user_id="1", balance=100, streak_count=3)
TRANSACTION = _Transaction(
    created_at="2026-08-06T12:00:00",
    giver_id="1",
    giver_username="giver",
    receiver_id="2",
    receiver_username="receiver",
    amount=5,
    reason="thanks",
)
CONFIG = _Config(
    daily_amount=10,
    starting_balance=100,
    max_transfer=1000,
    transfer_cooldown_hours=0,
    streak_bonuses={"7": 2},
)
SQUAD = _Squad(
    name="Alpha",
    description="First squad",
    member_count=3,
    max_members=10,
    switch_cost=50,
    current_join_cost=25,
    has_join_sale=True,
    role_id="9",
    is_default=False,
    is_active=True,
)
MEMBER = _Member(user_id="1", username="zech", joined_at="2026-07-01T00:00:00")
MEMBER_INFO = _MemberInfo(member_since="2026-07-01T00:00:00")

SQUAD_BODY = {
    "name": "Alpha",
    "description": "First squad",
    "member_count": 3,
    "max_members": 10,
    "switch_cost": 50,
    "current_join_cost": 25,
    "has_join_sale": True,
    "role_id": "9",
    "is_default": False,
    "is_active": True,
}
MEMBER_BODY = {
    "user_id": "1",
    "username": "zech",
    "joined_at": "2026-07-01T00:00:00",
}

# Mirror of design §6.4: method -> (path, request body). This table is the
# Python side of the wire contract; the media service holds the other half.
CARD_CONTRACT = [
    (
        "create_simple_embed",
        {"title": "T", "description": "D", "embed_type": "info"},
        "/v1/cards/simple",
        {"title": "T", "description": "D", "embed_type": "info"},
    ),
    (
        "create_error_embed",
        {"message": "nope"},
        "/v1/cards/error",
        {"message": "nope"},
    ),
    (
        "create_success_embed",
        {"title": "T", "description": "D"},
        "/v1/cards/success",
        {"title": "T", "description": "D"},
    ),
    (
        "create_info_embed",
        {"title": "T", "description": "D"},
        "/v1/cards/info",
        {"title": "T", "description": "D"},
    ),
    (
        "create_cooldown_embed",
        {"message": "wait", "cooldown_end_timestamp": 1780000000},
        "/v1/cards/cooldown",
        {"message": "wait", "cooldown_end_timestamp": 1780000000},
    ),
    (
        "create_leaderboard_embed",
        {
            "entries": [ENTRY],
            "guild_name": "Guild",
            "user_display_names": {"1": "zech"},
        },
        "/v1/cards/leaderboard",
        {
            "entries": [
                {"rank": 1, "user_id": "1", "balance": 100, "streak_count": 3}
            ],
            "guild_name": "Guild",
            "user_display_names": {"1": "zech"},
        },
    ),
    (
        "create_history_embed",
        {"transactions": [TRANSACTION], "user_id": "1"},
        "/v1/cards/history",
        {
            "transactions": [
                {
                    "created_at": "2026-08-06T12:00:00",
                    "giver_id": "1",
                    "giver_username": "giver",
                    "receiver_id": "2",
                    "receiver_username": "receiver",
                    "amount": 5,
                    "reason": "thanks",
                }
            ],
            "user_id": "1",
        },
    ),
    (
        "create_config_embed",
        {"config": CONFIG, "guild_name": "Guild"},
        "/v1/cards/config",
        {
            "config": {
                "daily_amount": 10,
                "starting_balance": 100,
                "max_transfer": 1000,
                "transfer_cooldown_hours": 0,
                "streak_bonuses": {"7": 2},
            },
            "guild_name": "Guild",
        },
    ),
    (
        "create_squad_list_embed",
        {
            "squads": [SQUAD],
            "guild_name": "Guild",
            "current_squad_id": "abc",
            "guild_roles": {"9": 16711680},
            "has_active_campaign": True,
        },
        "/v1/cards/squad-list",
        {
            "squads": [SQUAD_BODY],
            "guild_name": "Guild",
            "current_squad_id": "abc",
            "guild_roles": {"9": 16711680},
            "has_active_campaign": True,
        },
    ),
    (
        "create_squad_info_embed",
        {"squad": SQUAD, "members": [MEMBER], "user_member_info": MEMBER_INFO},
        "/v1/cards/squad-info",
        {
            "squad": SQUAD_BODY,
            "members": [MEMBER_BODY],
            "user_member_info": {"member_since": "2026-07-01T00:00:00"},
        },
    ),
    (
        "create_squad_members_embed",
        {"squad": SQUAD, "members": [MEMBER]},
        "/v1/cards/squad-members",
        {"squad": SQUAD_BODY, "members": [MEMBER_BODY]},
    ),
    (
        "create_squad_join_selector_embed",
        {
            "user_balance": 42,
            "current_squad_name": "Alpha",
            "available_squads_count": 3,
        },
        "/v1/cards/squad-join-selector",
        {
            "user_balance": 42,
            "current_squad_name": "Alpha",
            "available_squads_count": 3,
        },
    ),
    (
        "create_balance_embed",
        {
            "username": "zech",
            "balance": 100,
            "streak_count": 2,
            "last_daily": "2026-08-05",
            "total_received": 10,
            "total_sent": 5,
        },
        "/v1/cards/balance",
        {
            "username": "zech",
            "balance": 100,
            "streak_count": 2,
            "last_daily": "2026-08-05",
            "total_received": 10,
            "total_sent": 5,
        },
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
        "/v1/cards/transfer-success",
        {
            "giver_name": "a",
            "receiver_name": "b",
            "amount": 5,
            "reason": "why",
            "new_balance": 95,
        },
    ),
]


@pytest.mark.parametrize(
    "method_name,kwargs,expected_path,expected_body",
    CARD_CONTRACT,
    ids=[row[0] for row in CARD_CONTRACT],
)
async def test_card_methods_match_the_wire_contract(
    method_name, kwargs, expected_path, expected_body
):
    requests, handle = _recorder(lambda _request: _png_response())
    client = _client(handle)

    await getattr(client, method_name)(**kwargs)

    request = requests[0]
    assert request.url.path == expected_path
    assert json.loads(request.content) == expected_body


def test_every_card_method_is_covered_by_the_contract_table():
    covered = {row[0] for row in CARD_CONTRACT}
    declared = {
        name
        for name in dir(MediaClient)
        if name.startswith("create_") and name.endswith("_embed")
    }
    assert declared == covered


async def test_squad_payload_omits_attributes_the_object_does_not_have():
    class MinimalSquad:
        name = "Alpha"
        description = None
        member_count = 1
        max_members = None
        switch_cost = 0
        is_active = True

    requests, handle = _recorder(lambda _request: _png_response())
    client = _client(handle)

    await client.create_squad_members_embed(MinimalSquad(), [])

    body = json.loads(requests[0].content)
    assert "current_join_cost" not in body["squad"]
    assert "has_join_sale" not in body["squad"]
    assert "is_default" not in body["squad"]
    assert "role_id" not in body["squad"]


async def test_datetime_attributes_are_serialised_as_iso_strings():
    from datetime import UTC
    from datetime import datetime

    class DatedMember:
        user_id = "1"
        username = "zech"
        joined_at = datetime(2026, 7, 1, tzinfo=UTC)

    requests, handle = _recorder(lambda _request: _png_response())
    client = _client(handle)

    await client.create_squad_members_embed(SQUAD, [DatedMember()])

    body = json.loads(requests[0].content)
    assert body["members"][0]["joined_at"] == "2026-07-01T00:00:00+00:00"
