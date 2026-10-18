"""Claude Haiku 5.5's long-context tier is priced per provider request (#199).

Over 100,000 prompt tokens one request bills 5x every rate, so a run summed
across several requests cannot price it: one 100,001-token request bills
$0.0525005, while three 40,000-token requests (120,000 in all) stay at the
base rate. Every ledger path that meters a multi-request run keeps each
response's own counts and prices them one by one.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.usage import RequestUsage
from skrift.agents.models import AgentUsageRecord
from skrift.agents.models import AgentUsageTotals
from skrift.agents.models import RunState

from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.shared.request_usage import response_request_tokens
from smarter_dev.web.llm_pricing import calc_cost_by_request

HAIKU = "anthropic:claude-haiku-5-5"
LONG = RequestTokens(100_001, 1_000)
SHORT = RequestTokens(40_000, 500)
# $0.50 in and $2.50 out per million: the whole request at 5x.
LONG_COST = Decimal("0.0525005")
# $0.10 in and $0.50 out per million, three times.
THREE_SHORT_COST = Decimal("0.01275")


def _total(*requests: RequestTokens) -> RequestTokens:
    return RequestTokens(*map(sum, zip(*requests)))


def _response(tokens: RequestTokens, model: str = "claude-haiku-5-5") -> ModelResponse:
    return ModelResponse(
        parts=[TextPart("ok")],
        usage=RequestUsage(**tokens.as_dict()),
        model_name=model,
    )


def _json(*messages) -> list[dict]:
    return ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")


# -- the pricing rule ----------------------------------------------------------


def test_one_long_request_bills_the_tier():
    assert calc_cost_by_request(HAIKU, LONG, [LONG]) == LONG_COST
    # The same total with no per-request counts stays at the base rate.
    assert calc_cost_by_request(HAIKU, LONG) == Decimal("0.0105001")


def test_short_requests_over_100k_together_stay_at_the_base_rate():
    total = _total(SHORT, SHORT, SHORT)
    assert total.input_tokens > 100_000
    assert calc_cost_by_request(HAIKU, total, [SHORT] * 3) == THREE_SHORT_COST


def test_cache_tokens_bill_at_the_tier_too():
    # 100,001 prompt tokens of which 90,000 read from cache and 10,000 written.
    request = RequestTokens(100_001, 0, 90_000, 10_000)
    # 1 uncached at $0.50, reads at $0.05, 1-hour writes at $1 per million.
    assert calc_cost_by_request(HAIKU, request, [request]) == Decimal("0.0145005")


def test_tokens_no_request_accounts_for_bill_at_the_base_rate():
    total = _total(LONG, SHORT)
    assert calc_cost_by_request(HAIKU, total, [LONG]) == LONG_COST + Decimal("0.00425")


def test_requests_that_outgrow_their_total_are_not_trusted():
    assert calc_cost_by_request(HAIKU, SHORT, [LONG]) == Decimal("0.00425")


def test_stages_are_priced_at_their_own_models_and_untrusted_ones_dropped():
    from smarter_dev.web.llm_pricing import calc_cost_by_stage

    sol = "gpt-6.1-sol"
    total = _total(LONG, SHORT, SHORT)
    staged = calc_cost_by_stage(
        HAIKU,
        total,
        [(sol, _total(SHORT, SHORT), [SHORT, SHORT]), (HAIKU, LONG, [LONG])],
    )
    assert (
        staged
        == calc_cost_by_request(sol, _total(SHORT, SHORT), [SHORT, SHORT]) + LONG_COST
    )
    # What the stages leave of the total is the turn model's, at the base rate.
    assert calc_cost_by_stage(HAIKU, total, [(HAIKU, LONG, [LONG])]) == (
        LONG_COST + calc_cost_by_request(HAIKU, _total(SHORT, SHORT))
    )
    # Stages over the total are not its parts.
    assert calc_cost_by_stage(HAIKU, SHORT, [(HAIKU, LONG, [LONG])]) == Decimal(
        "0.00425"
    )


def test_only_the_named_models_responses_count():
    messages = [
        ModelRequest(parts=[UserPromptPart("q")]),
        _response(LONG),
        _response(SHORT, model="gpt-6-luna"),
    ]
    assert response_request_tokens(messages, "anthropic:claude-haiku-5-5") == [LONG]
    assert response_request_tokens(_json(*messages)) == [LONG, SHORT]


# -- Skrift sessions (title, Resources) ---------------------------------------


def _state(turns: dict[str, list[RequestTokens]], *, steer: bool = False) -> RunState:
    messages: list[dict] = []
    usage = {}
    for turn_id, requests in turns.items():
        messages.append({"role": "user", "content": "q", "turn_id": turn_id})
        for index, tokens in enumerate(requests):
            if steer and index == 1:
                messages.append({"role": "user", "content": "steer"})
            messages.append({"role": "model", "content": _json(_response(tokens))[0]})
        total = _total(*requests)
        usage[turn_id] = AgentUsageRecord(
            session_id="s",
            turn_id=turn_id,
            agent_name="smarter.dev.resources.author",
            configured_model=HAIKU,
            requests=len(requests),
            **total.as_dict(),
        )
    return RunState(
        session_id="s",
        agent_name="smarter.dev.resources.author",
        messages=messages,
        turn_usage=usage,
        usage_totals=AgentUsageTotals(
            **_total(*(t for turn in turns.values() for t in turn)).as_dict()
        ),
    )


def test_a_skrift_turn_prices_each_stored_response():
    from smarter_dev.web.agent_session_cleanup import session_cost

    assert session_cost(_state({"t1": [LONG]})) == LONG_COST
    assert session_cost(_state({"t1": [SHORT] * 3}, steer=True)) == THREE_SHORT_COST
    # Each turn's responses are its own: a long request in one turn does not
    # lift a short one in the next.
    assert session_cost(_state({"t1": [LONG], "t2": [SHORT]})) == LONG_COST + Decimal(
        "0.00425"
    )


async def test_a_resources_stage_carries_its_per_request_cost_to_the_ledger(
    monkeypatch,
):
    from smarter_dev.web import resources_agent
    from smarter_dev.web import resources_jobs

    state = _state({"t1": [LONG]})
    sink: list[dict] = []

    async def current_state():
        return state

    await resources_agent._capture_stage_usage(
        SimpleNamespace(state=current_state), "author", sink
    )
    assert sink[0]["cost_usd"] == str(LONG_COST)

    recorded = {}

    async def record_usage(session, **row):
        recorded.update(row)

    monkeypatch.setattr(resources_jobs, "record_usage", record_usage)
    run = SimpleNamespace(
        id="run", attempt_count=1, owner_user_id=None, conversation_id=None
    )
    await resources_jobs._persist_stage_usage(None, run, sink)
    assert recorded["cost_usd"] == LONG_COST
    assert recorded["input_tokens"] == 100_001


# -- web search ------------------------------------------------------------------


def test_web_search_rows_price_each_request():
    from smarter_dev.web.web_search.metering import model_row

    run = SimpleNamespace(id="search", attempt_count=1, owner_user_id=None)

    def usage(*requests: RequestTokens) -> dict:
        return {
            "model": "claude-haiku-5-5",
            "requests": len(requests),
            **_total(*requests).as_dict(),
            "request_tokens": [tokens.as_dict() for tokens in requests],
        }

    assert model_row(run, usage(LONG), "answer").cost_usd == LONG_COST
    assert model_row(run, usage(SHORT, SHORT, SHORT)).cost_usd == THREE_SHORT_COST


# -- Discord chat and compaction -------------------------------------------------


def _turn(model_name: str, *requests: RequestTokens, delta=None):
    from smarter_dev.web.api_native.schemas import ChatAgentTurnCreate

    total = _total(*requests)
    return ChatAgentTurnCreate(
        engagement_id="00000000-0000-0000-0000-000000000001",
        request_id="r",
        turn_kind="initial",
        output_kind="send_response",
        triggering_messages=[],
        agent_output={},
        model_messages_delta=(
            _json(*(_response(tokens) for tokens in requests))
            if delta is None
            else delta
        ),
        chat_tokens_input=total.input_tokens,
        chat_tokens_output=total.output_tokens,
        chat_model_name=model_name,
    )


def test_a_discord_chat_turn_on_haiku_prices_each_request():
    from smarter_dev.web.api_native.chat_conversations import _billed_chat_model

    assert _billed_chat_model(_turn("claude-haiku-5-5", LONG)).cost_usd == LONG_COST
    assert (
        _billed_chat_model(_turn("claude-haiku-5-5", SHORT, SHORT, SHORT)).cost_usd
        == THREE_SHORT_COST
    )
    # A turn sent without its messages is priced on its totals, as before.
    assert _billed_chat_model(
        _turn("claude-haiku-5-5", LONG, delta=[])
    ).cost_usd == Decimal("0.0105001")


def test_a_discord_compaction_prices_each_summarizer_request():
    from smarter_dev.web.api_native.chat_conversations import _billed_summarizer
    from smarter_dev.web.api_native.schemas import ChatAgentCompactionEventCreate

    def billed(*requests: RequestTokens, sent: bool = True) -> Decimal:
        total = _total(*requests)
        return _billed_summarizer(
            ChatAgentCompactionEventCreate(
                event_kind="conversation",
                original_content="",
                summary="",
                original_chars=0,
                summary_chars=0,
                summarizer_tokens_input=total.input_tokens,
                summarizer_tokens_output=total.output_tokens,
                summarizer_model_name="claude-haiku-5-5",
                summarizer_request_tokens=(
                    [tokens.as_dict() for tokens in requests] if sent else None
                ),
            )
        ).cost_usd

    assert billed(LONG) == LONG_COST
    # Asked twice (say the first reply was empty): two requests at base rate,
    # not their 120,000-token total at 5x.
    two = RequestTokens(60_000, 500)
    assert billed(two, two) == Decimal("0.0125")
    # From a bot that sends no request counts: a fold is one request.
    assert billed(LONG, sent=False) == LONG_COST
