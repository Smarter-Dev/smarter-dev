"""The memory dream's and the memory purge's model usage reaches the ledger.

Both run the real agents (the real output validators) on a FunctionModel that
reports known per-request usage, through the real job functions, into SQLite
``usage_cost_rows``. A night that keeps the previous blocks, a run whose
validator gives up and a purge pass that fails are all billed, so all are
recorded. Synthetic members only: kai (111…) is purged, nia (222…) is not.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select

from smarter_dev.shared.request_usage import RequestTokens
from smarter_dev.web import chat_memory_dream
from smarter_dev.web.chat_bot_purge import STATUS_AWAITING_ACKS
from smarter_dev.web.chat_bot_purge import STATUS_COMPLETE
from smarter_dev.web.chat_memory_dream import DREAM_OUTPUT_RETRIES
from smarter_dev.web.chat_memory_dream import DreamOutcome
from smarter_dev.web.chat_memory_dream import dream_cutoff
from smarter_dev.web.chat_memory_dream import dream_day
from smarter_dev.web.chat_memory_dream import run_dream_session
from smarter_dev.web.chat_memory_purge import PURGE_OUTPUT_RETRIES
from smarter_dev.web.chat_memory_purge import build_purge_agent
from smarter_dev.web.llm_pricing import calc_cost_by_request
from smarter_dev.web.models import ChatAgentMemoryNote
from smarter_dev.web.models import UsageCostRow
from tests.web.chat_bot_purge_test import _GUILD as _PURGE_GUILD
from tests.web.chat_bot_purge_test import _KAI_ID
from tests.web.chat_bot_purge_test import _NOW
from tests.web.chat_bot_purge_test import KAI_LINE
from tests.web.chat_bot_purge_test import NIA_LINE
from tests.web.chat_bot_purge_test import _ack_all
from tests.web.chat_bot_purge_test import _check
from tests.web.chat_bot_purge_test import _enforce
from tests.web.chat_bot_purge_test import _open
from tests.web.chat_bot_purge_test import _run
from tests.web.chat_bot_purge_test import _seed_memory
from tests.web.chat_bot_purge_test import redis  # noqa: F401 — fixture
from tests.web.chat_bot_purge_test import session_factory  # noqa: F401 — fixture
from tests.web.chat_memory_dream_test import _DREAMED_AT
from tests.web.chat_memory_dream_test import _GUILD
from tests.web.chat_memory_dream_test import _KAI_NOTE
from tests.web.chat_memory_dream_test import _seed_blob
from tests.web.chat_memory_dream_test import _session_factory_for
from tests.web.chat_memory_dream_test import _write_note

DREAM_MODEL = "gpt-6.1-sol"
GOOD_DREAM = {
    "memory": "## People & Relationships\nkai (id 7) is deep in embedded rust."
}


class _Answers:
    """A FunctionModel giving each request the next scripted answer and usage.

    ``billed`` is what the provider would have charged for: every response it
    actually sent, in order.
    """

    def __init__(self, answers, *, name: str):
        self.answers = list(answers)
        self.billed: list[RequestTokens] = []
        self.model = FunctionModel(self._respond, model_name=name)

    def _respond(self, messages, info: AgentInfo) -> ModelResponse:
        args, usage = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(args):
            args = args(messages)
        self.billed.append(RequestTokens(usage.input_tokens, usage.output_tokens))
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, args)], usage=usage
        )

    def total(self) -> RequestTokens:
        return RequestTokens(*(sum(column) for column in zip(*self.billed)))


def _usage(input_tokens: int, output_tokens: int) -> RequestUsage:
    return RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens)


async def _rows(session, operation_type: str) -> list[UsageCostRow]:
    session.expire_all()
    return list(
        (
            await session.scalars(
                select(UsageCostRow)
                .where(UsageCostRow.operation_type == operation_type)
                .order_by(UsageCostRow.metered_at)
            )
        ).all()
    )


def _assert_priced(row: UsageCostRow, model: str, answers: _Answers) -> None:
    total = answers.total()
    assert (row.input_tokens, row.output_tokens) == (
        total.input_tokens,
        total.output_tokens,
    )
    assert row.cost_usd == calc_cost_by_request(model, total, answers.billed)
    assert row.details["requests"] == len(answers.billed)


# -- the dream -----------------------------------------------------------------


@pytest.fixture
def dream_model(monkeypatch):
    """Build the real dream agent (prompt, validator, retries) on ``answers``."""

    def use(answers: _Answers) -> None:
        monkeypatch.setattr(chat_memory_dream, "_dream_agent", None)
        monkeypatch.setattr(
            chat_memory_dream, "build_model_for", lambda _model: answers.model
        )

    return use


async def test_a_dreamed_night_records_each_request_priced_on_its_own(
    db_session, dream_model
):
    await _write_note(db_session, content=_KAI_NOTE)
    # The first answer is malformed and sent back; both were billed. The first
    # request is past gpt-6.1-sol's 272k long-context line on its own.
    answers = _Answers(
        [({}, _usage(300_000, 40)), (GOOD_DREAM, _usage(2_000, 300))], name=DREAM_MODEL
    )
    dream_model(answers)

    summary = await run_dream_session(
        _DREAMED_AT, session_factory=_session_factory_for(db_session)
    )

    assert [result.outcome for result in summary.results] == [DreamOutcome.DREAMED]
    [row] = await _rows(db_session, "memory_dream")
    assert row.product_mode == "discord"
    assert (row.provider_key, row.model_id) == ("openai", DREAM_MODEL)
    assert row.reasoning_level == "high"
    assert (
        row.user_id is None
        and row.discord_user_id is None
        and row.conversation_id is None
    )
    day = dream_day(dream_cutoff(_DREAMED_AT)).isoformat()
    assert row.operation_key == (
        f"memory_dream:{_GUILD}:{day}:{_DREAMED_AT.isoformat()}:{DREAM_MODEL}"
    )
    assert row.details["guild_id"] == _GUILD
    assert len(answers.billed) >= 2
    _assert_priced(row, DREAM_MODEL, answers)
    # Priced per request, the long-context request is dearer than the total
    # priced at the base rate.
    assert row.cost_usd > calc_cost_by_request(DREAM_MODEL, answers.total())


async def test_a_night_that_keeps_the_previous_blob_is_still_recorded(
    db_session, dream_model
):
    await _seed_blob(
        db_session, content="## People & Relationships\n" + "kai (id 7) rust. " * 30
    )
    await _write_note(db_session, content=_KAI_NOTE)
    answers = _Answers(
        [({"memory": "## People\nok."}, _usage(5_000, 20))], name=DREAM_MODEL
    )
    dream_model(answers)

    summary = await run_dream_session(
        _DREAMED_AT, session_factory=_session_factory_for(db_session)
    )

    assert [result.outcome for result in summary.results] == [
        DreamOutcome.KEPT_PREVIOUS
    ]
    [row] = await _rows(db_session, "memory_dream")
    _assert_priced(row, DREAM_MODEL, answers)


async def test_a_dream_whose_validator_gives_up_is_recorded_and_a_rerun_is_new_spend(
    db_session, dream_model
):
    await _write_note(db_session, content=_KAI_NOTE)
    failing = _Answers([({}, _usage(4_000, 30))], name=DREAM_MODEL)
    dream_model(failing)
    factory = _session_factory_for(db_session)

    summary = await run_dream_session(_DREAMED_AT, session_factory=factory)

    assert summary.failed_guild_ids == [_GUILD]
    # The notes survived the rollback; the usage did not roll back with them.
    assert len((await db_session.scalars(select(ChatAgentMemoryNote.id))).all()) == 1
    [row] = await _rows(db_session, "memory_dream")
    assert len(failing.billed) == DREAM_OUTPUT_RETRIES + 1
    _assert_priced(row, DREAM_MODEL, failing)

    # The CronJob run again that night is a second bill: a second row.
    working = _Answers([(GOOD_DREAM, _usage(1_000, 100))], name=DREAM_MODEL)
    dream_model(working)
    later = _DREAMED_AT + timedelta(minutes=30)
    summary = await run_dream_session(later, session_factory=factory)

    assert [result.outcome for result in summary.results] == [DreamOutcome.DREAMED]
    first, second = await _rows(db_session, "memory_dream")
    assert first.operation_key != second.operation_key
    _assert_priced(second, DREAM_MODEL, working)


# -- the purge -----------------------------------------------------------------


_SEGMENT = re.compile(r"^\[([^\]]+)\] ", re.MULTILINE)


def _remove_every_listed_segment(messages) -> dict:
    prompt = next(
        part.content
        for message in messages
        for part in message.parts
        if part.part_kind == "user-prompt"
    )
    decide = prompt.split("# Segments to decide", 1)[1].split("\n# ", 1)[0]
    return {"edits": [{"id": i, "action": "remove"} for i in _SEGMENT.findall(decide)]}


def _purge_agent(answers: _Answers):
    return build_purge_agent(answers.model)


async def test_a_purge_records_both_passes_as_the_bots_own_spend(
    db_session,
    session_factory,  # noqa: F811 — fixture
    redis,  # noqa: F811 — fixture
):
    from smarter_dev.web.crud import create_memory_note

    await _seed_memory(db_session)
    request = await _open(db_session)
    request_id, run_id = request.id, request.run_id
    await _enforce(redis, 1)
    memory_pass = _Answers(
        [(_remove_every_listed_segment, _usage(150_000, 60))], name=DREAM_MODEL
    )

    status = await _run(
        session_factory, redis, request_id, run_id, _purge_agent(memory_pass)
    )

    assert status == STATUS_AWAITING_ACKS
    [row] = await _rows(db_session, "memory_purge")
    assert row.product_mode == "discord"
    assert (row.provider_key, row.model_id) == ("openai", DREAM_MODEL)
    # Never the forgotten person's: no user on the row, their id nowhere in it.
    assert row.user_id is None and row.discord_user_id is None
    assert _KAI_ID not in json.dumps(row.details) and _KAI_ID not in row.operation_key
    assert row.details["request_id"] == str(request_id)
    assert row.details["run_id"] == str(run_id)
    assert row.details["guild_id"] == _PURGE_GUILD
    assert row.details["pass"] == "memory"
    _assert_priced(row, DREAM_MODEL, memory_pass)

    async with session_factory() as session:
        await create_memory_note(
            session,
            guild_id=_PURGE_GUILD,
            channel_id="555000111222333444",
            content="kai is back on embedded rust.",
            created_at=_NOW + timedelta(minutes=1),
            day_start=_NOW,
        )
        await session.commit()
    await _ack_all(session_factory, run_id, [_PURGE_GUILD])
    notes_pass = _Answers(
        [(_remove_every_listed_segment, _usage(900, 10))], name=DREAM_MODEL
    )

    assert await _check(
        session_factory, redis, request_id, _purge_agent(notes_pass)
    ) == (STATUS_COMPLETE)
    _, final = await _rows(db_session, "memory_purge")
    assert final.details["pass"] == "final_notes"
    _assert_priced(final, DREAM_MODEL, notes_pass)


async def test_a_purge_pass_that_fails_after_billed_answers_is_recorded(
    db_session,
    session_factory,  # noqa: F811 — fixture
    redis,  # noqa: F811 — fixture
):
    await _seed_memory(db_session)
    request = await _open(db_session)
    await _enforce(redis, 1)
    # Never removes kai: the validator sends it back until it gives up.
    refused = _Answers([({"edits": []}, _usage(3_000, 25))], name=DREAM_MODEL)

    await _run(
        session_factory, redis, request.id, request.run_id, _purge_agent(refused)
    )

    async with session_factory() as session:
        from smarter_dev.web.crud import get_guild_memory_blob

        memory = await get_guild_memory_blob(session, _PURGE_GUILD)
        assert KAI_LINE in memory.content and NIA_LINE in memory.content
    [row] = await _rows(db_session, "memory_purge")
    assert len(refused.billed) == PURGE_OUTPUT_RETRIES + 1
    _assert_priced(row, DREAM_MODEL, refused)
