"""The Discord chat turn's side agents reach the usage ledger.

The length summarizer, the chat tools' sub-agents (web summarizer, media
reader, image-prompt reviewer), the /rule matcher and handler authoring each
run on their own model. Every test drives the real call site with its model
swapped for a FunctionModel reporting known per-request usage, captures what
the bot posts to ``/bot-usage``, and (for at least one site per group) feeds
that payload through the real ``BotUsageController`` into the sqlite ledger.
No live calls.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch
from uuid import UUID

import pytest
from PIL import Image
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from smarter_dev.bot.agents import chat_tools
from smarter_dev.bot.agents import handler_authoring
from smarter_dev.bot.agents import handler_tools
from smarter_dev.bot.agents import image_prompt_reviewer
from smarter_dev.bot.agents import media_reader
from smarter_dev.bot.agents import response_fitting
from smarter_dev.bot.agents import rule_matcher
from smarter_dev.bot.agents import web_summarizer
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.plugins import admin_handlers
from smarter_dev.bot.plugins import rules as rules_plugin
from smarter_dev.bot.services.rate_limiter import RateLimiter
from smarter_dev.bot.services.usage_reporter import configure_usage_reporter
from smarter_dev.bot.services.usage_reporter import drain_usage_reports
from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.web.api_native.bot_usage import BotUsageController
from smarter_dev.web.api_native.schemas import BotUsageOperationWrite
from smarter_dev.web.llm_pricing import calc_cost_by_request
from smarter_dev.web.models import UsageCostRow
from tests.bot.admin_handlers_plugin_test import _FakeAPI as _AdminAPI
from tests.bot.admin_handlers_plugin_test import _FakeCtx as _AdminCtx
from tests.bot.handler_authoring_test import ADMIN_SCRIPT
from tests.bot.handler_authoring_test import GOOD_SCRIPT
from tests.bot.handler_authoring_test import _settings_with
from tests.bot.test_rules_command import CHANNEL_ID as RULE_CHANNEL
from tests.bot.test_rules_command import GUILD_ID as RULE_GUILD
from tests.bot.test_rules_command import INVOKER_ID as RULE_INVOKER
from tests.bot.test_rules_command import RULES_MARKDOWN
from tests.bot.test_rules_command import _ctx as _rule_ctx
from tests.bot.test_rules_command import _FakeSessionCtx

HAIKU = "claude-haiku-5-5"
GEMINI = "gemini-3.8-flash"
SOL = "gpt-6.1-sol"
GUILD = 333333333333333333
CHANNEL = 555555555555555555
USER = 666666666666666666
ENGAGEMENT = UUID("8f2ca1de-4b7e-4c3a-9d1f-0e6b5a7c9d2e")
LONG = "z" * 4000


class _Api:
    def __init__(self):
        self.posts: list[dict] = []

    async def post(self, path, json_data=None, **_):
        assert path == "/bot-usage"
        self.posts.append(json_data)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-test")
    client = _Api()
    configure_usage_reporter(client)
    yield client
    configure_usage_reporter(None)


async def _posted(api: _Api) -> list[dict]:
    await drain_usage_reports()
    return api.posts


async def _land(db_session, payload: dict) -> list[UsageCostRow]:
    """The payload through the real endpoint handler into the ledger."""
    data = BotUsageOperationWrite.model_validate(payload)
    await BotUsageController.record_bot_usage.fn(None, db_session=db_session, data=data)
    return list(
        (
            await db_session.scalars(
                select(UsageCostRow)
                .where(
                    UsageCostRow.operation_key.startswith(
                        f"bot:{payload['operation_type']}:{payload['operation_id']}:"
                    )
                )
                .order_by(UsageCostRow.provider_key)
            )
        ).all()
    )


def _tokens(usage: RequestUsage) -> RequestTokens:
    return RequestTokens(
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
    )


def _text_model(*usages: RequestUsage, text: str = "short", name: str = HAIKU):
    queue = list(usages)

    def respond(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)], usage=queue.pop(0))

    return FunctionModel(respond, model_name=name)


def _output_model(*answers, name: str):
    """Answers each request with the output tool; an answer may be a callable
    of the request number, or a usage to pair with the last args."""
    queue = list(answers)

    def respond(messages, info: AgentInfo) -> ModelResponse:
        args, usage = queue.pop(0) if len(queue) > 1 else queue[0]
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, args)], usage=usage
        )

    return FunctionModel(respond, model_name=name)


def _broken_model(usage: RequestUsage, *, name: str, calls: list | None = None):
    """Billed every time, never a usable answer: it calls a tool that does not
    exist until the run gives up."""

    def respond(messages, info: AgentInfo) -> ModelResponse:
        if calls is not None:
            calls.append(usage)
        return ModelResponse(parts=[ToolCallPart("no_such_tool", {})], usage=usage)

    return FunctionModel(respond, model_name=name)


def _stage(payload: dict) -> dict:
    [stage] = payload["stages"]
    return stage


def _assert_row(row: UsageCostRow, *, model, provider, usages, payload) -> None:
    requests = [_tokens(usage) for usage in usages]
    total = RequestTokens(*(sum(column) for column in zip(*requests)))
    assert row.product_mode == "discord"
    assert row.operation_type == payload["operation_type"]
    assert row.provider_key == provider
    assert row.model_id == model
    assert (
        row.input_tokens,
        row.output_tokens,
        row.cache_read_tokens,
        row.cache_write_tokens,
    ) == tuple(total)
    assert row.cost_usd == calc_cost_by_request(model, total, requests)
    assert row.details["requests"] == len(requests)


# -- length summarizer ---------------------------------------------------------------


def _install_summarizer(monkeypatch, model) -> None:
    monkeypatch.setattr(response_fitting, "_length_summarizer", None)
    with patch.object(response_fitting, "build_agent_model", return_value=model):
        response_fitting.get_length_summarizer()


async def test_length_summarizer_lands_a_long_context_row_per_request(
    api, db_session, monkeypatch
):
    # One request past 100k input tokens bills Haiku's long-context tier.
    usage = RequestUsage(input_tokens=150_000, output_tokens=200, cache_write_tokens=40)
    _install_summarizer(monkeypatch, _text_model(usage, text="condensed"))

    fit = await response_fitting.fit_writer_message(
        LONG,
        usage_fields={
            "guild_id": GUILD,
            "channel_id": CHANNEL,
            "engagement_id": ENGAGEMENT,
            "discord_user_id": USER,
        },
    )

    assert fit.method == "summarized"
    [payload] = await _posted(api)
    assert payload["operation_type"] == "length_summarizer"
    assert payload["failed"] is False
    [row] = await _land(db_session, payload)
    _assert_row(row, model=HAIKU, provider="anthropic", usages=[usage], payload=payload)
    total = _tokens(usage)
    assert row.cost_usd != calc_cost_by_request(HAIKU, total, [])
    assert row.discord_user_id == str(USER)
    assert row.conversation_id == ENGAGEMENT
    assert row.details["guild_id"] == str(GUILD)
    assert row.details["channel_id"] == str(CHANNEL)
    # A replayed report writes nothing more.
    assert await _land(db_session, payload) == [row]


async def test_a_length_summarizer_that_gives_up_still_reports_its_requests(
    api, db_session, monkeypatch
):
    calls: list = []
    usage = RequestUsage(input_tokens=3_000, output_tokens=50)
    _install_summarizer(monkeypatch, _broken_model(usage, name=HAIKU, calls=calls))

    fit = await response_fitting.fit_overlong_response(
        LONG,
        agent=None,
        deps=None,
        message_history=[],
        usage_fields={"guild_id": GUILD, "channel_id": CHANNEL},
    )

    assert fit.method == "truncated"
    [payload] = await _posted(api)
    assert payload["failed"] is True
    assert len(calls) >= 2
    assert _stage(payload)["requests"] == [_tokens(u).as_dict() for u in calls]
    [row] = await _land(db_session, payload)
    _assert_row(row, model=HAIKU, provider="anthropic", usages=calls, payload=payload)
    assert row.details["failed"] is True


# -- chat tools: web summarizer, media reader, image-prompt reviewer ------------------


def _chat_ctx(**over) -> SimpleNamespace:
    bot = MagicMock()
    bot.rest.create_message = AsyncMock()
    fields = {
        "bot": bot,
        "channel_id": CHANNEL,
        "guild_id": GUILD,
        "engagement_id": ENGAGEMENT,
    }
    return SimpleNamespace(deps=ChatDeps(**{**fields, **over}))


def _install_web_summarizer(monkeypatch, primary, fallback) -> None:
    monkeypatch.setattr(web_summarizer, "_summarizer_agent", None)
    monkeypatch.setattr(web_summarizer, "_fallback_summarizer_agent", None)
    with patch.object(web_summarizer, "build_model_for", return_value=primary):
        web_summarizer.get_web_summarizer_agent()
    with patch.object(web_summarizer, "build_model_for", return_value=fallback):
        web_summarizer.get_web_summarizer_fallback_agent()


async def test_web_read_counts_both_summarizer_providers(api, db_session, monkeypatch):
    # Haiku answers (and bills) but never usably, so Gemini takes over: both
    # providers' requests are one chat_web_summarizer run, a row per model.
    calls: list = []
    primary_usage = RequestUsage(input_tokens=120_000, output_tokens=30)
    fallback_usage = RequestUsage(
        input_tokens=9_000, output_tokens=400, cache_read_tokens=1_000
    )
    _install_web_summarizer(
        monkeypatch,
        _broken_model(primary_usage, name=HAIKU, calls=calls),
        _text_model(fallback_usage, text="the summary", name=GEMINI),
    )
    monkeypatch.setattr(web_summarizer, "record_llm_failover", MagicMock())
    monkeypatch.setattr(
        chat_tools.web_fetch,
        "fetch_via_jina",
        AsyncMock(return_value={"title": "T", "content": "page body"}),
    )

    out = await chat_tools.web_read(
        _chat_ctx(), "https://example.test/page", "summarize"
    )

    assert out["summary"] == "the summary"
    [payload] = await _posted(api)
    assert payload["operation_type"] == "chat_web_summarizer"
    assert payload["failed"] is False
    assert (payload["guild_id"], payload["channel_id"], payload["engagement_id"]) == (
        str(GUILD),
        str(CHANNEL),
        str(ENGAGEMENT),
    )
    anthropic_row, google_row = await _land(db_session, payload)
    _assert_row(
        anthropic_row, model=HAIKU, provider="anthropic", usages=calls, payload=payload
    )
    _assert_row(
        google_row,
        model=GEMINI,
        provider="google",
        usages=[fallback_usage],
        payload=payload,
    )
    assert {anthropic_row.conversation_id, google_row.conversation_id} == {ENGAGEMENT}


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, "PNG")
    return buffer.getvalue()


async def test_web_read_of_an_image_reports_the_media_reader(api, monkeypatch):
    usage = RequestUsage(input_tokens=1_500, output_tokens=60)
    monkeypatch.setattr(media_reader, "_media_reader_agent", None)
    monkeypatch.setattr(
        media_reader, "_build_model", lambda: _text_model(usage, text="a red square")
    )
    monkeypatch.setattr(
        chat_tools.web_fetch,
        "fetch_bytes",
        AsyncMock(return_value=(_png(), "image/png")),
    )

    out = await chat_tools.web_read(
        _chat_ctx(), "https://example.test/pic.png", "what?"
    )

    assert out["summary"] == "a red square"
    [payload] = await _posted(api)
    assert payload["operation_type"] == "chat_media_reader"
    assert (payload["guild_id"], payload["channel_id"], payload["engagement_id"]) == (
        str(GUILD),
        str(CHANNEL),
        str(ENGAGEMENT),
    )
    assert _stage(payload)["model_name"] == HAIKU
    assert _stage(payload)["requests"] == [_tokens(usage).as_dict()]


async def test_a_media_read_that_fails_after_answering_still_reports(api, monkeypatch):
    calls: list = []
    usage = RequestUsage(input_tokens=2_000, output_tokens=10)
    monkeypatch.setattr(media_reader, "_media_reader_agent", None)
    monkeypatch.setattr(
        media_reader,
        "_build_model",
        lambda: _broken_model(usage, name=HAIKU, calls=calls),
    )
    monkeypatch.setattr(
        chat_tools.web_fetch,
        "fetch_bytes",
        AsyncMock(return_value=(_png(), "image/png")),
    )

    out = await chat_tools.web_read(
        _chat_ctx(), "https://example.test/pic.png", "what?"
    )

    assert out["error"] == "media_read_failed"
    [payload] = await _posted(api)
    assert payload["operation_type"] == "chat_media_reader"
    assert payload["failed"] is True
    assert _stage(payload)["requests"] == [_tokens(u).as_dict() for u in calls]


class _QuotaAPI:
    async def get(self, path, params=None):
        return SimpleNamespace(
            status_code=200, json=lambda: {"remaining": 3, "limit": 5}
        )

    async def post(self, path, json_data=None):  # pragma: no cover - never reached
        raise AssertionError(f"rejected prompt must not reserve: {path}")


async def test_generate_image_reports_the_prompt_review(api, monkeypatch):
    usage = RequestUsage(input_tokens=2_400, output_tokens=80, cache_read_tokens=2_000)
    monkeypatch.setattr(image_prompt_reviewer, "_reviewer_agent", None)
    monkeypatch.setattr(
        image_prompt_reviewer,
        "_build_model",
        lambda: _output_model(
            ({"approved": False, "reason": "not technical"}, usage), name=HAIKU
        ),
    )

    out = await chat_tools.generate_image(_chat_ctx(api_client=_QuotaAPI()), "a sunset")

    assert "rejected" in out
    [payload] = await _posted(api)
    assert payload["operation_type"] == "chat_image_prompt_review"
    assert (payload["guild_id"], payload["channel_id"], payload["engagement_id"]) == (
        str(GUILD),
        str(CHANNEL),
        str(ENGAGEMENT),
    )
    assert _stage(payload)["requests"] == [_tokens(usage).as_dict()]


# -- /rule matcher ---------------------------------------------------------------------


async def _run_rule_command(monkeypatch, model):
    monkeypatch.setattr(rule_matcher, "_rule_matcher_agent", None)
    with patch.object(rule_matcher, "build_model_for", return_value=model):
        rule_matcher.get_rule_matcher_agent()
    monkeypatch.setattr(rules_plugin, "rate_limiter", RateLimiter())
    ctx = _rule_ctx()
    with (
        patch.object(
            rules_plugin,
            "get_db_session_context",
            return_value=_FakeSessionCtx(AsyncMock()),
        ),
        patch.object(
            rules_plugin.guild_rules_ops,
            "get_config",
            AsyncMock(return_value=SimpleNamespace(rules_markdown=RULES_MARKDOWN)),
        ),
    ):
        await rules_plugin.rule(ctx)
    return ctx


async def test_rule_command_lands_the_matcher_as_the_moderators_spend(
    api, db_session, monkeypatch
):
    usage = RequestUsage(input_tokens=4_000, output_tokens=120)
    citation = {
        "matched_rule_indices": [1],
        "target_message_id": "1001",
        "explanation": "They advertised a paid course without asking.",
    }
    ctx = await _run_rule_command(
        monkeypatch, _output_model((citation, usage), name=GEMINI)
    )

    ctx.bot.rest.create_message.assert_awaited_once()
    [payload] = await _posted(api)
    assert payload["operation_type"] == "rule_matcher"
    [row] = await _land(db_session, payload)
    _assert_row(row, model=GEMINI, provider="google", usages=[usage], payload=payload)
    assert row.discord_user_id == str(RULE_INVOKER)
    assert row.details["guild_id"] == str(RULE_GUILD)
    assert row.details["channel_id"] == str(RULE_CHANNEL)
    assert row.conversation_id is None


async def test_a_rule_match_that_gives_up_still_reports(api, monkeypatch):
    calls: list = []
    usage = RequestUsage(input_tokens=4_000, output_tokens=5)
    with pytest.raises(UnexpectedModelBehavior):
        await _run_rule_command(
            monkeypatch, _broken_model(usage, name=GEMINI, calls=calls)
        )

    [payload] = await _posted(api)
    assert payload["operation_type"] == "rule_matcher"
    assert payload["failed"] is True
    assert payload["discord_user_id"] == str(RULE_INVOKER)
    assert _stage(payload)["requests"] == [_tokens(u).as_dict() for u in calls]


# -- handler authoring -----------------------------------------------------------------


def _verdict(approved: bool, reason: str = "fine") -> dict:
    checks = (
        "sandbox_valid",
        "within_limits",
        "memory_bounded",
        "guards_effective",
        "agent_verdict_safe",
        "actions_appropriate",
        "transparent",
        "schedule_reasonable",
    )
    return {**dict.fromkeys(checks, True), "approved": approved, "reason": reason}


class _HandlerAPI:
    def __init__(self):
        self.posted: list = []

    async def get(self, path, params=None):
        return SimpleNamespace(status_code=200, json=lambda: [])

    async def post(self, path, json_data=None):
        self.posted.append((path, json_data))
        return SimpleNamespace(
            status_code=201,
            json=lambda: {
                "handler_id": "H1",
                "name": "huzzah-reactor",
                "description": "d",
            },
        )


async def test_register_handler_reports_the_author_and_the_judge(
    api, db_session, monkeypatch
):
    monkeypatch.setattr(handler_authoring, "get_settings", _settings_with)
    author_usage = RequestUsage(input_tokens=110_000, output_tokens=900)
    judge_usage = RequestUsage(input_tokens=6_000, output_tokens=300)
    plan = {
        "feasible": True,
        "action": "create",
        "name": "huzzah-reactor",
        "trigger_type": "message",
        "settings": {},
        "description": "reacts to huzzah",
        "script": GOOD_SCRIPT,
    }
    monkeypatch.setattr(handler_authoring, "_author_agent", None)
    monkeypatch.setattr(handler_authoring, "_judge_agent", None)
    with patch.object(
        handler_authoring,
        "_build_configured_model",
        return_value=_output_model((plan, author_usage), name=HAIKU),
    ):
        handler_authoring._get_author_agent()
    with patch.object(
        handler_authoring,
        "_build_configured_model",
        return_value=_output_model((_verdict(True), judge_usage), name=HAIKU),
    ):
        handler_authoring._get_judge_agent()
    handler_api = _HandlerAPI()

    out = await handler_tools.register_handler(
        _chat_ctx(api_client=handler_api), "react on huzzah", "new message"
    )

    assert "Created handler" in out
    author, judge = await _posted(api)
    assert (author["operation_type"], judge["operation_type"]) == (
        "handler_author",
        "handler_judge",
    )
    assert author["operation_id"] != judge["operation_id"]
    for payload in (author, judge):
        assert (
            payload["guild_id"],
            payload["channel_id"],
            payload["engagement_id"],
        ) == (
            str(GUILD),
            str(CHANNEL),
            str(ENGAGEMENT),
        )
    [author_row] = await _land(db_session, author)
    _assert_row(
        author_row,
        model=HAIKU,
        provider="anthropic",
        usages=[author_usage],
        payload=author,
    )
    [judge_row] = await _land(db_session, judge)
    _assert_row(
        judge_row,
        model=HAIKU,
        provider="anthropic",
        usages=[judge_usage],
        payload=judge,
    )


def _install_admin_models(monkeypatch, author_model, judge_models: dict) -> None:
    monkeypatch.setattr(
        handler_authoring,
        "get_settings",
        lambda: _settings_with(
            handler_admin_author_model=SOL,
            handler_admin_judge_model=SOL,
            handler_admin_second_judge_model=GEMINI,
        ),
    )
    monkeypatch.setattr(handler_authoring, "_admin_author_agent", None)
    monkeypatch.setattr(handler_authoring, "_admin_judge_agents", {})
    with patch.object(
        handler_authoring, "_build_configured_model", return_value=author_model
    ):
        handler_authoring._get_admin_author_agent()
    with patch.object(
        handler_authoring, "_build_configured_model", side_effect=judge_models.get
    ):
        for model_id in judge_models:
            handler_authoring._get_admin_judge_agent(model_id)


def _wire_admin_command(monkeypatch) -> _AdminAPI:
    async def allow_admin(ctx, message):
        return False

    async def no_handlers(api, guild_id):
        return []

    admin_api = _AdminAPI()
    monkeypatch.setattr(admin_handlers, "deny_if_not_admin", allow_admin)
    monkeypatch.setattr(admin_handlers, "_api_client", lambda: admin_api)
    monkeypatch.setattr(
        admin_handlers, "_guild_admin_handlers_with_scripts", no_handlers
    )
    return admin_api


_ADMIN_PLAN = {
    "feasible": True,
    "action": "create",
    "name": "raid-alarm",
    "trigger_type": "message",
    "channel_ids": [],
    "settings": {},
    "description": "bans scammers",
    "script": ADMIN_SCRIPT,
}


async def test_admin_create_reports_every_author_draft_and_both_judges(
    api, db_session, monkeypatch
):
    # Gemini rejects the first draft, so the author writes a revision and both
    # judges review again: two author runs, four judge runs.
    author_usage = RequestUsage(input_tokens=20_000, output_tokens=2_000)
    sol_usage = RequestUsage(input_tokens=8_000, output_tokens=500)
    gemini_usage = RequestUsage(input_tokens=7_000, output_tokens=400)
    gemini_answers = iter(
        [
            (_verdict(False, "it bans on a guess"), gemini_usage),
            (_verdict(True), gemini_usage),
        ]
    )

    def gemini_judge(messages, info):
        args, usage = next(gemini_answers)
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, args)], usage=usage
        )

    _install_admin_models(
        monkeypatch,
        _output_model((_ADMIN_PLAN, author_usage), name=SOL),
        {
            SOL: _output_model((_verdict(True), sol_usage), name=SOL),
            GEMINI: FunctionModel(gemini_judge, model_name=GEMINI),
        },
    )
    admin_api = _wire_admin_command(monkeypatch)
    ctx = _AdminCtx()

    await admin_handlers._run_create_admin_handler(ctx, "ban scammers")

    assert admin_api.posted and "Created" in ctx.edits[-1]
    payloads = await _posted(api)
    by_type: dict[str, list[dict]] = {}
    for payload in payloads:
        by_type.setdefault(payload["operation_type"], []).append(payload)
    assert len(by_type["admin_handler_author"]) == 2
    assert len(by_type["admin_handler_judge"]) == 4
    assert len({payload["operation_id"] for payload in payloads}) == 6
    judged = sorted(_stage(p)["model_name"] for p in by_type["admin_handler_judge"])
    assert judged == [GEMINI, GEMINI, SOL, SOL]
    for payload in payloads:
        assert payload["discord_user_id"] == _AdminCtx.author.id
        assert payload["guild_id"] == _AdminCtx.guild_id
        assert payload["channel_id"] == _AdminCtx.channel_id
    [author_row] = await _land(db_session, by_type["admin_handler_author"][0])
    _assert_row(
        author_row,
        model=SOL,
        provider="openai",
        usages=[author_usage],
        payload=by_type["admin_handler_author"][0],
    )
    gemini_judge_payload = next(
        p for p in by_type["admin_handler_judge"] if _stage(p)["model_name"] == GEMINI
    )
    [judge_row] = await _land(db_session, gemini_judge_payload)
    _assert_row(
        judge_row,
        model=GEMINI,
        provider="google",
        usages=[gemini_usage],
        payload=gemini_judge_payload,
    )
    assert judge_row.discord_user_id == _AdminCtx.author.id


async def test_an_admin_judge_that_raises_still_reports_both_judges(api, monkeypatch):
    calls: list = []
    sol_usage = RequestUsage(input_tokens=8_000, output_tokens=500)
    _install_admin_models(
        monkeypatch,
        _output_model(
            (_ADMIN_PLAN, RequestUsage(input_tokens=10, output_tokens=10)), name=SOL
        ),
        {
            SOL: _output_model((_verdict(True), sol_usage), name=SOL),
            GEMINI: _broken_model(
                RequestUsage(input_tokens=7_000, output_tokens=5),
                name=GEMINI,
                calls=calls,
            ),
        },
    )
    _wire_admin_command(monkeypatch)
    ctx = _AdminCtx()

    with pytest.raises(UnexpectedModelBehavior):
        await admin_handlers._run_create_admin_handler(ctx, "ban scammers")

    judges = [
        p for p in await _posted(api) if p["operation_type"] == "admin_handler_judge"
    ]
    by_model = {_stage(p)["model_name"]: p for p in judges}
    assert set(by_model) == {SOL, GEMINI}
    assert {p["discord_user_id"] for p in judges} == {_AdminCtx.author.id}
    assert by_model[SOL]["failed"] is False
    assert by_model[GEMINI]["failed"] is True
    assert _stage(by_model[GEMINI])["requests"] == [_tokens(u).as_dict() for u in calls]
