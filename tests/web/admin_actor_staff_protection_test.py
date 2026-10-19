"""The staff-protection rule on the worker tier, over the real REST path.

AdminActor gathers the member, the guild (owner, roles) and every channel's
overwrites from a fake Discord API behind ``httpx.MockTransport``, and refuses
ban, kick, timeout, hold, warn and member-message deletes for protected
targets. Every request the actor makes is logged, so "refused" means no
mutating call went out.
"""

from __future__ import annotations

import json

import fakeredis.aioredis as fakeredis_aioredis
import httpx
import pytest

from smarter_dev.shared.staff_protection import ADMINISTRATOR
from smarter_dev.shared.staff_protection import BAN_MEMBERS
from smarter_dev.shared.staff_protection import MANAGE_MESSAGES
from smarter_dev.web import admin_actions
from smarter_dev.web.admin_actions import AdminActor
from smarter_dev.web.handler_holds import MemberHolds

# Run the real check; conftest otherwise clears every target.
REAL_STAFF_PROTECTION = True

GUILD = "644299523686006834"
BOT = "1000"
OWNER = "1001"
MEMBER = "2001"
STAFF_ROLE_HOLDER = "2002"
BANNER = "2003"
CHANNEL_MOD = "2004"
OTHER_BOT = "2005"
DEPARTED = "2006"
DEPARTED_BOT = "2007"
SEND_MESSAGES = 1 << 11


class FakeDiscordApi:
    """Just enough of Discord's REST API for AdminActor."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.roles = [
            {"id": GUILD, "permissions": str(SEND_MESSAGES)},  # @everyone
            {"id": "50", "permissions": str(BAN_MEMBERS)},
            {"id": "51", "permissions": str(SEND_MESSAGES)},
        ]
        self.members = {
            MEMBER: {"roles": ["51"]},
            STAFF_ROLE_HOLDER: {"roles": ["644301991832453120"]},
            BANNER: {"roles": ["50"]},
            CHANNEL_MOD: {"roles": []},
            OTHER_BOT: {"roles": [], "bot": True},
            OWNER: {"roles": []},
        }
        self.users = {DEPARTED: {}, DEPARTED_BOT: {"bot": True}}
        self.channels = [
            {"id": "C1", "permission_overwrites": []},
            {
                "id": "C2",
                "permission_overwrites": [
                    {
                        "id": CHANNEL_MOD,
                        "type": 1,
                        "allow": str(MANAGE_MESSAGES),
                        "deny": "0",
                    }
                ],
            },
        ]
        self.messages: dict[str, dict] = {}
        self.fail: set[str] = set()

    def message(self, message_id: str, author_id: str, **extra) -> None:
        author = {"id": author_id}
        if author_id in (BOT, OTHER_BOT):
            author["bot"] = True
        self.messages[message_id] = {"id": message_id, "author": author, **extra}

    @property
    def mutations(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path.removeprefix("/api/v10")
        self.requests.append((method, path))
        if path in self.fail:
            return httpx.Response(500, json={"message": "boom"})
        parts = path.strip("/").split("/")
        if method == "GET":
            if path == "/users/@me":
                return httpx.Response(200, json={"id": BOT, "bot": True})
            if parts[0] == "users":
                user = self.users.get(parts[1])
                if user is None and parts[1] in self.members:
                    user = {"bot": self.members[parts[1]].get("bot", False)}
                if user is None:
                    return httpx.Response(404, json={"code": 10013})
                return httpx.Response(200, json={"id": parts[1], **user})
            if parts == ["guilds", GUILD]:
                return httpx.Response(
                    200, json={"id": GUILD, "owner_id": OWNER, "roles": self.roles}
                )
            if parts == ["guilds", GUILD, "channels"]:
                return httpx.Response(200, json=self.channels)
            if parts[:3] == ["guilds", GUILD, "members"]:
                member = self.members.get(parts[3])
                if member is None:
                    return httpx.Response(404, json={"code": 10007})
                user = {"id": parts[3]}
                if member.get("bot"):
                    user["bot"] = True
                return httpx.Response(
                    200, json={"user": user, "roles": member["roles"]}
                )
            if parts[0] == "channels" and parts[2] == "messages":
                message = self.messages.get(parts[3])
                if message is None:
                    return httpx.Response(404, json={"code": 10008})
                return httpx.Response(200, json=message)
        if method == "DELETE" and parts[:3] == ["guilds", GUILD, "members"]:
            if len(parts) == 6 and parts[4] == "roles":
                self.members[parts[3]]["roles"].remove(parts[5])
        if method == "PATCH" and parts[:3] == ["guilds", GUILD, "members"]:
            body = json.loads(request.content)
            return httpx.Response(200, json={"user": {"id": parts[3]}, **body})
        return httpx.Response(204)


@pytest.fixture(autouse=True)
def quiet_event_log(monkeypatch):
    async def drop(event, **kwargs) -> None:
        return None

    monkeypatch.setattr(admin_actions, "record_guild_event", drop)


@pytest.fixture
def api() -> FakeDiscordApi:
    return FakeDiscordApi()


def _actor(api: FakeDiscordApi) -> AdminActor:
    return AdminActor(
        bot_token="t", guild_id=GUILD, transport=httpx.MockTransport(api.handle)
    )


PROTECTED = [
    pytest.param(STAFF_ROLE_HOLDER, id="hardcoded staff role"),
    pytest.param(BANNER, id="ban members via a role"),
    pytest.param(CHANNEL_MOD, id="manage messages in one channel"),
    pytest.param(OWNER, id="owner"),
    pytest.param(OTHER_BOT, id="another bot"),
    pytest.param(BOT, id="the bot itself"),
    pytest.param(DEPARTED_BOT, id="a bot that is not a member"),
]


@pytest.mark.parametrize("target", PROTECTED)
async def test_protected_targets_are_never_banned_kicked_or_timed_out(api, target):
    actor = _actor(api)

    assert (await actor.ban_user(target, "scam")).startswith("not banned")
    assert (await actor.kick_user(target)).startswith("not kicked")
    assert (await actor.timeout_user(target, 600)).startswith("not timed out")
    assert api.mutations == []


@pytest.mark.parametrize("target", [MEMBER, DEPARTED])
async def test_ordinary_members_and_departed_accounts_can_be_actioned(api, target):
    actor = _actor(api)

    assert await actor.ban_user(target, "scam") == f"banned {target}"
    assert ("PUT", f"/guilds/{GUILD}/bans/{target}") in api.mutations


async def test_an_unanswerable_check_refuses(api):
    api.fail.add(f"/guilds/{GUILD}/channels")

    out = await _actor(api).ban_user(MEMBER, "scam")

    assert out.startswith("not banned")
    assert "could not be checked" in out
    assert api.mutations == []


async def test_a_role_the_guild_does_not_list_refuses(api):
    api.members[MEMBER]["roles"] = ["999"]

    assert (await _actor(api).timeout_user(MEMBER, 60)).startswith("not timed out")
    assert api.mutations == []


@pytest.mark.parametrize("member_denied", [False, True])
async def test_channel_overwrites_apply_in_discords_order(api, member_denied):
    # A role grant in one channel protects; a member-level deny there cancels it.
    overwrites = [{"id": "51", "type": 0, "allow": str(MANAGE_MESSAGES), "deny": "0"}]
    if member_denied:
        overwrites.append(
            {"id": MEMBER, "type": 1, "allow": "0", "deny": str(MANAGE_MESSAGES)}
        )
    api.channels[0]["permission_overwrites"] = overwrites

    out = await _actor(api).kick_user(MEMBER)

    if member_denied:
        assert out == f"kicked {MEMBER}"
    else:
        assert out.startswith("not kicked")


async def test_administrator_on_everyone_protects_everybody(api):
    api.roles[0]["permissions"] = str(ADMINISTRATOR)

    assert (await _actor(api).ban_user(MEMBER)).startswith("not banned")


async def test_guild_facts_are_read_once_per_fire(api):
    actor = _actor(api)
    await actor.ban_user(MEMBER)
    await actor.kick_user(MEMBER)

    reads = [path for method, path in api.requests if method == "GET"]
    assert reads.count(f"/guilds/{GUILD}") == 1
    assert reads.count(f"/guilds/{GUILD}/channels") == 1
    assert reads.count("/users/@me") == 1


async def test_stripping_a_staff_role_does_not_clear_the_member_in_that_fire(api):
    actor = _actor(api)

    assert await actor.remove_role(STAFF_ROLE_HOLDER, "644301991832453120") is True
    out = await actor.ban_user(STAFF_ROLE_HOLDER, "scam")

    assert out.startswith("not banned")
    assert api.mutations == [
        (
            "DELETE",
            f"/guilds/{GUILD}/members/{STAFF_ROLE_HOLDER}/roles/644301991832453120",
        )
    ]


async def test_stripping_an_ordinary_role_leaves_the_member_actionable(api):
    actor = _actor(api)

    assert await actor.remove_role(MEMBER, "51") is True

    assert await actor.ban_user(MEMBER, "scam") == f"banned {MEMBER}"


# -- message deletes -------------------------------------------------------------


@pytest.mark.parametrize("author", [STAFF_ROLE_HOLDER, CHANNEL_MOD, OWNER])
async def test_a_staff_members_message_is_not_deleted(api, author):
    api.message("M1", author)

    out = await _actor(api).delete_message("C1", "M1")

    assert out.startswith("not deleted")
    assert api.mutations == []


@pytest.mark.parametrize(
    ("author", "extra"),
    [
        (MEMBER, {}),
        (OTHER_BOT, {}),  # Disboard's notice: upkeep, not a sanction
        (BOT, {}),  # the bot's own old reminder
        ("3333", {"webhook_id": "3333"}),
    ],
)
async def test_member_bot_and_webhook_messages_are_deleted(api, author, extra):
    api.message("M1", author, **extra)

    assert await _actor(api).delete_message("C1", "M1") == "deleted message M1"
    assert api.mutations == [("DELETE", "/channels/C1/messages/M1")]


# -- holds and warns -------------------------------------------------------------


async def test_a_staff_member_is_never_held(api):
    redis = fakeredis_aioredis.FakeRedis(decode_responses=True)
    holds = MemberHolds(redis=redis, guild_id=GUILD, handler_id="H1", actor=_actor(api))

    assert await holds.hold(BANNER, "post-a", 300) is False
    assert api.mutations == []
    assert await redis.keys("*") == []


async def test_an_ordinary_member_is_held(api):
    redis = fakeredis_aioredis.FakeRedis(decode_responses=True)
    holds = MemberHolds(redis=redis, guild_id=GUILD, handler_id="H1", actor=_actor(api))

    assert await holds.hold(MEMBER, "post-a", 300) is True
    assert ("PATCH", f"/guilds/{GUILD}/members/{MEMBER}") in api.mutations
