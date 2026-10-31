"""The staff-protection rule on the worker tier, over the real REST path.

AdminActor gathers the member, the guild (owner, roles) and every channel's
overwrites from a fake Discord API behind ``httpx.MockTransport``: the rule the
worker's moderation gateway asks, and the role rule a script's role changes
pass. A member's message is never deleted, whoever they are; a bot's or a
webhook's is. Every request the actor makes is logged, so "refused" means no
mutating call went out.
"""

from __future__ import annotations

import json

import httpx
import pytest

from smarter_dev.shared.staff_protection import ADMINISTRATOR
from smarter_dev.shared.staff_protection import BAN_MEMBERS
from smarter_dev.shared.staff_protection import MANAGE_MESSAGES
from smarter_dev.web.admin_actions import AdminActor

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
            if parts[:3] == ["guilds", GUILD, "bans"]:
                return httpx.Response(404, json={"code": 10026})
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
    from smarter_dev.web import guild_event_recorder
    from smarter_dev.web import handler_dispatch

    async def drop(*args, **kwargs):
        return []

    monkeypatch.setattr(guild_event_recorder, "record_guild_event", drop)
    monkeypatch.setattr(handler_dispatch, "dispatch_handler_event", drop)


@pytest.fixture
def api() -> FakeDiscordApi:
    return FakeDiscordApi()


def _actor(
    api: FakeDiscordApi, staff_exempt_role_ids: tuple[str, ...] | None = ()
) -> AdminActor:
    return AdminActor(
        bot_token="t",
        guild_id=GUILD,
        staff_exempt_role_ids=staff_exempt_role_ids,
        transport=httpx.MockTransport(api.handle),
    )


PROTECTED = [
    pytest.param(STAFF_ROLE_HOLDER, id="hardcoded staff role"),
    pytest.param(BANNER, id="ban members via a role"),
    pytest.param(OWNER, id="owner"),
    pytest.param(OTHER_BOT, id="another bot"),
    pytest.param(BOT, id="the bot itself"),
    pytest.param(DEPARTED_BOT, id="a bot that is not a member"),
]


@pytest.mark.parametrize("target", PROTECTED)
async def test_protected_targets_are_refused_by_the_shared_rule(api, target):
    """What the worker's moderation gateway asks before acting on a member."""
    assert await _actor(api).staff_refusal(target) is not None
    assert api.mutations == []


@pytest.mark.parametrize("target", [MEMBER, DEPARTED])
async def test_ordinary_members_and_departed_accounts_are_not_protected(api, target):
    assert await _actor(api).staff_refusal(target) is None


async def test_an_unanswerable_check_refuses(api):
    api.fail.add(f"/guilds/{GUILD}/channels")

    refusal = await _actor(api).staff_refusal(MEMBER)

    assert refusal is not None and "could not be checked" in refusal


async def test_a_role_the_guild_does_not_list_refuses(api):
    api.members[MEMBER]["roles"] = ["999"]

    assert await _actor(api).staff_refusal(MEMBER) is not None


@pytest.mark.parametrize("target_id", [GUILD, "51", MEMBER])
async def test_a_channel_overwrite_protects_nobody(api, target_id):
    # #303: staff standing comes from roles at guild level only; an @everyone,
    # role or member overwrite granting a staff permission in one channel
    # does not make the member staff.
    api.channels[0]["permission_overwrites"] = [
        {"id": target_id, "type": 0, "allow": str(MANAGE_MESSAGES), "deny": "0"}
    ]

    assert await _actor(api).staff_refusal(MEMBER) is None
    assert await _actor(api).staff_refusal(CHANNEL_MOD) is None


async def test_administrator_on_everyone_protects_everybody(api):
    api.roles[0]["permissions"] = str(ADMINISTRATOR)

    assert await _actor(api).staff_refusal(MEMBER) is not None


async def test_guild_facts_are_read_once_per_fire(api):
    actor = _actor(api)
    await actor.staff_refusal(MEMBER)
    await actor.role_refusal("51")

    reads = [path for method, path in api.requests if method == "GET"]
    assert reads.count(f"/guilds/{GUILD}") == 1
    assert reads.count(f"/guilds/{GUILD}/channels") == 1
    assert reads.count("/users/@me") == 1


async def test_a_staff_role_cannot_be_stripped(api):
    """#239: the role change itself is refused, so no moderator loses the
    standing that protects them."""
    from smarter_dev.web.admin_actions import ProtectedRoleError

    actor = _actor(api)

    with pytest.raises(ProtectedRoleError, match="is a staff role"):
        await actor.remove_role(STAFF_ROLE_HOLDER, "644301991832453120")

    assert api.mutations == []


async def test_the_guilds_staff_exempt_roles_are_staff_roles(api):
    """#255: a role the guild's filter config lists as staff carries no
    permission bit, yet the filters treat its holders as staff. A script may
    neither strip it from a moderator nor hand it out (to exempt a spammer),
    and its holders are protected."""
    from smarter_dev.web.admin_actions import ProtectedRoleError

    api.members[MEMBER]["roles"].append("52")
    api.roles.append({"id": "52", "permissions": str(SEND_MESSAGES)})
    actor = _actor(api, staff_exempt_role_ids=("52",))

    with pytest.raises(ProtectedRoleError, match="is a staff role"):
        await actor.remove_role(MEMBER, "52")
    with pytest.raises(ProtectedRoleError, match="is a staff role"):
        await actor.add_role(OTHER_BOT, "52")
    refusal = await actor.staff_refusal(MEMBER)

    assert refusal is not None and "holds a staff role" in refusal
    assert api.mutations == []


async def test_unread_staff_exempt_roles_refuse_everything(api):
    actor = _actor(api, staff_exempt_role_ids=None)

    with pytest.raises(Exception, match="staff roles were not read"):
        await actor.add_role(MEMBER, "51")
    assert await actor.staff_refusal(MEMBER) is not None
    assert api.mutations == []


async def test_stripping_an_ordinary_role_takes_one_call(api):
    actor = _actor(api)

    assert await actor.remove_role(MEMBER, "51") is True
    assert api.mutations == [("DELETE", f"/guilds/{GUILD}/members/{MEMBER}/roles/51")]
    # #242: no member read rides on a role change any more.
    assert ("GET", f"/guilds/{GUILD}/members/{MEMBER}") not in api.requests


# -- message deletes: bots' messages only ----------------------------------------


@pytest.mark.parametrize("author", [MEMBER, STAFF_ROLE_HOLDER, BANNER, OWNER])
async def test_a_members_message_is_never_deleted(api, author):
    api.message("M1", author)

    out = await _actor(api).delete_bot_message("C1", "M1")

    assert out.startswith("not deleted")
    assert "posted by a member" in out
    assert api.mutations == []


@pytest.mark.parametrize(
    ("author", "extra"),
    [
        (OTHER_BOT, {}),  # Disboard's notice: upkeep, not a sanction
        (BOT, {}),  # the bot's own old reminder
        ("3333", {"webhook_id": "3333"}),
    ],
)
async def test_bot_and_webhook_messages_are_deleted(api, author, extra):
    api.message("M1", author, **extra)

    assert await _actor(api).delete_bot_message("C1", "M1") == "deleted message M1"
    assert api.mutations == [("DELETE", "/channels/C1/messages/M1")]


async def test_a_gone_message_is_already_deleted(api):
    out = await _actor(api).delete_bot_message("C1", "M9")

    assert out == "message M9 already deleted"
    assert api.mutations == []
