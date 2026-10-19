"""``POST /api/bot-usage``: the bot's side-agent spend, into the usage ledger.

The real ``BotUsageController`` behind a Litestar test client, writing to a
real sqlite session. Auth is bypassed the way the other native-controller
tests do it (the route's shared guard list is emptied while the app is built);
the guards themselves are covered by ``test_auth.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from uuid import UUID

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import AsyncTestClient
from litestar.testing import create_async_test_client
from sqlalchemy import func
from sqlalchemy import select

from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.web.api_native import bot_usage as bot_usage_module
from smarter_dev.web.api_native.bot_usage import BotUsageController
from smarter_dev.web.llm_pricing import calc_cost_by_request
from smarter_dev.web.models import UsageCostRow

HAIKU = "claude-haiku-5-5"
GEMINI = "gemini-3.8-flash"
ENGAGEMENT = "8f2ca1de-4b7e-4c3a-9d1f-0e6b5a7c9d2e"


@pytest.fixture
async def client(db_session) -> AsyncIterator[AsyncTestClient]:
    original_guards = list(bot_usage_module.BOT_API_GUARDS)
    bot_usage_module.BOT_API_GUARDS.clear()
    try:
        async with create_async_test_client(
            route_handlers=[BotUsageController],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
        ) as test_client:
            yield test_client
    finally:
        bot_usage_module.BOT_API_GUARDS[:] = original_guards


def _request(
    input_tokens=0, output_tokens=0, cache_read_tokens=0, cache_write_tokens=0
):
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
    }


def _stage(model_name: str, *requests: dict) -> dict:
    return {
        "model_name": model_name,
        **{
            key: sum(request[key] for request in requests)
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        },
        "requests": list(requests),
    }


def _payload(**over) -> dict:
    payload = {
        "operation_id": "3f2b9c0d5e6a4b7c8d9e0f1a2b3c4d5e",
        "operation_type": "chat_web_summarizer",
        "metered_at": "2026-10-18T12:00:00+00:00",
        "discord_user_id": "666666666666666666",
        "guild_id": "333333333333333333",
        "channel_id": "555555555555555555",
        "engagement_id": ENGAGEMENT,
        "reasoning_level": "low",
        "failed": False,
        "stages": [_stage(HAIKU, _request(1_000, 100, 200, 50))],
    }
    payload.update(over)
    return payload


async def _rows(db_session) -> list[UsageCostRow]:
    return list(
        (
            await db_session.scalars(
                select(UsageCostRow).order_by(UsageCostRow.provider_key)
            )
        ).all()
    )


async def test_a_report_lands_one_discord_row_per_model(client, db_session):
    payload = _payload(
        stages=[
            _stage(HAIKU, _request(1_000, 100, 200, 50), _request(2_000, 300)),
            _stage(GEMINI, _request(4_000, 400, 1_000)),
        ]
    )

    response = await client.post("/api/bot-usage", json=payload)

    assert response.status_code == 200
    assert response.json() == {"recorded": 2}
    anthropic_row, google_row = await _rows(db_session)
    assert anthropic_row.operation_key == (
        f"bot:chat_web_summarizer:{payload['operation_id']}:{HAIKU}"
    )
    assert google_row.operation_key == (
        f"bot:chat_web_summarizer:{payload['operation_id']}:{GEMINI}"
    )
    for row in (anthropic_row, google_row):
        assert row.product_mode == "discord"
        assert row.operation_type == "chat_web_summarizer"
        assert row.discord_user_id == "666666666666666666"
        assert row.conversation_id == UUID(ENGAGEMENT)
        assert row.user_id is None
        assert row.root_turn_id is None
        assert row.reasoning_level == "low"
        assert row.overage_cost_usd == Decimal("0")
        assert row.metered_at.isoformat().startswith("2026-10-18T12:00:00")
    assert (anthropic_row.provider_key, anthropic_row.model_id) == ("anthropic", HAIKU)
    assert (google_row.provider_key, google_row.model_id) == ("google", GEMINI)
    assert (
        anthropic_row.input_tokens,
        anthropic_row.output_tokens,
        anthropic_row.cache_read_tokens,
        anthropic_row.cache_write_tokens,
    ) == (3_000, 400, 200, 50)
    assert anthropic_row.details == {
        "guild_id": "333333333333333333",
        "channel_id": "555555555555555555",
        "requests": 2,
    }
    assert google_row.details["requests"] == 1
    assert google_row.cost_usd == calc_cost_by_request(
        GEMINI,
        RequestTokens(4_000, 400, 1_000, 0),
        [RequestTokens(4_000, 400, 1_000, 0)],
    )


async def test_each_request_is_priced_on_its_own(client, db_session):
    # 150k + 40k input: the total alone is base rate, but the first request
    # passed 100k on its own and bills Haiku's long-context tier.
    long_request = _request(150_000, 1_000)
    short_request = _request(40_000, 500)
    payload = _payload(stages=[_stage(HAIKU, long_request, short_request)])

    response = await client.post("/api/bot-usage", json=payload)

    assert response.status_code == 200
    [row] = await _rows(db_session)
    total = RequestTokens(190_000, 1_500, 0, 0)
    requests = [RequestTokens(150_000, 1_000, 0, 0), RequestTokens(40_000, 500, 0, 0)]
    assert row.cost_usd == calc_cost_by_request(HAIKU, total, requests)
    assert row.cost_usd > calc_cost_by_request(HAIKU, total, [])


async def test_a_replayed_report_writes_nothing(client, db_session):
    payload = _payload()

    first = await client.post("/api/bot-usage", json=payload)
    again = await client.post("/api/bot-usage", json=payload)

    assert first.json() == {"recorded": 1}
    assert again.status_code == 200
    assert again.json() == {"recorded": 0}
    assert await db_session.scalar(select(func.count()).select_from(UsageCostRow)) == 1
    # The same id under another operation type is another run.
    other = await client.post(
        "/api/bot-usage", json=_payload(operation_type="chat_media_reader")
    )
    assert other.json() == {"recorded": 1}


@pytest.mark.parametrize(
    "operation_type",
    ["Length_Summarizer", "length summarizer", "length/summarizer", "", "x" * 41],
)
async def test_operation_type_must_be_short_lowercase_snake_case(
    client, db_session, operation_type
):
    response = await client.post(
        "/api/bot-usage", json=_payload(operation_type=operation_type)
    )

    assert response.status_code == 422
    assert await _rows(db_session) == []


async def test_a_forty_character_operation_type_is_accepted(client, db_session):
    response = await client.post(
        "/api/bot-usage", json=_payload(operation_type="x" * 40)
    )

    assert response.status_code == 200
    [row] = await _rows(db_session)
    assert row.operation_type == "x" * 40


@pytest.mark.parametrize("operation_id", ["has space", "a/b", "", "x" * 65])
async def test_operation_id_is_validated(client, db_session, operation_id):
    response = await client.post(
        "/api/bot-usage", json=_payload(operation_id=operation_id)
    )

    assert response.status_code == 422
    assert await _rows(db_session) == []


async def test_a_failed_run_is_flagged_in_details(client, db_session):
    response = await client.post("/api/bot-usage", json=_payload(failed=True))

    assert response.status_code == 200
    [row] = await _rows(db_session)
    assert row.details["failed"] is True
    assert row.product_mode == "discord"


async def test_a_run_without_location_has_no_location_details(client, db_session):
    response = await client.post(
        "/api/bot-usage",
        json=_payload(
            guild_id=None, channel_id=None, engagement_id=None, discord_user_id=None
        ),
    )

    assert response.status_code == 200
    [row] = await _rows(db_session)
    assert row.details == {"requests": 1}
    assert row.conversation_id is None
    assert row.discord_user_id is None


async def test_product_mode_is_always_discord_never_chat(client, db_session):
    # The bot cannot bill a user's Chat window: a product_mode in the report
    # is not part of the contract and is never honoured.
    response = await client.post(
        "/api/bot-usage", json={**_payload(), "product_mode": "chat"}
    )

    assert response.status_code == 200
    [row] = await _rows(db_session)
    assert row.product_mode == "discord"
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(UsageCostRow)
            .where(UsageCostRow.product_mode == "chat")
        )
        == 0
    )


async def test_a_stage_with_no_tokens_writes_no_row(client, db_session):
    response = await client.post(
        "/api/bot-usage",
        json=_payload(
            stages=[_stage(HAIKU, _request()), _stage(GEMINI, _request(10, 1))]
        ),
    )

    assert response.json() == {"recorded": 1}
    [row] = await _rows(db_session)
    assert row.model_id == GEMINI


async def test_a_report_without_stages_is_refused(client, db_session):
    response = await client.post("/api/bot-usage", json=_payload(stages=[]))

    assert response.status_code == 422
    assert await _rows(db_session) == []
