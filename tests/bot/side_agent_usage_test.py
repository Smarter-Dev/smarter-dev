"""The bot's utility agents, privacy purges and message gate reach the ledger.

Each test drives the real call site with its model swapped for a FunctionModel
that reports known per-request usage, captures what the bot posts to
``/bot-usage``, and (for at least one site per group) feeds that payload
through the real ``BotUsageController`` into the sqlite ledger. No live calls.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from smarter_dev.bot.agents import forum_agent
from smarter_dev.bot.agents import help_agent
from smarter_dev.bot.agents import message_gate
from smarter_dev.bot.agents import moderation_agent
from smarter_dev.bot.agents import streak_agent
from smarter_dev.bot.agents import tldr_agent
from smarter_dev.bot.agents import utility_model
from smarter_dev.bot.agents.message_gate import GateMessage
from smarter_dev.bot.plugins import timeout
from smarter_dev.bot.privacy import compaction
from smarter_dev.bot.privacy.compaction import PrivacyCompactionFailed
from smarter_dev.bot.services.forum_agent_service import ForumAgentService
from smarter_dev.bot.services.usage_reporter import configure_usage_reporter
from smarter_dev.bot.services.usage_reporter import drain_usage_reports
from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.web.api_native.bot_usage import BotUsageController
from smarter_dev.web.api_native.schemas import BotUsageOperationWrite
from smarter_dev.web.llm_pricing import calc_cost_by_request
from smarter_dev.web.models import UsageCostRow

HAIKU = "claude-haiku-5-5"
GUILD = "333333333333333333"
CHANNEL = "555555555555555555"
USER = "666666666666666666"
KAI = "111111111111111111"


class _Api:
    def __init__(self):
        self.posts: list[dict] = []

    async def post(self, path, json_data=None, **_):
        assert path == "/bot-usage"
        self.posts.append(json_data)


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
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
                select(UsageCostRow).where(
                    UsageCostRow.operation_key.startswith(
                        f"bot:{payload['operation_type']}:{payload['operation_id']}:"
                    )
                )
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


def _output_model(args, *usages: RequestUsage, name: str = HAIKU, calls=None):
    """Answers with the output tool; ``args`` may be a callable of the info."""
    queue = list(usages)

    def respond(messages, info: AgentInfo) -> ModelResponse:
        if calls is not None:
            calls.append(info)
        usage = queue.pop(0) if len(queue) > 1 else queue[0]
        payload = args(info) if callable(args) else args
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, payload)], usage=usage
        )

    return FunctionModel(respond, model_name=name)


def _install(monkeypatch, module, attr: str, getter, model) -> None:
    """Build the singleton agent through ``build_utility_agent`` on *model*."""
    monkeypatch.setattr(module, attr, None)
    with patch.object(utility_model, "build_model_for", return_value=model):
        getter()
    monkeypatch.setattr(module, attr, getattr(module, attr))


def _stage(payload: dict) -> dict:
    [stage] = payload["stages"]
    return stage


# -- utility agents ----------------------------------------------------------------


_HELP_CONTEXT = {
    "conversation_timeline": "[NEW] alice: how do squads work?",
    "users": [],
    "channel": {"name": "general"},
    "me": {"bot_id": "99"},
}


async def _help(**kwargs):
    builder = AsyncMock()
    builder.build_context.return_value = _HELP_CONTEXT
    with patch(
        "smarter_dev.bot.utils.messages.ConversationContextBuilder",
        return_value=builder,
    ):
        return await help_agent.HelpAgent().generate_response(
            user_question="how do squads work?",
            bot=object(),
            channel_id=int(CHANNEL),
            guild_id=int(GUILD),
            **kwargs,
        )


async def test_help_lands_one_ledger_row_priced_per_request(
    api, db_session, monkeypatch
):
    # One request past Haiku 5.5's 100k long-context threshold: priced per
    # request it bills at the long-context rate, which the total alone misses.
    usage = RequestUsage(
        input_tokens=150_000, output_tokens=900, cache_read_tokens=20_000
    )
    _install(
        monkeypatch,
        help_agent,
        "_help_agent",
        help_agent.get_help_agent,
        _output_model({"response": "Use /squads join."}, usage),
    )

    response, tokens = await _help(user_id=USER)

    assert response == "Use /squads join."
    assert tokens == 150_900  # the stored tokens_used integer is unchanged
    [payload] = await _posted(api)
    assert payload["operation_type"] == "help_agent"
    assert payload["discord_user_id"] == USER
    assert (payload["guild_id"], payload["channel_id"]) == (GUILD, CHANNEL)
    assert payload["reasoning_level"] == "medium"
    assert payload["failed"] is False
    assert "squads" not in json.dumps(payload)  # counts only, never content

    [row] = await _land(db_session, payload)
    request = _tokens(usage)
    assert row.product_mode == "discord"
    assert row.operation_type == "help_agent"
    assert row.provider_key == "anthropic"
    assert row.model_id == HAIKU
    assert row.discord_user_id == USER
    assert (
        row.input_tokens,
        row.output_tokens,
        row.cache_read_tokens,
        row.cache_write_tokens,
    ) == tuple(request)
    assert row.cost_usd == calc_cost_by_request(HAIKU, request, [request])
    assert row.cost_usd > calc_cost_by_request(HAIKU, request, [])
    assert row.details["guild_id"] == GUILD
    assert row.details["channel_id"] == CHANNEL
    assert row.details["requests"] == 1

    # A re-sent report of the same run writes nothing more.
    assert await _land(db_session, payload) == [row]


async def test_help_command_attributes_the_asker(api, monkeypatch):
    from smarter_dev.bot.plugins import help as help_plugin

    _install(
        monkeypatch,
        help_agent,
        "_help_agent",
        help_agent.get_help_agent,
        _output_model(
            {"response": "Use /squads join."},
            RequestUsage(input_tokens=700, output_tokens=70),
        ),
    )
    monkeypatch.setattr(help_plugin, "store_conversation", AsyncMock())
    builder = AsyncMock()
    builder.build_context.return_value = _HELP_CONTEXT
    with patch(
        "smarter_dev.bot.utils.messages.ConversationContextBuilder",
        return_value=builder,
    ):
        response = await help_plugin.generate_help_response(
            USER,
            "how do squads work?",
            bot=object(),
            guild_id=GUILD,
            channel_id=CHANNEL,
        )

    assert response == "Use /squads join."
    [payload] = await _posted(api)
    assert (
        payload["operation_type"],
        payload["discord_user_id"],
        payload["guild_id"],
        payload["channel_id"],
    ) == (
        "help_agent",
        USER,
        GUILD,
        CHANNEL,
    )


async def test_help_failure_after_a_billed_response_still_records(
    api, db_session, monkeypatch
):
    calls: list = []
    _install(
        monkeypatch,
        help_agent,
        "_help_agent",
        help_agent.get_help_agent,
        # Never valid output: the agent retries until it gives up.
        _output_model(
            {"wrong": 1},
            RequestUsage(input_tokens=1_000, output_tokens=50),
            calls=calls,
        ),
    )

    with pytest.raises(UnexpectedModelBehavior):
        await _help(user_id=USER)

    [payload] = await _posted(api)
    assert payload["failed"] is True
    assert len(calls) >= 2
    assert len(_stage(payload)["requests"]) == len(calls)
    [row] = await _land(db_session, payload)
    assert row.input_tokens == 1_000 * len(calls)
    assert row.details["failed"] is True


async def test_tldr_reports_a_failed_run_it_swallows(api, monkeypatch):
    calls: list = []
    _install(
        monkeypatch,
        tldr_agent,
        "_tldr_agent",
        tldr_agent.get_tldr_agent,
        _output_model(
            {"wrong": 1}, RequestUsage(input_tokens=500, output_tokens=20), calls=calls
        ),
    )
    from datetime import UTC
    from datetime import datetime

    from smarter_dev.bot.agents.models import DiscordMessage

    summary, tokens, used = await tldr_agent.TLDRAgent().generate_summary_async(
        [
            DiscordMessage(
                author="alice", timestamp=datetime.now(UTC), content="ship it?"
            )
        ],
        user_id=USER,
        guild_id=GUILD,
        channel_id=CHANNEL,
    )

    assert "Sorry" in summary and (tokens, used) == (0, 0)
    [payload] = await _posted(api)
    assert payload["operation_type"] == "tldr_agent"
    assert payload["failed"] is True
    assert (payload["discord_user_id"], payload["guild_id"], payload["channel_id"]) == (
        USER,
        GUILD,
        CHANNEL,
    )
    assert len(_stage(payload)["requests"]) == len(calls) >= 2


async def test_tldr_command_attributes_the_requester(api, monkeypatch):
    from smarter_dev.bot.plugins import llm

    _install(
        monkeypatch,
        tldr_agent,
        "_tldr_agent",
        tldr_agent.get_tldr_agent,
        _output_model(
            {"summary": "**Channel Summary**\nok"},
            RequestUsage(input_tokens=300, output_tokens=30),
        ),
    )
    monkeypatch.setattr(llm, "store_tldr_conversation", AsyncMock(return_value=True))
    from datetime import UTC
    from datetime import datetime

    from smarter_dev.bot.agents.models import DiscordMessage

    await llm.generate_tldr_summary(
        USER,
        [
            DiscordMessage(
                author="alice", timestamp=datetime.now(UTC), content="ship it?"
            )
        ],
        5,
        guild_id=GUILD,
        channel_id=CHANNEL,
        user_username="alice",
    )

    [payload] = await _posted(api)
    assert (
        payload["operation_type"],
        payload["discord_user_id"],
        payload["guild_id"],
        payload["channel_id"],
    ) == (
        "tldr_agent",
        USER,
        GUILD,
        CHANNEL,
    )
    assert _stage(payload)["requests"] == [
        {
            "input_tokens": 300,
            "output_tokens": 30,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }
    ]


async def test_streak_celebration_is_the_members_spend(api, monkeypatch):
    _install(
        monkeypatch,
        streak_agent,
        "_streak_celebration_agent",
        streak_agent.get_streak_celebration_agent,
        _output_model(
            {"response": "48 days!"}, RequestUsage(input_tokens=400, output_tokens=40)
        ),
    )

    (
        message,
        tokens,
    ) = await streak_agent.StreakCelebrationAgent().generate_celebration_message(
        bytes_earned=32,
        streak_multiplier=4,
        streak_days=48,
        user_id=int(USER),
        user_message="hi",
        guild_id=int(GUILD),
        channel_id=int(CHANNEL),
    )

    assert message.endswith("48 days!") and tokens == 440
    [payload] = await _posted(api)
    assert payload["operation_type"] == "streak_celebration"
    assert (payload["discord_user_id"], payload["guild_id"], payload["channel_id"]) == (
        USER,
        GUILD,
        CHANNEL,
    )
    assert _stage(payload)["model_name"] == HAIKU


async def test_timeout_message_is_the_moderators_spend(api, monkeypatch):
    _install(
        monkeypatch,
        timeout,
        "_timeout_message_agent",
        timeout.get_timeout_message_agent,
        _output_model(
            {"polite_message": "<@7> has been timed out."},
            RequestUsage(input_tokens=250, output_tokens=25),
        ),
    )

    await timeout.generate_timeout_message(
        "<@7>",
        "spamming",
        "10 minutes",
        moderator_id=USER,
        guild_id=GUILD,
        channel_id=CHANNEL,
    )

    [payload] = await _posted(api)
    assert payload["operation_type"] == "timeout_message"
    assert (payload["discord_user_id"], payload["guild_id"], payload["channel_id"]) == (
        USER,
        GUILD,
        CHANNEL,
    )
    assert "spamming" not in json.dumps(payload)


def _forum_answer(info: AgentInfo) -> dict:
    properties = info.output_tools[0].parameters_json_schema["properties"]
    answer = {}
    if "decision" in properties:
        answer.update(decision="helpful", confidence=0.9, response="Try this.")
    if "matching_topics" in properties:
        answer["matching_topics"] = ["rust"]
    return answer


async def test_forum_agents_report_every_mode_with_the_post_location(api, monkeypatch):
    model = _output_model(
        _forum_answer, RequestUsage(input_tokens=600, output_tokens=60)
    )
    _install(
        monkeypatch,
        forum_agent,
        "_monitor_agent",
        forum_agent.get_forum_monitor_agent,
        model,
    )
    _install(
        monkeypatch,
        forum_agent,
        "_topic_classifier_agent",
        forum_agent.get_forum_topic_classifier_agent,
        model,
    )
    _install(
        monkeypatch,
        forum_agent,
        "_combined_agent",
        forum_agent.get_forum_combined_agent,
        model,
    )
    service = ForumAgentService(api_client=MagicMock())
    agents = [
        {
            "id": f"a{n}",
            "name": f"a{n}",
            "guild_id": GUILD,
            "system_prompt": "help",
            "monitored_forums": [CHANNEL],
            **flags,
        }
        for n, flags in enumerate(
            [
                {"enable_responses": True, "enable_user_tagging": False},
                {"enable_responses": False, "enable_user_tagging": True},
                {"enable_responses": True, "enable_user_tagging": True},
            ]
        )
    ]
    monkeypatch.setattr(service, "load_guild_agents", AsyncMock(return_value=agents))
    monkeypatch.setattr(service, "check_rate_limit", AsyncMock(return_value=True))
    monkeypatch.setattr(service, "record_response", AsyncMock())
    monkeypatch.setattr(
        service, "get_notification_topics", AsyncMock(return_value=["rust"])
    )
    post = SimpleNamespace(
        title="borrowck",
        content="why",
        author_display_name="nia",
        tags=[],
        attachments=[],
        channel_id=CHANNEL,
        thread_id="9",
        guild_id=GUILD,
    )

    responses, _ = await service.process_forum_post_with_tagging(
        GUILD,
        post,
        user_subscriptions=[
            {"user_id": "1", "username": "u", "subscribed_topics": ["rust"]}
        ],
    )

    # The stored tokens_used integer is as before.
    assert [r["tokens_used"] for r in responses] == [660, 660, 660]
    payloads = await _posted(api)
    assert len(payloads) == 3
    assert len({p["operation_id"] for p in payloads}) == 3
    for payload in payloads:
        assert payload["operation_type"] == "forum_agent"
        assert (payload["guild_id"], payload["channel_id"]) == (GUILD, CHANNEL)
        assert payload["discord_user_id"] is None
        assert _stage(payload)["input_tokens"] == 600


async def test_moderation_triage_records_every_request_up_to_the_limit(
    api, db_session, monkeypatch
):
    def forever(messages, info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart("flag_users", {"user_ids": "42"})],
            usage=RequestUsage(
                input_tokens=2_000, output_tokens=30, cache_read_tokens=1_500
            ),
        )

    _install(
        monkeypatch,
        moderation_agent,
        "_moderation_agent",
        moderation_agent.get_moderation_agent,
        FunctionModel(forever, model_name=HAIKU),
    )

    _, tracker = await moderation_agent.run_moderation_agent(
        bot=MagicMock(),
        guild_id=GUILD,
        channel_id=CHANNEL,
        trigger_message_content="@mods",
        trigger_author="reporter",
        context_messages=[],
        guild_instructions="",
        enabled_tools=[],
        trigger_message_id="3",
    )

    assert tracker.failure == "UsageLimitExceeded"
    [payload] = await _posted(api)
    assert payload["operation_type"] == "moderation_triage"
    assert payload["failed"] is True
    assert payload["discord_user_id"] is None
    requests = _stage(payload)["requests"]
    assert len(requests) == moderation_agent.MAX_TRIAGE_REQUESTS
    [row] = await _land(db_session, payload)
    assert row.input_tokens == 2_000 * moderation_agent.MAX_TRIAGE_REQUESTS
    assert row.cache_read_tokens == 1_500 * moderation_agent.MAX_TRIAGE_REQUESTS
    request = RequestTokens(2_000, 30, 1_500, 0)
    assert row.cost_usd == calc_cost_by_request(
        HAIKU,
        RequestTokens(*(n * len(requests) for n in request)),
        [request] * len(requests),
    )


async def test_moderation_follow_up_is_part_of_the_triage_operation(api, monkeypatch):
    turns = [
        [ToolCallPart("flag_users", {"user_ids": "42"})],
        None,  # the final answer
    ]

    def triage(messages, info: AgentInfo) -> ModelResponse:
        parts = turns.pop(0) or [
            ToolCallPart(info.output_tools[0].name, {"assessment": "Flagged."})
        ]
        return ModelResponse(
            parts=parts, usage=RequestUsage(input_tokens=1_000, output_tokens=10)
        )

    _install(
        monkeypatch,
        moderation_agent,
        "_moderation_agent",
        moderation_agent.get_moderation_agent,
        FunctionModel(triage, model_name=HAIKU),
    )
    _install(
        monkeypatch,
        moderation_agent,
        "_follow_up_agent",
        moderation_agent.get_follow_up_agent,
        _output_model(
            {"message": "Please stop."}, RequestUsage(input_tokens=300, output_tokens=5)
        ),
    )

    _, tracker = await moderation_agent.run_moderation_agent(
        bot=MagicMock(),
        guild_id=GUILD,
        channel_id=CHANNEL,
        trigger_message_content="@mods",
        trigger_author="reporter",
        context_messages=[],
        guild_instructions="",
        enabled_tools=[],
    )

    assert tracker.channel_message == "Please stop."
    [payload] = await _posted(api)
    assert payload["failed"] is False
    assert [r["input_tokens"] for r in _stage(payload)["requests"]] == [
        1_000,
        1_000,
        300,
    ]


# -- message gate --------------------------------------------------------------------


def _gate_candidates() -> list[GateMessage]:
    return [
        GateMessage(message_id="1", author_display="alice", content="python?"),
        GateMessage(message_id="2", author_display="bob", content="lunch?"),
    ]


@pytest.fixture
def gate_model():
    message_gate._gate_model = None
    yield
    message_gate._gate_model = None


async def test_message_gate_lands_as_jev(api, db_session, gate_model):
    usage = RequestUsage(input_tokens=1_200)
    message_gate._gate_model = _output_model(
        {"candidate_1": True, "candidate_2": False}, usage, name="jev-1.13.0"
    )

    allowed = await message_gate.filter_messages(
        "only python",
        _gate_candidates(),
        [],
        guild_id=int(GUILD),
        channel_id=int(CHANNEL),
    )

    assert allowed == ["1"]
    [payload] = await _posted(api)
    assert payload["operation_type"] == "chat_message_gate"
    assert _stage(payload)["model_name"] == message_gate.GATE_MODEL_ID
    [row] = await _land(db_session, payload)
    assert (row.provider_key, row.model_id) == ("typesafe", "jev-1.13.0")
    assert row.input_tokens == 1_200
    assert row.cost_usd == calc_cost_by_request(
        message_gate.GATE_MODEL_ID, _tokens(usage), [_tokens(usage)]
    )
    assert row.cost_usd > 0
    assert (row.details["guild_id"], row.details["channel_id"]) == (GUILD, CHANNEL)


async def test_message_gate_fails_open_and_still_records(api, gate_model):
    # A missing field: retries=0, so the run raises after the billed response.
    message_gate._gate_model = _output_model(
        {"candidate_1": True}, RequestUsage(input_tokens=900), name="jev-1.13.0"
    )

    allowed = await message_gate.filter_messages(
        "only python", _gate_candidates(), [], guild_id=GUILD, channel_id=CHANNEL
    )

    assert allowed == ["1", "2"]
    [payload] = await _posted(api)
    assert payload["failed"] is True
    assert _stage(payload)["input_tokens"] == 900


async def test_message_gate_short_circuits_post_nothing(api, gate_model):
    assert await message_gate.filter_messages("  ", _gate_candidates(), []) == [
        "1",
        "2",
    ]
    assert await _posted(api) == []


# -- privacy purges ------------------------------------------------------------------


_TARGET = PurgeTarget(user_id=KAI, names=("kai",))
OPUS = "claude-opus-5-5"


def _chat_answer(*summaries: str):
    queue = list(summaries)

    def respond(messages, info: AgentInfo) -> ModelResponse:
        summary = queue.pop(0)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"summary": summary, "topic": "", "notes": ""},
                )
            ],
            usage=RequestUsage(input_tokens=4_000, output_tokens=200),
        )

    return FunctionModel(respond, model_name=OPUS)


def _history():
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.messages import UserPromptPart

    return [
        ModelRequest(
            parts=[
                UserPromptPart(f'<message user-id="{KAI}" username="kai">hi</message>')
            ]
        ),
        ModelResponse(parts=[TextPart("hello kai")]),
    ]


async def test_chat_purge_records_every_attempt_without_the_person(api, db_session):
    model = _chat_answer(f"kai ({KAI}) said hi", "nia said hi")

    result = await compaction.purge_chat_memory(
        _history(), None, None, _TARGET, model=model, guild_id=GUILD, channel_id=CHANNEL
    )

    assert "nia" in result.history[0].parts[-1].content
    [payload] = await _posted(api)
    assert payload["operation_type"] == "privacy_purge_chat_memory"
    assert payload["discord_user_id"] is None
    assert (payload["guild_id"], payload["channel_id"]) == (GUILD, CHANNEL)
    assert KAI not in json.dumps(payload)
    assert len(_stage(payload)["requests"]) == 2
    [row] = await _land(db_session, payload)
    assert row.discord_user_id is None
    assert (row.product_mode, row.provider_key, row.model_id) == (
        "discord",
        "anthropic",
        OPUS,
    )
    assert row.input_tokens == 8_000
    request = RequestTokens(4_000, 200)
    assert row.cost_usd == calc_cost_by_request(
        OPUS, RequestTokens(8_000, 400), [request, request]
    )


async def test_a_failed_chat_purge_still_records(api):
    model = _chat_answer(*[f"kai ({KAI})"] * 3)

    with pytest.raises(PrivacyCompactionFailed):
        await compaction.purge_chat_memory(
            _history(),
            None,
            None,
            _TARGET,
            model=model,
            guild_id=GUILD,
            channel_id=CHANNEL,
        )

    [payload] = await _posted(api)
    assert payload["failed"] is True
    assert len(_stage(payload)["requests"]) == compaction.ID_RETRIES + 1


async def test_each_purge_step_run_gets_its_own_id(api):
    model = _chat_answer("nia said hi", "nia said hi")
    for _ in range(2):
        await compaction.purge_chat_memory(_history(), None, None, _TARGET, model=model)

    payloads = await _posted(api)
    assert len({p["operation_id"] for p in payloads}) == 2


async def test_the_whole_purge_attributes_each_store_to_its_guild_and_channel(api):
    from tests.bot.privacy_purge_test import GUILD as PURGE_GUILD
    from tests.bot.privacy_purge_test import KAI as PURGE_KAI
    from tests.bot.privacy_purge_test import LIVE_CHANNEL
    from tests.bot.privacy_purge_test import STORED_CHANNEL
    from tests.bot.privacy_purge_test import UNPLACED_CHANNEL
    from tests.bot.privacy_purge_test import _command
    from tests.bot.privacy_purge_test import _consume_once
    from tests.bot.privacy_purge_test import _publish
    from tests.bot.privacy_purge_test import build_world

    world = await build_world()
    command = _command()
    await _publish(world.redis, command)
    await _consume_once(world)

    payloads = await _posted(api)
    seen = sorted(
        (p["operation_type"], p["guild_id"], p["channel_id"] or "") for p in payloads
    )
    assert seen == sorted(
        [
            ("privacy_purge_chat_memory", PURGE_GUILD, str(LIVE_CHANNEL)),
            ("privacy_purge_chat_memory", PURGE_GUILD, str(STORED_CHANNEL)),
            # A channel no guild claims is purged under the first guild.
            ("privacy_purge_chat_memory", PURGE_GUILD, str(UNPLACED_CHANNEL)),
            ("privacy_purge_proactive", PURGE_GUILD, ""),
            ("privacy_purge_proactive", PURGE_GUILD, str(LIVE_CHANNEL)),
            ("privacy_purge_watch", PURGE_GUILD, str(LIVE_CHANNEL)),
        ]
    )
    text = json.dumps(payloads)
    assert PURGE_KAI not in text and command["run_id"] not in text
    assert all(p["discord_user_id"] is None and not p["failed"] for p in payloads)
    assert len({p["operation_id"] for p in payloads}) == len(payloads)


async def test_proactive_and_watch_purges_report_their_runs(api):
    from datetime import UTC
    from datetime import datetime
    from datetime import timedelta

    from smarter_dev.bot.proactive.environment import InstructionStore
    from smarter_dev.bot.proactive.environment import WatchInstruction

    note = FunctionModel(
        lambda messages, info: ModelResponse(
            parts=[TextPart("nia asked about tokio")],
            usage=RequestUsage(input_tokens=7_000, output_tokens=100),
        ),
        model_name=OPUS,
    )
    await compaction.purge_proactive_history(
        _history(), _TARGET, model=note, guild_id=GUILD
    )
    store = InstructionStore(
        seed="seed",
        entries=[
            WatchInstruction(
                "w1", "wake when kai posts", datetime.now(UTC) + timedelta(hours=1)
            )
        ],
        _next_id=2,
    )
    decide = _output_model(
        {"decisions": [{"instruction_id": "w1", "action": "drop", "text": ""}]},
        RequestUsage(input_tokens=800, output_tokens=20),
        name=OPUS,
    )
    await compaction.purge_watch_instructions(
        store, _TARGET, model=decide, guild_id=GUILD, channel_id=CHANNEL
    )

    proactive, watch = await _posted(api)
    assert (
        proactive["operation_type"],
        proactive["guild_id"],
        proactive["channel_id"],
    ) == (
        "privacy_purge_proactive",
        GUILD,
        None,
    )
    assert _stage(proactive)["input_tokens"] == 7_000
    assert (watch["operation_type"], watch["channel_id"]) == (
        "privacy_purge_watch",
        CHANNEL,
    )
    assert _stage(watch)["input_tokens"] == 800
