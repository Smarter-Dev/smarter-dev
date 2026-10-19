"""The embedded proactive agent bills every provider request it was charged
for, priced one by one in the ledger (#211).

A wake runs through the real ProactiveRuntime: ``agent_runner_for`` builds the
real KimiAgentRunner and proactive agent, the real AgentConsumer runs it, the
runtime's own compaction_summarize writes the memory note, and
``_consume_guild_once`` reports the usage. Only the models are FunctionModels
answering with known per-request usage. The report the bot would POST is then
written by the real ``record_wake_usage`` endpoint into the test database.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from litestar.di import Provide
from litestar.plugins.pydantic import PydanticPlugin
from litestar.testing import create_test_client
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ThinkingPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from smarter_dev.bot.plugins import proactive
from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.proactive.watcher import SkimRunner
from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.web.api_native import proactive_settings as proactive_settings_module
from smarter_dev.web.llm_pricing import calc_cost
from smarter_dev.web.llm_pricing import calc_cost_by_request
from smarter_dev.web.models import UsageCostRow
from tests.bot.proactive_plugin_test import _fake_bot
from tests.bot.proactive_plugin_test import _StubSettingsService

AGENT = "claude-opus-5-5"
NOTE = RequestUsage(input_tokens=90_000, output_tokens=700)
FIRST = RequestUsage(input_tokens=20_000, output_tokens=30, cache_write_tokens=19_000)
SECOND = RequestUsage(input_tokens=20_500, output_tokens=50, cache_read_tokens=19_000)


def tokens(usage: RequestUsage) -> dict:
    return RequestTokens(
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
    ).as_dict()


def agent_model(
    *, fail_second: bool = False, tool=("list_channels", {})
) -> FunctionModel:
    """The guild agent: its compaction note, then a tool call and a reply."""

    def respond(messages, info: AgentInfo) -> ModelResponse:
        if not info.function_tools:
            return ModelResponse(parts=[TextPart("earlier: kai asked")], usage=NOTE)
        if (
            not any(
                isinstance(part, ToolCallPart)
                for message in messages
                if isinstance(message, ModelResponse)
                for part in message.parts
            )
            or isinstance(messages[-1], ModelRequest)
            and not any(
                getattr(part, "part_kind", "") == "tool-return"
                for part in messages[-1].parts
            )
        ):
            return ModelResponse(parts=[ToolCallPart(*tool)], usage=FIRST)
        if fail_second:
            raise ModelHTTPError(500, AGENT)
        return ModelResponse(parts=[TextPart("stayed quiet")], usage=SECOND)

    return FunctionModel(respond, model_name=AGENT)


def long_history() -> list:
    history = []
    for n in range(12):
        history.append(ModelRequest(parts=[UserPromptPart(f"[id={n}] kai: hello {n}")]))
        history.append(ModelResponse(parts=[TextPart(f"noted {n}")]))
    return history


@pytest.fixture
def wake(monkeypatch):
    service = _StubSettingsService(addendum="")
    runtime = proactive.ProactiveRuntime(_fake_bot([], service), start_consumers=False)
    monkeypatch.setattr(proactive, "runtime", runtime)
    monkeypatch.setattr(runtime, "history_store", lambda: None)
    runtime._agent_model_id = AGENT

    def start(model: FunctionModel):
        monkeypatch.setattr(proactive, "build_twopass_model", lambda model_id: model)
        state = runtime.guild_state_for(2)
        runner = runtime.agent_runner_for(state)
        runner.history = long_history()
        runner.token_limit = 1  # compact before this wake's own turn
        state.queue.push(
            Notification(
                kind="mention",
                created_at=proactive.datetime.now(proactive.UTC),
                body="kai mentioned you",
                channel_id="1",
                channel_name="general",
                message_ids=("555",),
                wakes=True,
            )
        )
        return state

    return start, service


async def test_a_wake_bills_its_compaction_and_each_request(wake):
    start, service = wake
    state = start(agent_model())

    await proactive._consume_guild_once(state)

    [report] = [r for r in service.recorded_usage if r["entries"]]
    [entry] = report["entries"]
    assert entry["model_id"] == AGENT and entry["operation"] == "agent"
    # The compaction note the wake wrote is billed with it, and every request
    # keeps its own counts.
    assert sorted(entry["requests"], key=str) == sorted(
        [tokens(NOTE), tokens(FIRST), tokens(SECOND)], key=str
    )
    assert entry["input_tokens"] == sum(r.input_tokens for r in (NOTE, FIRST, SECOND))
    assert entry["cache_write_tokens"] == FIRST.cache_write_tokens
    assert state.agent_runner.unreported_usage == {}


async def test_a_wake_that_fails_still_bills_what_the_provider_charged(wake):
    start, service = wake
    state = start(agent_model(fail_second=True))

    with pytest.raises(ModelHTTPError):
        await proactive._consume_guild_once(state)

    [report] = service.recorded_usage
    [entry] = report["entries"]
    assert sorted(entry["requests"], key=str) == sorted(
        [tokens(NOTE), tokens(FIRST)], key=str
    )
    assert report["responses"] == 0
    assert state.agent_runner.unreported_usage == {}


async def test_the_idle_fold_bills_its_note(wake, monkeypatch):
    start, service = wake
    state = start(agent_model())
    history = long_history()

    class Store:
        async def guild_idle_state(self, guild_id):
            return 0.0, False

        async def read_guild_raw(self, guild_id):
            return b"raw"

        async def read_guild(self, guild_id):
            return history

        async def write_guild_idle_compacted(self, guild_id, compacted):
            pass

    assert (
        await proactive._compact_idle_guild(proactive.runtime, Store(), 2) == "folded"
    )

    [report] = service.recorded_usage
    assert report["channel_id"] == "guild-wide" and report["passive"] is True
    [entry] = report["entries"]
    assert entry["requests"] == [tokens(NOTE)]
    assert state.agent_runner.unreported_usage == {}


@pytest.fixture
def usage_endpoint(db_session):
    original = list(proactive_settings_module.BOT_API_GUARDS)
    proactive_settings_module.BOT_API_GUARDS.clear()
    try:
        with create_test_client(
            route_handlers=[
                proactive_settings_module.ProactiveChannelSettingsController
            ],
            plugins=[PydanticPlugin()],
            dependencies={
                "db_session": Provide(lambda: db_session, sync_to_thread=False)
            },
        ) as client:
            yield client
    finally:
        proactive_settings_module.BOT_API_GUARDS[:] = original


def _post(client, entries, wake_id="w1"):
    return client.post(
        "/api/guilds/2/channels/guild-wide/proactive-settings/usage",
        json={
            "wake_id": wake_id,
            "metered_at": "2026-10-18T12:00:00+00:00",
            "passive": False,
            "responses": 1,
            "entries": entries,
        },
    )


async def test_the_ledger_prices_a_reported_wake_per_request(
    wake, usage_endpoint, db_session
):
    start, service = wake
    await proactive._consume_guild_once(start(agent_model()))
    [report] = [r for r in service.recorded_usage if r["entries"]]

    response = _post(usage_endpoint, report["entries"])

    assert response.status_code == 200, response.text
    [row] = (await db_session.execute(select(UsageCostRow))).scalars().all()
    [entry] = report["entries"]
    assert (row.product_mode, row.operation_type) == ("discord", "proactive-agent")
    assert row.model_id == AGENT and row.provider_key == "anthropic"
    assert row.input_tokens == entry["input_tokens"]
    assert row.details["requests"] == 3
    assert row.cost_usd == calc_cost_by_request(
        AGENT,
        RequestTokens(*(entry[k] for k in RequestTokens._fields)),
        [RequestTokens(**r) for r in entry["requests"]],
    )


async def test_haiku_requests_over_the_tier_bill_5x_and_totals_stay_base(
    usage_endpoint, db_session
):
    # Two Haiku 5.5 requests of 60K each: neither reaches the 100K tier,
    # though their 120K total would. And one request of 150K, which does.
    under = {"input_tokens": 60_000, "output_tokens": 100}
    over = {"input_tokens": 150_000, "output_tokens": 100}

    def entry(requests, model="claude-haiku-5-5"):
        return {
            "model_id": model,
            "operation": "agent",
            "input_tokens": sum(r["input_tokens"] for r in requests),
            "output_tokens": sum(r["output_tokens"] for r in requests),
            "requests": requests,
        }

    assert _post(usage_endpoint, [entry([under, under])], "split").status_code == 200
    assert _post(usage_endpoint, [entry([over])], "long").status_code == 200
    old = entry([over])
    del old["requests"]  # a worker from before #211
    assert _post(usage_endpoint, [old], "old").status_code == 200

    rows = {
        row.operation_key.split(":")[1]: row.cost_usd
        for row in (await db_session.execute(select(UsageCostRow))).scalars()
    }
    base = calc_cost(150_000, 100, "claude-haiku-5-5")
    assert rows["long"] == base * 5
    assert rows["old"] == base
    assert rows["split"] == calc_cost(120_000, 200, "claude-haiku-5-5")
    assert rows["split"] < calc_cost(120_000, 200, "claude-haiku-5-5", per_request=True)
    assert rows["long"] > Decimal("0")


async def test_a_jev_judgment_that_fails_to_parse_still_bills_its_request():
    from smarter_dev.bot.proactive.watcher import JevWatcherRunner

    billed = RequestUsage(input_tokens=4_000)

    def respond(messages, info):
        # Text where the judgments' output tool is expected: the run raises.
        return ModelResponse(parts=[TextPart("not judgments")], usage=billed)

    runner = JevWatcherRunner(FunctionModel(respond, model_name="jev-1.13.0"))
    decision, usage = await runner.decide(
        instructions="policy",
        context_transcript="context",
        new_transcript="new",
        bot_user_id="B1",
        new_message_ids=["m1"],
    )

    assert decision.wake is False
    assert decision.details()["classifier"]["abstained"] is True
    assert usage["input_tokens"] == 4_000
    assert usage["requests"] == [tokens(billed)]


def test_watcher_usage_with_requests_crosses_to_the_worker():
    import json
    from pathlib import Path

    import jsonschema

    from smarter_dev.bot.proactive.contracts import NotificationEnvelope
    from smarter_dev.bot.proactive.contracts import TokenUsage

    usage = {
        "input_tokens": 3,
        "output_tokens": 1,
        "cache_read_tokens": 0,
        "requests": [tokens(RequestUsage(input_tokens=3, output_tokens=1))],
    }
    envelope = NotificationEnvelope.from_notification(
        Notification(
            kind="mention",
            created_at=proactive.datetime.now(proactive.UTC),
            body="hi",
            channel_id="1",
            channel_name="general",
            message_ids=(),
            wakes=True,
        ),
        guild_id="2",
        watcher_usage={"typesafe:jev-1.13.0": usage},
    )
    schema = json.loads(
        (
            Path(__file__).parents[2]
            / "contracts/proactive/v1/notification.schema.json"
        ).read_text()
    )
    jsonschema.validate(envelope.model_dump(mode="json"), schema)
    assert TokenUsage(input_tokens=1).requests is None
    assert TokenUsage(input_tokens=3, cache_write_tokens=2).cache_write_tokens == 2
    with pytest.raises(ValueError):
        TokenUsage(input_tokens=1, cache_creation_tokens=2)


async def test_a_wake_whose_reply_fails_to_send_still_bills(wake, monkeypatch):
    start, service = wake
    state = start(
        agent_model(tool=("send_channel_message", {"channel_id": "1", "content": "hi"}))
    )

    async def discord_down(*args, **kwargs):
        raise RuntimeError("discord down")

    monkeypatch.setattr(proactive, "dispatch_response", discord_down)
    with pytest.raises(RuntimeError, match="discord down"):
        await proactive._consume_guild_once(state)

    [report] = [r for r in service.recorded_usage if r["entries"]]
    [entry] = report["entries"]
    assert len(entry["requests"]) == 3  # the note and both agent requests
    assert report["responses"] == 0


# Astra's #211 reproduction: the compaction note comes back thinking only,
# cut off at its output limit. The provider billed it; the run raises.
CUT_OFF = RequestUsage(input_tokens=90_000, output_tokens=4_096)
SKIMMED = RequestUsage(input_tokens=100, output_tokens=10)


def cut_off_note_model() -> FunctionModel:
    """The guild agent whose compaction note is cut off; its wake is fine."""
    wake_model = agent_model()

    def respond(messages, info: AgentInfo) -> ModelResponse:
        if not info.function_tools:
            return ModelResponse(
                parts=[ThinkingPart("still thinking")],
                usage=CUT_OFF,
                finish_reason="length",
            )
        return wake_model.function(messages, info)

    return FunctionModel(respond, model_name=AGENT)


def skim_model(*, cut_off: bool = False) -> FunctionModel:
    def respond(messages, info: AgentInfo) -> ModelResponse:
        if cut_off:
            return ModelResponse(
                parts=[ThinkingPart("hm")], usage=SKIMMED, finish_reason="length"
            )
        return ModelResponse(parts=[TextPart("skimmed note")], usage=SKIMMED)

    return FunctionModel(respond, model_name="glm-5.5")


async def test_a_cut_off_compaction_note_is_billed_beside_its_skim_fallback(wake):
    start, service = wake
    state = start(cut_off_note_model())
    proactive.runtime._skim = SkimRunner(skim_model())

    await proactive._consume_guild_once(state)

    [report] = [r for r in service.recorded_usage if r["entries"]]
    entries = {entry["model_id"]: entry for entry in report["entries"]}
    assert sorted(entries[AGENT]["requests"], key=str) == sorted(
        [tokens(CUT_OFF), tokens(FIRST), tokens(SECOND)], key=str
    )
    assert entries[AGENT]["output_tokens"] == sum(
        r.output_tokens for r in (CUT_OFF, FIRST, SECOND)
    )
    assert entries[proactive.runtime.skim_model_id]["requests"] == [tokens(SKIMMED)]
    assert state.agent_runner.unreported_usage == {}


async def test_a_compaction_that_fails_on_both_models_bills_both(wake):
    start, service = wake
    state = start(cut_off_note_model())
    proactive.runtime._skim = SkimRunner(skim_model(cut_off=True))

    await proactive._consume_guild_once(state)

    [report] = [r for r in service.recorded_usage if r["entries"]]
    entries = {entry["model_id"]: entry for entry in report["entries"]}
    assert tokens(CUT_OFF) in entries[AGENT]["requests"]
    assert entries[proactive.runtime.skim_model_id]["requests"] == [tokens(SKIMMED)]
    assert entries[proactive.runtime.skim_model_id]["input_tokens"] == 100


async def test_a_skim_that_writes_the_cache_is_priced_per_request(
    usage_endpoint, db_session
):
    # A Claude skim: one request over Haiku's 100K tier that writes the cache.
    # The totals must hold the write, or the ledger cannot trust the requests
    # and prices the whole at the base rate.
    haiku = "claude-haiku-5-5"
    billed = RequestUsage(
        input_tokens=150_000,
        output_tokens=100,
        cache_read_tokens=20_000,
        cache_write_tokens=10_000,
    )
    runner = SkimRunner(
        FunctionModel(
            lambda messages, info: ModelResponse(
                parts=[TextPart("skim")], usage=billed
            ),
            model_name=haiku,
        )
    )
    _, usage = await runner.skim("transcript")
    usage_by_model: dict = {}
    proactive.merge_usage(usage_by_model, haiku, usage)
    service = _StubSettingsService(addendum="")
    await proactive._record_usage(
        service,
        guild_id="2",
        channel_id="guild-wide",
        metered_at=proactive.datetime.now(proactive.UTC),
        passive=False,
        responses=0,
        operation="watcher",
        usage_by_model=usage_by_model,
    )
    [report] = service.recorded_usage

    assert _post(usage_endpoint, report["entries"]).status_code == 200

    [row] = (await db_session.execute(select(UsageCostRow))).scalars().all()
    assert row.cache_write_tokens == 10_000
    request = RequestTokens(150_000, 100, 20_000, 10_000)
    assert row.cost_usd == calc_cost_by_request(haiku, request, [request])
    assert row.cost_usd == Decimal("0.07125")


async def test_a_skim_tool_that_fails_mid_wake_bills_its_request(wake):
    from tests.bot.proactive_plugin_test import _FakeIterator
    from tests.bot.proactive_plugin_test import _hikari_message

    start, service = wake
    state = start(
        agent_model(
            tool=("skim_messages", {"channel_id": "1", "around_message_id": "555"})
        )
    )
    state.agent_runner.token_limit = 10**9  # no compaction this time
    proactive.runtime.bot.rest.fetch_messages = lambda channel_id, before=None: (
        _FakeIterator([_hikari_message()])
    )
    proactive.runtime._skim = SkimRunner(skim_model(cut_off=True))

    await proactive._consume_guild_once(state)

    [report] = [r for r in service.recorded_usage if r["entries"]]
    entries = {entry["model_id"]: entry for entry in report["entries"]}
    # The tool's error went back to the agent and the wake went on; the skim
    # the provider billed is reported with it.
    assert entries[proactive.runtime.skim_model_id]["requests"] == [tokens(SKIMMED)]
    assert sorted(entries[AGENT]["requests"], key=str) == sorted(
        [tokens(FIRST), tokens(SECOND)], key=str
    )
