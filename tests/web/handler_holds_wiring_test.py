"""``claimed``, ``hold_member`` and ``release_hold`` as a script reaches them.

The runtime tests drive the three functions through ``run_handler_script``
with the real :class:`MemberHolds` and the real claim functions over one
Redis. The last two tests run the real fire jobs, so nothing between a
handler's script and Redis or Discord's member endpoint is stood in for
except the HTTP transport.
"""

from __future__ import annotations

import json
import math
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
from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.admin_actions import HandlerFireFacts
from smarter_dev.web.admin_handlers_jobs import AdminHandlerFirePayload
from smarter_dev.web.handler_budget import HandlerBudget
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_caps import MAX_CLAIMS_PER_FIRE
from smarter_dev.web.handler_caps import claim_handler_key
from smarter_dev.web.handler_caps import handler_key_claimed
from smarter_dev.web.handler_holds import HOLD_MAX_SECONDS
from smarter_dev.web.handler_holds import MemberHolds
from smarter_dev.web.handler_holds import member_holds_lock_key
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


class _Discord(FakeModerationDiscord):
    """The guild; notes whether the member's hold lock is held at each write."""

    def __init__(self) -> None:
        super().__init__(guild_id="G1")
        self.add_member("U1")
        self.redis = None
        self.locked_during: list = []
        self._writes: list = []

    async def set_timeout(self, guild_id, user_id, until, *, reason):
        self.locked_during.append(
            bool(await self.redis.exists(member_holds_lock_key("G1", "U1")))
        )
        self._writes.append(
            ("remove_timeout",)
            if until is None
            else ("timeout", math.ceil(until.timestamp() - self.now()))
        )
        return await super().set_timeout(guild_id, user_id, until, reason=reason)

    def writes(self) -> list:
        """Each timeout write as ("timeout", seconds) or ("remove_timeout",)."""
        return list(self._writes)


def _actor(discord: _Discord) -> AdminActor:
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


async def _run(script, redis, *, discord=None, budget=None, wired=True):
    """Run ``script`` as an admin fire of handler H1; return (result, said)."""
    emitter = _Emitter()
    discord = discord or _Discord()
    discord.redis = redis
    actor = _actor(discord)
    kwargs = {}
    if wired:
        kwargs = {
            "claimer": partial(claim_handler_key, redis, "H1"),
            "claim_reader": partial(handler_key_claimed, redis, "H1"),
            "holds": MemberHolds(
                redis=redis, guild_id="G1", handler_id="H1", actor=actor
            ),
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


# -- hold_member / release_hold ----------------------------------------------------


async def test_a_script_holds_and_releases_a_member(redis):
    discord = _Discord()
    script = (
        'first = await hold_member("U1", "post-a")\n'
        'again = await hold_member("U1", "post-a", 300)\n'
        'lifted = await release_hold("U1", "post-a")\n'
        "await send_message(str([first, again, lifted]))\n"
    )

    result, said = await _run(script, redis, discord=discord)

    assert result.outcome == "ok", result.error
    assert said == ["[True, False, True]"]
    assert discord.writes() == [("timeout", 300), ("remove_timeout",)]
    # Three calls, three moderation actions: the same price as the timeout
    # calls they stand in for.
    assert result.usage["mod_actions"] == 3


async def test_hold_and_release_are_refused_to_a_fire_with_no_mod_actions(redis):
    # A mod_action-triggered fire runs with zero: it can neither hold a member
    # nor undo a hold in response to a moderation action.
    discord = _Discord()
    budget = HandlerBudget(max_mod_actions=0, max_messages=5)

    held, _ = await _run(
        'await hold_member("U1", "k")\n', redis, discord=discord, budget=budget
    )
    released, _ = await _run(
        'await release_hold("U1", "k")\n', redis, discord=discord, budget=budget
    )

    assert (held.cap, released.cap) == ("mod_actions", "mod_actions")
    assert discord.writes() == []
    assert await redis.keys("hhold:*") == []


@pytest.mark.parametrize(
    "call",
    [
        'hold_member("U1", "")',
        'hold_member("U1", 7)',
        'hold_member("U1", "k" * 129)',
        'hold_member("U1", "k", 0)',
        'hold_member("U1", "k", 2.5)',
        'hold_member("U1", "k", True)',
        f'hold_member("U1", "k", {HOLD_MAX_SECONDS + 1})',
        'release_hold("U1", "")',
    ],
)
async def test_a_bad_hold_call_errors_before_anything_is_spent(redis, call):
    discord = _Discord()

    result, _ = await _run(f"await {call}\n", redis, discord=discord)

    assert result.outcome == "error"
    assert result.usage["mod_actions"] == 0
    assert discord.writes() == []


async def test_hold_functions_not_configured_error_loudly(redis):
    result, _ = await _run('await hold_member("U1", "k")\n', redis, wired=False)

    assert result.outcome == "error"


async def test_timeout_user_and_remove_timeout_run_under_the_members_lock(redis):
    discord = _Discord()
    script = 'await timeout_user("U1", 600)\nawait remove_timeout("U1")\n'

    result, _ = await _run(script, redis, discord=discord)

    assert result.outcome == "ok", result.error
    assert discord.writes() == [("timeout", 600), ("remove_timeout",)]
    assert discord.locked_during == [True, True]
    assert await redis.exists(member_holds_lock_key("G1", "U1")) == 0


async def test_a_release_does_not_lift_what_the_scripts_own_timeout_user_set(redis):
    discord = _Discord()
    script = (
        'await hold_member("U1", "post-a", 300)\n'
        'await timeout_user("U1", 3600)\n'
        'await send_message(str(await release_hold("U1", "post-a")))\n'
    )

    _, said = await _run(script, redis, discord=discord)

    assert said == ["False"]
    assert discord.writes() == [("timeout", 300), ("timeout", 3600)]


async def test_without_holds_wired_the_timeout_calls_still_reach_the_actor(redis):
    # A caller of run_handler_script that passes no holds keeps the plain calls.
    discord = _Discord()
    script = 'await timeout_user("U1", 600)\nawait remove_timeout("U1")\n'

    result, _ = await _run(script, redis, discord=discord, wired=False)

    assert result.outcome == "ok", result.error
    assert discord.writes() == [("timeout", 600), ("remove_timeout",)]
    assert discord.locked_during == [False, False]


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


async def test_admin_fire_job_gives_a_script_real_holds_and_claims(
    monkeypatch, test_engine, redis
):
    member: dict = {"requests": []}

    def discord_api(request: httpx.Request) -> httpx.Response:
        member["requests"].append((request.method, request.url.path))
        if request.method == "PATCH":
            member["until"] = json.loads(request.content)[
                "communication_disabled_until"
            ]
        return httpx.Response(
            200,
            json={
                "user": {"id": "U7"},
                "communication_disabled_until": member.get("until"),
            },
        )

    async def drop_event(event, **kwargs) -> None:
        return None

    handler_id = uuid4()
    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        session.add(
            AdminHandler(
                id=handler_id,
                guild_id="G1",
                name="hold-probe",
                trigger_type="message",
                settings={},
                channel_ids=[],
                description="d",
                script=(
                    "user = context['author_id']\n"
                    "seen = [await claimed('bad:k'), await claim('bad:k', 60), "
                    "await claimed('bad:k')]\n"
                    "seen.append(await hold_member(user, 'post-a', 300))\n"
                    "seen.append(await hold_member(user, 'post-b', 300))\n"
                    "seen.append(await release_hold(user, 'post-a'))\n"
                    "await memory_set('seen', seen)\n"
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

    await admin_handlers_jobs.run_admin_handler_fire(
        AdminHandlerFirePayload(
            admin_handler_id=str(handler_id),
            channel_id="C2",
            trigger_context={"trigger_type": "message", "author_id": "U7"},
        ),
        _job(),
    )

    async with async_sessionmaker(test_engine, expire_on_commit=False)() as session:
        handler = await session.get(AdminHandler, handler_id)
    # Not claimed, claimed by this call, claimed. Both holds began. The first
    # release lifts nothing: the second post still needs the timeout.
    assert handler.memory["seen"] == [False, True, True, True, True, False]
    assert member["until"] is not None
    assert ("PATCH", "/api/v10/guilds/G1/members/U7") in member["requests"]
    # Scoped to this guild, this member and this handler.
    held = await redis.hgetall("hhold:G1:U7")
    assert {f"{handler_id}:post-a", f"{handler_id}:post-b"} <= set(held)
    assert await redis.exists(f"hclaim:{handler_id}:bad:k") == 1


async def test_admin_fire_job_hands_the_actor_the_guilds_staff_roles(
    monkeypatch, test_engine, redis
):
    """#255: the fire's actor protects the roles the guild's filters treat
    as staff (``staff_exempt_role_ids``); left unread it refuses everything."""
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
        session.add(
            ModerationFilterConfig(guild_id="G1", staff_exempt_role_ids=["R9", 77])
        )
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

    assert [kwargs["staff_exempt_role_ids"] for kwargs in built] == [("R9", "77")]


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
