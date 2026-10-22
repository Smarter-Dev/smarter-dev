"""The shared staff-protection rule (``smarter_dev.shared.staff_protection``)."""

from __future__ import annotations

import hikari
import pytest

from smarter_dev.shared import staff_protection as rule
from smarter_dev.shared.staff_protection import GuildFacts
from smarter_dev.shared.staff_protection import MemberFacts
from smarter_dev.shared.staff_protection import Overwrite

GUILD = "10"
BOT = "1"
OWNER = "2"
USER = "3"


def _guild(roles=None, channels=(), owner=OWNER) -> GuildFacts:
    return GuildFacts(
        guild_id=GUILD,
        owner_id=owner,
        role_permissions={
            GUILD: int(hikari.Permissions.SEND_MESSAGES),
            **(roles or {}),
        },
        channel_overwrites=tuple(channels),
    )


def _refusal(member: MemberFacts, guild: GuildFacts, **kw) -> str | None:
    return rule.member_refusal(member, guild, bot_user_id=BOT, **kw)


def _user(*role_ids, is_bot=False, user_id=USER) -> MemberFacts:
    return MemberFacts(user_id=user_id, is_bot=is_bot, role_ids=tuple(role_ids))


def test_the_staff_permissions_are_exactly_the_eleven_named_in_237():
    expected = (
        hikari.Permissions.ADMINISTRATOR
        | hikari.Permissions.MANAGE_GUILD
        | hikari.Permissions.MANAGE_ROLES
        | hikari.Permissions.MANAGE_CHANNELS
        | hikari.Permissions.MANAGE_MESSAGES
        | hikari.Permissions.MODERATE_MEMBERS
        | hikari.Permissions.KICK_MEMBERS
        | hikari.Permissions.BAN_MEMBERS
        | hikari.Permissions.MANAGE_NICKNAMES
        | hikari.Permissions.MANAGE_WEBHOOKS
        | hikari.Permissions.VIEW_AUDIT_LOG
    )
    assert rule.STAFF_PERMISSIONS == int(expected)


def test_the_three_staff_roles_are_protected():
    assert rule.STAFF_ROLE_IDS == {
        "644390354157568014",
        "644301991832453120",
        "720655282115706892",
    }
    for role_id in rule.STAFF_ROLE_IDS:
        assert _refusal(_user(role_id), _guild()) == rule.STAFF_ROLE


def test_a_member_holding_only_the_staff_role_is_refused():
    # #303: `staff` grants Manage Messages only through mod-chat's overwrite.
    mod_chat = {
        "720655282115706892": Overwrite(
            allow=int(hikari.Permissions.MANAGE_MESSAGES), deny=0
        )
    }
    guild = _guild({"720655282115706892": 0}, channels=[mod_chat])

    assert _refusal(_user("720655282115706892"), guild) == rule.STAFF_ROLE


def test_a_configured_extra_role_is_protected():
    assert (
        _refusal(_user("77"), _guild({"77": 0}), extra_role_ids=["77"])
        == rule.STAFF_ROLE
    )


@pytest.mark.parametrize(
    "permission",
    [
        hikari.Permissions.ADMINISTRATOR,
        hikari.Permissions.MANAGE_GUILD,
        hikari.Permissions.MANAGE_ROLES,
        hikari.Permissions.MANAGE_CHANNELS,
        hikari.Permissions.MANAGE_MESSAGES,
        hikari.Permissions.MODERATE_MEMBERS,
        hikari.Permissions.KICK_MEMBERS,
        hikari.Permissions.BAN_MEMBERS,
        hikari.Permissions.MANAGE_NICKNAMES,
        hikari.Permissions.MANAGE_WEBHOOKS,
        hikari.Permissions.VIEW_AUDIT_LOG,
    ],
)
def test_each_staff_permission_protects_at_guild_level_only(permission):
    assert (
        _refusal(_user("5"), _guild({"5": int(permission)}))
        == rule.GUILD_STAFF_PERMISSION
    )
    channel = {USER: Overwrite(allow=int(permission), deny=0)}
    assert _refusal(_user("5"), _guild({"5": 0}, channels=[channel])) is None


def test_permissions_given_to_everyone_count():
    guild = GuildFacts(
        guild_id=GUILD,
        owner_id=OWNER,
        role_permissions={GUILD: int(hikari.Permissions.KICK_MEMBERS)},
        channel_overwrites=(),
    )
    assert _refusal(_user(), guild) == rule.GUILD_STAFF_PERMISSION


def test_an_ordinary_member_is_not_protected():
    roles = {
        "5": int(hikari.Permissions.SEND_MESSAGES | hikari.Permissions.ATTACH_FILES)
    }
    channel = {"5": Overwrite(allow=int(hikari.Permissions.EMBED_LINKS), deny=0)}
    assert _refusal(_user("5"), _guild(roles, channels=[channel])) is None


def test_channel_overwrites_make_nobody_staff():
    # #303: #the-sudo-squad's @everyone overwrite granted Manage Webhooks, and
    # every member, squad role or none, came back as staff.
    webhooks = int(hikari.Permissions.MANAGE_WEBHOOKS)
    manage = int(hikari.Permissions.MANAGE_MESSAGES)
    sudo_squad = {
        GUILD: Overwrite(allow=webhooks, deny=int(hikari.Permissions.VIEW_CHANNEL))
    }
    mod_chat = {"6": Overwrite(allow=manage, deny=0)}
    own = {USER: Overwrite(allow=manage, deny=0)}
    guild = _guild({"5": 0, "6": 0}, channels=[sudo_squad, mod_chat, own])

    assert _refusal(_user(), guild) is None
    assert _refusal(_user("5"), guild) is None
    assert _refusal(_user("6"), guild) is None


def test_real_mods_stay_exempt_beside_channel_overwrites():
    mods = int(
        hikari.Permissions.KICK_MEMBERS
        | hikari.Permissions.BAN_MEMBERS
        | hikari.Permissions.MANAGE_MESSAGES
        | hikari.Permissions.MODERATE_MEMBERS
    )
    sudo_squad = {
        GUILD: Overwrite(allow=int(hikari.Permissions.MANAGE_WEBHOOKS), deny=0)
    }
    guild = _guild({"8": mods, "644390354157568014": 0}, channels=[sudo_squad])

    assert _refusal(_user("8"), guild) == rule.GUILD_STAFF_PERMISSION
    assert _refusal(_user("644390354157568014"), guild) == rule.STAFF_ROLE
    assert _refusal(_user(user_id=OWNER), guild) == rule.GUILD_OWNER


def test_owner_and_bots_are_protected():
    assert _refusal(_user(user_id=OWNER), _guild()) == rule.GUILD_OWNER
    assert _refusal(_user(user_id=BOT, is_bot=True), _guild()) == rule.BOT_ITSELF
    assert _refusal(_user(is_bot=True), _guild()) == rule.A_BOT


@pytest.mark.parametrize(
    ("member", "guild", "bot_user_id"),
    [
        (_user("404"), _guild(), BOT),  # a role the guild does not list
        (_user(), _guild(owner=None), BOT),  # owner unknown
        (_user(), _guild(), None),  # the bot's own id unknown
        (
            _user(),
            GuildFacts(
                guild_id=GUILD,
                owner_id=OWNER,
                role_permissions={},
                channel_overwrites=(),
            ),
            BOT,
        ),  # @everyone unknown
    ],
)
def test_what_cannot_be_established_protects(member, guild, bot_user_id):
    refusal = rule.member_refusal(member, guild, bot_user_id=bot_user_id)
    assert refusal is not None
    assert refusal.startswith(rule.UNKNOWN_PREFIX)


def test_non_members_are_protected_only_as_bots_or_unknowns():
    assert rule.non_member_refusal(USER, is_bot=False, bot_user_id=BOT) is None
    assert rule.non_member_refusal(USER, is_bot=True, bot_user_id=BOT) == rule.A_BOT
    assert (
        rule.non_member_refusal(BOT, is_bot=False, bot_user_id=BOT) == rule.BOT_ITSELF
    )
    assert rule.non_member_refusal(USER, is_bot=None, bot_user_id=BOT).startswith(
        rule.UNKNOWN_PREFIX
    )
