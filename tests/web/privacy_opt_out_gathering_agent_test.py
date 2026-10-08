"""A handler's gathering agent never runs for an opted-out member (#100).

Handlers keep firing for everyone: scripts are not agents. The gathering
agent a script can spawn is gated: a fire triggered by someone on the
blocked-users list gets a refusal and no model call, and any other fire's
prompt has their id redacted. Real SQLite block list, stub model. Synthetic
members only: kai (opted out) and nia.
"""

from __future__ import annotations

import contextlib

import pytest

from smarter_dev.web import handler_agent
from smarter_dev.web.chat_bot_opt_out import opt_out

KAI = 111111111111111111
NIA = 222222222222222222


async def test_the_gathering_agent_refuses_a_fire_from_an_opted_out_member(
    db_session, monkeypatch
):
    await opt_out(db_session, str(KAI))
    prompts: list[str] = []

    async def model(prompt, has_tools, budget):
        prompts.append(prompt)
        return "gathered"

    monkeypatch.setattr(handler_agent, "run_gathering_agent", model)

    @contextlib.asynccontextmanager
    async def sessions():
        yield db_session

    from_kai = handler_agent.gathering_agent_for({"author_id": str(KAI)}, sessions)
    assert await from_kai("summarise this", False, None) == handler_agent.GATHERING_REFUSED
    assert prompts == []

    # Control: a fire from nia runs, with kai's id redacted from the prompt.
    from_nia = handler_agent.gathering_agent_for({"author_id": str(NIA)}, sessions)
    assert await from_nia(f"what did <@{KAI}> and <@{NIA}> say", False, None) == "gathered"
    assert prompts == [f"what did @[blocked user] and <@{NIA}> say"]


async def test_a_thread_started_by_an_opted_out_member_gets_no_agent_either(
    db_session, monkeypatch
):
    await opt_out(db_session, str(KAI))
    monkeypatch.setattr(handler_agent, "run_gathering_agent", None)  # never called

    @contextlib.asynccontextmanager
    async def sessions():
        yield db_session

    agent = handler_agent.gathering_agent_for({"creator_id": str(KAI)}, sessions)
    assert await agent("summarise the thread", True, None) == handler_agent.GATHERING_REFUSED


@pytest.mark.parametrize("admin", [False, True], ids=["member-handler", "admin-handler"])
async def test_both_fire_jobs_hand_the_script_the_gated_agent(monkeypatch, admin):
    """Wiring: each job builds its agent from this fire's trigger context."""
    from types import SimpleNamespace
    from uuid import uuid4

    from smarter_dev.web import admin_handlers_jobs
    from smarter_dev.web import handler_runtime
    from smarter_dev.web import handlers_jobs
    from smarter_dev.web.handler_fire_payloads import AdminHandlerFirePayload
    from smarter_dev.web.handler_fire_payloads import HandlerFirePayload
    from tests.web.handlers_jobs_test import _ctx
    from tests.web.handlers_jobs_test import _FakeSessionCtx
    from tests.web.handlers_jobs_test import _ok_result

    record = SimpleNamespace(
        enabled=True,
        script="pass",
        name="h",
        channel_id="C1",
        channel_ids=["C1"],
        guild_id="G1",
        trigger_type="message",
        settings={},
        memory={},
    )
    job = admin_handlers_jobs if admin else handlers_jobs
    monkeypatch.setattr(
        job,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(job, "get_db_session_context", lambda: _FakeSessionCtx(record))
    monkeypatch.setattr(job, "get_redis_client", lambda: object())
    monkeypatch.setattr(job, "WindowedLimiter", lambda **kwargs: object())

    async def granted(*args, **kwargs):
        return True

    monkeypatch.setattr(job, "claim_fire_attempt", granted)
    built: list[dict] = []

    def spy(trigger_context, session_factory):
        built.append(trigger_context)
        return "gated-agent"

    monkeypatch.setattr(handler_agent, "gathering_agent_for", spy)
    handed: list = []

    async def run(script, context, **kwargs):
        handed.append(kwargs["agent_runner"])
        return _ok_result()

    monkeypatch.setattr(handler_runtime, "run_handler_script", run)
    context = {"author_id": str(KAI), "message_content": "hi"}

    if admin:
        await admin_handlers_jobs.run_admin_handler_fire(
            AdminHandlerFirePayload(
                admin_handler_id=str(uuid4()), channel_id="C1", trigger_context=context
            ),
            _ctx(),
        )
    else:
        await handlers_jobs.run_handler_fire(
            HandlerFirePayload(handler_id=str(uuid4()), trigger_context=context), _ctx()
        )

    assert handed == ["gated-agent"]
    assert built[0]["author_id"] == str(KAI)
