"""``claim`` and ``claimed`` as a script reaches them, and the admin fire job.

The runtime tests drive the claim functions through ``run_handler_script``
with the real claim functions over one Redis. The fire-job tests run the real
fire jobs, so nothing between a handler's script and Redis or Discord is
stood in for except the HTTP transport. (Member holds went with the handler
sanctions, #242.)
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from functools import partial
from types import SimpleNamespace
from uuid import UUID
from uuid import uuid4

import fakeredis.aioredis as fakeredis_aioredis
import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import smarter_dev.web.admin_actions as admin_actions
import smarter_dev.web.admin_handlers_jobs as admin_handlers_jobs
import smarter_dev.web.handlers_jobs as handlers_jobs
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web import moderation_policy_store as store
from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.admin_actions import HandlerFireFacts
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_caps import MAX_CLAIMS_PER_FIRE
from smarter_dev.web.handler_caps import claim_handler_key
from smarter_dev.web.handler_caps import handler_key_claimed
from smarter_dev.web.handler_runtime import run_handler_script
from smarter_dev.web.handlers_jobs import HandlerFirePayload
from smarter_dev.web.models import AdminHandler
from smarter_dev.web.models import ChannelHandler
from smarter_dev.web.models import ModerationFilterConfig
from tests.moderation_fakes import FakeModerationDiscord


@dataclass
class _Emitter:
    messages: list = field(default_factory=list)

    async def create_message(
        self, channel_id, content, ping_role_id=None, tolerate_missing_target=False
    ):
        self.messages.append(content)
        return f"msg{len(self.messages)}"


@dataclass
class _Limiter:
    async def hit(self, key, limit, window_seconds=None):
        return True


def _actor(discord: FakeModerationDiscord) -> AdminActor:
    return AdminActor(
        bot_token="t",
        guild_id="G1",
        staff_exempt_role_ids=(),
        moderation_port=discord,
        fire=HandlerFireFacts(handler_id="H1", handler_name="probe"),
    )


@pytest.fixture
def redis():
    return fakeredis_aioredis.FakeRedis(decode_responses=True)


async def _run(script, redis, *, budget=None, wired=True):
    """Run ``script`` as an admin fire of handler H1; return (result, said)."""
    emitter = _Emitter()
    actor = _actor(FakeModerationDiscord(guild_id="G1"))
    kwargs = {}
    if wired:
        kwargs = {
            "claimer": partial(claim_handler_key, redis, "H1"),
            "claim_reader": partial(handler_key_claimed, redis, "H1"),
        }
    result = await run_handler_script(
        script,
        {"trigger_type": "message", "author_id": "U1"},
        channel_id="C1",
        guild_id="G1",
        emitter=emitter,
        limiter=_Limiter(),
        budget=budget or admin_budget("message"),
        actor=actor,
        handler_id="H1",
        **kwargs,
    )
    return result, emitter.messages


# -- claimed ---------------------------------------------------------------------


async def test_claimed_reads_a_claim_without_taking_it(redis):
    script = (
        'before = [await claimed("bad:k"), await claimed("bad:k")]\n'
        'took = await claim("bad:k", 60)\n'
        'await send_message(str(before + [took, await claimed("bad:k")]))\n'
    )

    result, said = await _run(script, redis)

    assert result.outcome == "ok", result.error
    # Asking twice did not take it: the claim after still won.
    assert said == ["[False, False, True, True]"]


async def test_claimed_sees_another_fires_claim_and_not_another_handlers(redis):
    await claim_handler_key(redis, "H1", "bad:ours", 60)
    await claim_handler_key(redis, "H2", "bad:theirs", 60)
    script = (
        'await send_message(str([await claimed("bad:ours"), '
        'await claimed("bad:theirs")]))\n'
    )

    _, said = await _run(script, redis)

    assert said == ["[True, False]"]


async def test_claimed_stops_being_true_when_the_claim_expires(redis):
    await claim_handler_key(redis, "H1", "bad:k", 60)
    await redis.delete("hclaim:H1:bad:k")

    _, said = await _run('await send_message(str(await claimed("bad:k")))\n', redis)

    assert said == ["False"]


async def test_claimed_counts_against_the_claims_per_fire_cap(redis):
    script = f'for i in range({MAX_CLAIMS_PER_FIRE + 1}):\n    await claimed("k")\n'

    result, _ = await _run(script, redis)

    assert result.outcome == "cap_exceeded"
    assert result.cap == "claims_per_fire"


@pytest.mark.parametrize("key", ['""', "123", '"k" * 129'])
async def test_claimed_rejects_a_bad_key(redis, key):
    result, _ = await _run(f"await claimed({key})\n", redis)

    assert result.outcome == "error"


async def test_claimed_not_configured_errors_loudly(redis):
    result, _ = await _run('await claimed("k")\n', redis, wired=False)

    assert result.outcome == "error"


# -- through the real fire jobs --------------------------------------------------


class _SessionCtx:
    """``get_db_session_context()`` over the test engine."""

    def __init__(self, engine):
        self._maker = async_sessionmaker(engine, expire_on_commit=False)

    def __call__(self):
        return self

    async def __aenter__(self):
        self._session = self._maker()
        return self._session

    async def __aexit__(self, *exc):
        await self._session.close()
        return False


def _job() -> SimpleNamespace:
    return SimpleNamespace(job=SimpleNamespace(id=uuid4().hex))


async def test_admin_fire_job_gives_a_script_claims_and_no_sanctions(
    monkeypatch, test_engine, redis
):
    member: dict = {"requests": []}

    def discord_api(request: httpx.Request) -> httpx.Response:
        member["requests"].append((request.method, request.url.path))
        return httpx.Response(200, json={"user": {"id": "U7"}})

    async def drop_event(event, **kwargs) -> None:
        return None

    handler_id = uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        session.add(
            AdminHandler(
                id=handler_id,
                guild_id="G1",
                name="claim-probe",
                trigger_type="message",
                settings={},
                channel_ids=[],
                description="d",
                script=(
                    "seen = [await claimed('bad:k'), await claim('bad:k', 60), "
                    "await claimed('bad:k')]\n"
                    "await memory_set('seen', seen)\n"
                    # The removed sanctions are not there to call: the fire
                    # errors on the name, after the memory above was set.
                    "await hold_member(context['author_id'], 'post-a', 300)\n"
                ),
                created_by_admin="A1",
            )
        )
        await session.commit()

    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", _SessionCtx(test_engine)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: redis)
    # The job builds its own actor; only its HTTP transport is swapped.
    monkeypatch.setattr(
        admin_actions,
        "AdminActor",
        partial(AdminActor, transport=httpx.MockTransport(discord_api)),
    )
    from smarter_dev.web import guild_event_recorder
    from smarter_dev.web import handler_dispatch

    monkeypatch.setattr(guild_event_recorder, "record_guild_event", drop_event)
    monkeypatch.setattr(handler_dispatch, "dispatch_handler_event", drop_event)
    # The fire errors on purpose; its error notice would post to Discord.
    notices: list = []

    async def capture_notice(*args, **kwargs) -> None:
        notices.append(kwargs)

    monkeypatch.setattr(admin_handlers_jobs, "notify_handler_error", capture_notice)

    outcome = await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(handler_id),
            channel_id="C2",
            trigger_context={"trigger_type": "message", "author_id": "U7"},
        ),
        _job(),
    )

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        handler = await session.get(AdminHandler, handler_id)
    assert len(notices) == 1
    # Not claimed, claimed by this call, claimed.
    assert handler.memory["seen"] == [False, True, True]
    assert await redis.exists(f"hclaim:{handler_id}:bad:k") == 1
    # hold_member is gone: the fire errored, and nothing reached the member.
    assert outcome["status"] == "error"
    assert not [r for r in member["requests"] if r[0] == "PATCH"]
    assert await redis.keys("hhold:*") == []


async def test_admin_fire_job_without_a_policy_hands_the_actor_no_extra_roles(
    monkeypatch, test_engine, redis
):
    """#255/#242: the fire's actor is handed the guild's extra staff roles,
    read (an unread list refuses everything); without a moderation policy
    there are none: the old ``staff_exempt_role_ids`` column is gone, and
    the built-in staff rule still protects."""
    handler_id = uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        session.add(
            AdminHandler(
                id=handler_id,
                guild_id="G1",
                name="role-probe",
                trigger_type="message",
                settings={},
                channel_ids=[],
                description="d",
                script="x = 1\n",
                created_by_admin="A1",
            )
        )
        session.add(ModerationFilterConfig(guild_id="G1", content_filter_enabled=True))
        await session.commit()

    built = []

    def actor(**kwargs):
        built.append(kwargs)
        return AdminActor(**kwargs)

    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", _SessionCtx(test_engine)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: redis)
    monkeypatch.setattr(admin_actions, "AdminActor", actor)

    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(handler_id),
            channel_id="C2",
            trigger_context={"trigger_type": "message", "author_id": "U7"},
        ),
        _job(),
    )

    assert [kwargs["staff_exempt_role_ids"] for kwargs in built] == [()]


#: Discord-shaped: the policy accepts only snowflakes.
POLICY_ROLE_A = "1100000000000000001"
POLICY_ROLE_B = "1100000000000000002"


async def test_admin_fire_job_under_a_policy_hands_the_actor_its_protected_roles(
    monkeypatch, test_engine, redis
):
    """#241: a guild's moderation policy's protected roles are the staff
    roles."""
    handler_id = uuid4()
    document = mp.default_document().as_json()
    document["protected_role_ids"] = [POLICY_ROLE_A, POLICY_ROLE_B]
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        session.add(
            AdminHandler(
                id=handler_id,
                guild_id="G1",
                name="role-probe",
                trigger_type="message",
                settings={},
                channel_ids=[],
                description="d",
                script="x = 1\n",
                created_by_admin="A1",
            )
        )
        session.add(ModerationFilterConfig(guild_id="G1"))
        await session.commit()
        await store.save_policy(
            session, "G1", document, base_revision=None, by="t", source="admin"
        )

    built = []

    def actor(**kwargs):
        built.append(kwargs)
        return AdminActor(**kwargs)

    monkeypatch.setattr(
        admin_handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        admin_handlers_jobs, "get_db_session_context", _SessionCtx(test_engine)
    )
    monkeypatch.setattr(admin_handlers_jobs, "get_redis_client", lambda: redis)
    monkeypatch.setattr(admin_actions, "AdminActor", actor)

    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(handler_id),
            channel_id="C2",
            trigger_context={"trigger_type": "message", "author_id": "U7"},
        ),
        _job(),
    )

    assert [kwargs["staff_exempt_role_ids"] for kwargs in built] == [
        (POLICY_ROLE_A, POLICY_ROLE_B)
    ]


async def test_standard_fire_job_gives_a_script_claimed(
    monkeypatch, test_engine, redis
):
    handler_id = uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        session.add(
            ChannelHandler(
                id=UUID(str(handler_id)),
                guild_id="G1",
                channel_id="C1",
                name="claimed-probe",
                trigger_type="message",
                settings={},
                description="d",
                script=(
                    "await memory_set('seen', [await claimed('k'), "
                    "await claim('k', 60), await claimed('k')])\n"
                ),
                created_by="U1",
            )
        )
        await session.commit()

    monkeypatch.setattr(
        handlers_jobs,
        "get_settings",
        lambda: SimpleNamespace(handlers_enabled=True, discord_bot_token="tok"),
    )
    monkeypatch.setattr(
        handlers_jobs, "get_db_session_context", _SessionCtx(test_engine)
    )
    monkeypatch.setattr(handlers_jobs, "get_redis_client", lambda: redis)

    await handlers_jobs.run_handler_fire(
        HandlerFirePayload(
            handler_id=str(handler_id),
            trigger_context={"trigger_type": "message", "author_id": "U7"},
        ),
        _job(),
    )

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        handler = await session.get(ChannelHandler, handler_id)
    assert handler.memory["seen"] == [False, True, True]
