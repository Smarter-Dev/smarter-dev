"""Tests for the bot-core content filters (blocked TLD / webhook killer / invites).

The pure layer (link extraction, TLD matching, webhook-URL validation, invite
detection, sanitization, notice + mod-log formatting) is exercised directly with
plain strings — no Discord, no database. The listener shell
(:func:`check_content_filters`) is then tested over fakes to prove the wiring:
every toggle, every exemption, and the failure paths that must not abort the
remaining filters. The fake bot has no cache, so the shared staff rule reads the
guild (owner, roles, channel overwrites) and the author from the fake REST.
"""

from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace

import hikari
import pytest
from sqlalchemy import select

from smarter_dev.bot import content_filter
from smarter_dev.bot.content_filter import ContentFilterResult
from smarter_dev.bot.content_filter import build_blocked_tld_notice
from smarter_dev.bot.content_filter import build_invite_notice
from smarter_dev.bot.content_filter import build_webhook_kill_reason
from smarter_dev.bot.content_filter import build_webhook_killed_notice
from smarter_dev.bot.content_filter import category_is_invite_exempt
from smarter_dev.bot.content_filter import check_content_filters
from smarter_dev.bot.content_filter import destroyed_webhook_ids
from smarter_dev.bot.content_filter import extract_links
from smarter_dev.bot.content_filter import find_blocked_tld_links
from smarter_dev.bot.content_filter import find_invite_links
from smarter_dev.bot.content_filter import find_webhook_url_candidates
from smarter_dev.bot.content_filter import is_discord_webhook_url
from smarter_dev.bot.content_filter import link_host
from smarter_dev.bot.content_filter import parse_discord_webhook_url
from smarter_dev.shared import moderation_policy as mp
from smarter_dev.web import moderation_policy_store as store
from smarter_dev.web.crud import DatabaseOperationError
from smarter_dev.web.models import ModerationAction
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.webhook_urls import normalize_webhook_url

GUILD_ID = 555
CHANNEL_ID = 777
CATEGORY_ID = 888
AUTHOR_ID = 111
MESSAGE_ID = 999
STAFF_ROLE_ID = 222
MOD_LOG_CHANNEL_ID = 333
BOT_USER_ID = 444
OWNER_ID = 1
#: Hardcoded in the shared staff rule; not in the guild's configured exemptions.
HARDCODED_STAFF_ROLE_ID = 644390354157568014

WEBHOOK_URL = "https://discord.com/api/webhooks/123456789/abcDEF-_token"


# --------------------------------------------------------------------------
# Pure layer: link extraction
# --------------------------------------------------------------------------


def test_extract_links_finds_schemed_and_bare_links_in_order():
    content = "hey visit https://evil.gay/free and also sketchy.xxx/now please"

    assert extract_links(content) == ["https://evil.gay/free", "sketchy.xxx/now"]


def test_extract_links_strips_trailing_punctuation_and_dedupes():
    content = "(https://evil.gay/free), again https://evil.gay/free."

    assert extract_links(content) == ["https://evil.gay/free"]


def test_extract_links_ignores_prose_that_is_not_a_domain():
    content = "Hello world. e.g. nothing here at all!"

    assert extract_links(content) == []


def test_link_host_strips_scheme_port_path_and_userinfo():
    assert link_host("https://User@Evil.GAY:8443/path?x=1#frag") == "evil.gay"
    assert link_host("sketchy.xxx") == "sketchy.xxx"


# --------------------------------------------------------------------------
# Pure layer: blocked TLDs
# --------------------------------------------------------------------------


def test_find_blocked_tld_links_matches_host_suffix_only():
    content = (
        "https://evil.gay/free and https://notgay.com/ok and https://x.gaymer.net/no"
    )

    assert find_blocked_tld_links(content, [".gay"]) == ["https://evil.gay/free"]


def test_find_blocked_tld_links_is_case_insensitive_and_tolerates_missing_dot():
    content = "HTTPS://Evil.GAY/free"

    assert find_blocked_tld_links(content, ["gay"]) == ["HTTPS://Evil.GAY/free"]


def test_find_blocked_tld_links_returns_nothing_when_no_tlds_configured():
    assert find_blocked_tld_links("https://evil.gay/free", []) == []


# --------------------------------------------------------------------------
# Pure layer: webhook URLs
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://discord.com/api/webhooks/123456789/abcDEF-_token",
        "https://canary.discord.com/api/webhooks/123456789/abcDEF-_token",
        "https://ptb.discord.com/api/webhooks/123456789/abcDEF-_token",
        "https://discordapp.com/api/webhooks/123456789/abcDEF-_token",
    ],
)
def test_is_discord_webhook_url_accepts_every_discord_host_variant(url):
    assert is_discord_webhook_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/api/webhooks/1/token",
        "https://discord.com.evil.com/api/webhooks/1/token",
        "https://discord.com/api/webhooks/123456789",
        "https://discord.com/api/webhooks/123/../../users/@me",
        "http://discord.com/api/webhooks/123/token",
        "not-a-url",
    ],
)
def test_is_discord_webhook_url_rejects_foreign_hosts_and_malformed_paths(url):
    assert is_discord_webhook_url(url) is False
    assert parse_discord_webhook_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        # No scheme at all — the shape a spammer pastes from a config file.
        "discord.com/api/webhooks/123456789/abcDEF-_token",
        # Uppercase scheme and uppercase host.
        "HTTPS://discord.com/api/webhooks/123456789/abcDEF-_token",
        "https://DISCORD.com/api/webhooks/123456789/abcDEF-_token",
        "HTTPS://Canary.Discord.COM/api/webhooks/123456789/abcDEF-_token",
        # The query string Discord's own documentation emits.
        "https://discord.com/api/webhooks/123456789/abcDEF-_token?wait=true",
        "https://discord.com/api/webhooks/123456789/abcDEF-_token#frag",
        "discord.com/api/webhooks/123456789/abcDEF-_token?wait=true",
    ],
)
def test_is_discord_webhook_url_accepts_the_common_leak_shapes(url):
    assert is_discord_webhook_url(url) is True

    reference = parse_discord_webhook_url(url)

    assert reference is not None
    assert reference.webhook_id == "123456789"
    # The token is case-sensitive and must survive normalization untouched.
    assert reference.token == "abcDEF-_token"


@pytest.mark.parametrize(
    "url",
    [
        # A lookalike that differs from the real host only in case.
        "https://DISCORD.COM.evil.com/api/webhooks/1/token",
        "DISCORD.COM.evil.com/api/webhooks/1/token",
        "EVIL.example.com/api/webhooks/1/token",
        # Plain http stays rejected however it is cased.
        "HTTP://discord.com/api/webhooks/123/token",
        # Traversal stays rejected even when a query string is stripped.
        "https://discord.com/api/webhooks/123/../../users/@me?wait=true",
        # A real host smuggled into the query of a foreign host.
        "https://evil.example.com/?next=https://discord.com/api/webhooks/1/token",
        # Userinfo trick: the real host is only the userinfo of a foreign host.
        "https://discord.com@evil.example.com/api/webhooks/1/token",
    ],
)
def test_is_discord_webhook_url_still_rejects_lookalikes_after_normalization(url):
    assert is_discord_webhook_url(url) is False
    assert parse_discord_webhook_url(url) is None


def test_normalize_webhook_url_lowercases_scheme_and_host_only():
    normalized = normalize_webhook_url(
        "HTTPS://DISCORD.com/api/webhooks/123/abcDEF-_token?wait=true#frag"
    )

    assert normalized == "https://discord.com/api/webhooks/123/abcDEF-_token"
    # A missing scheme becomes https, an explicit http is never upgraded.
    assert normalize_webhook_url("discord.com/x") == "https://discord.com/x"
    assert normalize_webhook_url("HTTP://Discord.com/x") == "http://discord.com/x"


def test_parse_discord_webhook_url_splits_id_and_token():
    reference = parse_discord_webhook_url(WEBHOOK_URL)

    assert reference is not None
    assert reference.webhook_id == "123456789"
    assert reference.token == "abcDEF-_token"


def test_find_webhook_url_candidates_includes_foreign_hosts_for_rejection():
    content = f"leak {WEBHOOK_URL} and https://evil.example.com/api/webhooks/1/tok"

    candidates = find_webhook_url_candidates(content)

    assert candidates == [WEBHOOK_URL, "https://evil.example.com/api/webhooks/1/tok"]


# --------------------------------------------------------------------------
# Pure layer: invites
# --------------------------------------------------------------------------


def test_find_invite_links_matches_short_and_long_invite_forms():
    content = "join discord.gg/abc123 or https://discord.com/invite/Xy-9"

    assert find_invite_links(content) == [
        "discord.gg/abc123",
        "https://discord.com/invite/Xy-9",
    ]


def test_find_invite_links_ignores_plain_discord_links():
    content = (
        "see https://discord.com/channels/1/2/3 and discord.gg by itself "
        f"and {WEBHOOK_URL}"
    )

    assert find_invite_links(content) == []


# --------------------------------------------------------------------------
# Pure layer: sanitization and formatting
# --------------------------------------------------------------------------


def test_notice_builders_mention_the_author_and_describe_the_kill():
    assert f"<@{AUTHOR_ID}>" in build_blocked_tld_notice(str(AUTHOR_ID))
    assert f"<@{AUTHOR_ID}>" in build_invite_notice(str(AUTHOR_ID))

    notice = build_webhook_killed_notice(
        author_id=str(AUTHOR_ID), killed_count=1, already_dead_count=1
    )

    assert f"<@{AUTHOR_ID}>" in notice
    assert "1" in notice
    assert "webhook" in notice.lower()


def test_destroyed_webhook_ids_dedupes_and_drops_unparseable_urls():
    assert destroyed_webhook_ids(
        [WEBHOOK_URL, WEBHOOK_URL, "https://evil.example.com/api/webhooks/1/tok"]
    ) == ["123456789"]


def test_build_webhook_kill_reason_lists_ids_without_tokens():
    reason = build_webhook_kill_reason(["123456789", "987654321"])

    assert "123456789" in reason
    assert "987654321" in reason
    assert "abcDEF-_token" not in reason


# --------------------------------------------------------------------------
# Pure layer: exemptions
# --------------------------------------------------------------------------


def test_category_is_invite_exempt_handles_missing_parent():
    assert category_is_invite_exempt("888", ["888"]) is True
    assert category_is_invite_exempt("777", ["888"]) is False
    assert category_is_invite_exempt(None, ["888"]) is False


# --------------------------------------------------------------------------
# Listener shell fakes
# --------------------------------------------------------------------------


class _FakeRest:
    """Records every REST call the filters make, optionally failing them.

    ``roles`` (role id -> permissions; @everyone's id is the guild's),
    ``overwrites`` (one dict per channel, target id -> (allow, deny)) and
    ``members`` (user id -> role ids) are the guild the staff rule reads.
    ``guild_error`` makes the guild read fail.
    """

    def __init__(
        self,
        *,
        delete_message_error: Exception | None = None,
        delete_webhook_error: Exception | None = None,
        fetch_channel_error: Exception | None = None,
        channel: object | None = None,
        roles: dict[int, hikari.Permissions] | None = None,
        overwrites: list[dict] | None = None,
        members: dict[int, list[int]] | None = None,
        guild_error: Exception | None = None,
    ) -> None:
        self.roles = {GUILD_ID: hikari.Permissions.SEND_MESSAGES, **(roles or {})}
        self.overwrites = overwrites or [{}]
        self.members = {AUTHOR_ID: []} if members is None else members
        self._guild_error = guild_error
        self.created_messages: list[tuple[int, str, dict]] = []
        self.edited_messages: list[tuple[int, str, dict]] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self.deleted_webhooks: list[tuple[str, str]] = []
        self.fetched_channels: list[int] = []
        self._delete_message_error = delete_message_error
        self._delete_webhook_error = delete_webhook_error
        self._fetch_channel_error = fetch_channel_error
        self._channel = channel

    async def create_message(self, channel, content=None, **kwargs):
        self.created_messages.append((int(channel), content, kwargs))
        return SimpleNamespace(id=900 + len(self.created_messages))

    async def edit_message(self, channel, message, content=None, **kwargs):
        self.edited_messages.append((int(channel), content, kwargs))

    async def fetch_ban(self, guild, user):
        raise _not_found()

    async def delete_message(self, channel, message):
        self.deleted_messages.append((int(channel), int(message)))
        if self._delete_message_error is not None:
            raise self._delete_message_error

    async def delete_webhook(self, webhook, *, token=None):
        self.deleted_webhooks.append((str(webhook), token))
        if self._delete_webhook_error is not None:
            raise self._delete_webhook_error

    async def fetch_channel(self, channel):
        self.fetched_channels.append(int(channel))
        if self._fetch_channel_error is not None:
            raise self._fetch_channel_error
        return self._channel

    def _role_objects(self) -> dict:
        return {
            role_id: SimpleNamespace(id=role_id, permissions=permissions)
            for role_id, permissions in self.roles.items()
        }

    async def fetch_guild(self, guild):
        if self._guild_error is not None:
            raise self._guild_error
        return SimpleNamespace(owner_id=OWNER_ID, roles=self._role_objects())

    async def fetch_roles(self, guild):
        return list(self._role_objects().values())

    async def fetch_guild_channels(self, guild):
        return [
            SimpleNamespace(
                permission_overwrites={
                    target: SimpleNamespace(allow=allow, deny=deny)
                    for target, (allow, deny) in channel.items()
                }
            )
            for channel in self.overwrites
        ]

    async def fetch_member(self, guild, user):
        if int(user) not in self.members:
            raise _not_found()
        return SimpleNamespace(
            id=int(user), role_ids=self.members[int(user)], is_bot=False
        )

    async def fetch_user(self, user):
        return SimpleNamespace(id=int(user), is_bot=False)


def _bot(*, rest: _FakeRest, **extra) -> SimpleNamespace:
    return SimpleNamespace(
        rest=rest, get_me=lambda: SimpleNamespace(id=BOT_USER_ID), **extra
    )


def _not_found() -> hikari.NotFoundError:
    return hikari.NotFoundError(url="https://discord.test", headers={}, raw_body="")


def _forbidden() -> hikari.ForbiddenError:
    return hikari.ForbiddenError(url="https://discord.test", headers={}, raw_body="")


def _rate_limited() -> hikari.RateLimitTooLongError:
    return hikari.RateLimitTooLongError(
        route=SimpleNamespace(),
        is_global=False,
        retry_after=120.0,
        max_retry_after=10.0,
        reset_at=0.0,
        limit=1,
        period=1.0,
    )


def _make_event(
    content: str,
    *,
    is_bot: bool = False,
    role_ids: tuple[int, ...] = (),
    parent_id: int | None = CATEGORY_ID,
    member_cached: bool = True,
    channel_cached: bool = True,
) -> SimpleNamespace:
    channel = SimpleNamespace(id=CHANNEL_ID, parent_id=parent_id)
    member = (
        SimpleNamespace(id=AUTHOR_ID, role_ids=list(role_ids), is_bot=is_bot)
        if member_cached
        else None
    )
    return SimpleNamespace(
        is_bot=is_bot,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        author=SimpleNamespace(id=AUTHOR_ID, username="spammer", is_bot=is_bot),
        author_id=AUTHOR_ID,
        member=member,
        message=SimpleNamespace(id=MESSAGE_ID, content=content),
        get_channel=lambda: channel if channel_cached else None,
    )


def _config(**overrides) -> ModerationFilterConfig:
    values = {
        "guild_id": str(GUILD_ID),
        "content_filter_enabled": True,
        "blocked_tlds": [".gay"],
        "invite_filter_enabled": True,
        "invite_filter_exempt_category_ids": [],
        "staff_exempt_role_ids": [str(STAFF_ROLE_ID)],
        "webhook_killer_enabled": True,
        "mod_log_channel_id": str(MOD_LOG_CHANNEL_ID),
    }
    values.update(overrides)
    return ModerationFilterConfig(**values)


@pytest.fixture
def filter_env(monkeypatch, moderation_gateway_db):
    """Stubs the config read and the mod_action dispatch; the gateway is real,
    and so is the moderation policy read (none unless a test saves one)."""
    from smarter_dev.bot import moderation_port

    state = SimpleNamespace(config=_config(), dispatched=[])

    async def fake_get_config(session, guild_id):
        return state.config

    async def fake_dispatch(action, *, bot=None) -> None:
        state.dispatched.append((action, bot))

    @contextlib.asynccontextmanager
    async def fake_session_context():
        async with moderation_gateway_db() as session:
            yield session

    async def rows() -> list[ModerationAction]:
        async with moderation_gateway_db() as session:
            return list((await session.execute(select(ModerationAction))).scalars())

    monkeypatch.setattr(
        content_filter.moderation_filter_config_ops, "get_config", fake_get_config
    )
    monkeypatch.setattr(moderation_port, "dispatch_mod_action", fake_dispatch)
    monkeypatch.setattr(content_filter, "get_db_session_context", fake_session_context)
    state.rows = rows
    state.database = moderation_gateway_db
    return state


# --------------------------------------------------------------------------
# Listener shell: gates
# --------------------------------------------------------------------------


async def test_bot_authored_message_is_never_filtered(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event("https://evil.gay/free", is_bot=True)
    )

    assert result == ContentFilterResult()
    assert rest.deleted_messages == []


async def test_missing_config_short_circuits(filter_env):
    filter_env.config = None
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event("https://evil.gay/free")
    )

    assert result.took_action is False
    assert rest.deleted_messages == []


async def test_disabled_content_filter_short_circuits(filter_env):
    filter_env.config = _config(content_filter_enabled=False)
    rest = _FakeRest()

    await check_content_filters(
        _bot(rest=rest), _make_event(f"https://evil.gay/free {WEBHOOK_URL}")
    )

    assert rest.deleted_messages == []
    assert rest.deleted_webhooks == []


# --------------------------------------------------------------------------
# Listener shell: blocked TLD
# --------------------------------------------------------------------------


async def test_blocked_tld_deletes_notifies_logs_and_records_action(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event("free stuff https://evil.gay/free")
    )

    assert result.blocked_tld_links == ("https://evil.gay/free",)
    assert result.message_deleted is True
    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]

    channel_notice = next(m for m in rest.created_messages if m[0] == CHANNEL_ID)
    assert f"<@{AUTHOR_ID}>" in channel_notice[1]
    assert channel_notice[2]["user_mentions"] == [AUTHOR_ID]

    report = next(m for m in rest.created_messages if m[0] == MOD_LOG_CHANNEL_ID)
    assert "https://evil.gay" not in report[1]
    assert report[2]["user_mentions"] is False
    assert report[2]["mentions_everyone"] is False

    [action] = await filter_env.rows()
    assert action.action_type == "delete"
    assert action.source == "handler"
    assert action.actor == "content_filter"
    assert action.guild_id == str(GUILD_ID)
    assert action.target_user_id == str(AUTHOR_ID)
    assert action.trigger_message_id == str(MESSAGE_ID)
    assert "https://evil.gay" not in action.reason
    assert len(filter_env.dispatched) == 1


async def test_filter_delete_dispatches_with_the_bot_so_it_reaches_the_event_log(
    filter_env,
):
    """The chat agent has to be able to own a filter delete it made."""
    bot = _bot(rest=_FakeRest(), d={})

    await check_content_filters(bot, _make_event("https://evil.gay/free"))

    assert len(filter_env.dispatched) == 1
    _, dispatched_bot = filter_env.dispatched[0]
    assert dispatched_bot is bot


async def test_blocked_tld_without_mod_log_channel_still_deletes(filter_env):
    filter_env.config = _config(mod_log_channel_id=None)
    rest = _FakeRest()

    await check_content_filters(
        _bot(rest=rest), _make_event("https://evil.gay/free")
    )

    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
    assert [m[0] for m in rest.created_messages] == [CHANNEL_ID]


async def test_staff_role_author_is_exempt_from_every_filter(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest),
        _make_event(
            f"https://evil.gay/free discord.gg/abc123 {WEBHOOK_URL}",
            role_ids=(STAFF_ROLE_ID,),
        ),
    )

    assert result.took_action is False
    assert rest.deleted_messages == []
    assert rest.deleted_webhooks == []
    assert filter_env.dispatched == []


EVERY_FILTER = f"https://evil.gay/free discord.gg/abc123 {WEBHOOK_URL}"


#: In the guild's moderation policy only.
POLICY_ROLE_ID = 1100000000000004242
POLICY_REPORT_CHANNEL_ID = 1100000000000007777


async def _save_policy(filter_env, **changes) -> None:
    document = mp.default_document().as_json()
    for section, values in changes.items():
        if isinstance(values, dict):
            document[section].update(values)
        else:
            document[section] = values
    async with filter_env.database() as session:
        await store.save_policy(
            session,
            str(GUILD_ID),
            document,
            base_revision=None,
            by="test",
            source=store.SOURCE_ADMIN,
        )


@pytest.mark.parametrize(
    ("role_id", "protected"), [(POLICY_ROLE_ID, True), (STAFF_ROLE_ID, False)]
)
async def test_under_a_policy_its_protected_roles_are_staff_not_the_old_column(
    filter_env, role_id, protected
):
    """#241: the filter form no longer saves ``staff_exempt_role_ids`` once a
    guild has a policy, so the roles edited on the policy page must reach the
    filter, and the stale column (``STAFF_ROLE_ID``) must not."""
    await _save_policy(filter_env, protected_role_ids=[str(POLICY_ROLE_ID)])
    rest = _FakeRest(roles={role_id: hikari.Permissions.SEND_MESSAGES})
    event = _make_event(EVERY_FILTER, role_ids=(role_id,))

    result = await check_content_filters(_bot(rest=rest), event)

    if protected:
        _untouched(result, rest, filter_env)
    else:
        assert result.message_deleted is True
        assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]


@pytest.mark.parametrize("reporting", [True, False])
async def test_under_a_policy_the_report_goes_to_its_channel_not_the_old_ones(
    filter_env, reporting
):
    """#241: under a policy the incident report goes to the policy's
    ``destinations.report_channel_id``, and nowhere while its reporting is
    off; the filter row's ``mod_log_channel_id`` is no longer read for it."""
    await _save_policy(
        filter_env,
        destinations={"report_channel_id": str(POLICY_REPORT_CHANNEL_ID)},
        controls={"report": reporting},
    )
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event("free stuff https://evil.gay/free")
    )

    assert result.message_deleted is True
    posted_to = {m[0] for m in rest.created_messages}
    assert MOD_LOG_CHANNEL_ID not in posted_to
    assert (POLICY_REPORT_CHANNEL_ID in posted_to) is reporting
    assert posted_to - {POLICY_REPORT_CHANNEL_ID} == {CHANNEL_ID}


def _untouched(result: ContentFilterResult, rest: _FakeRest, filter_env) -> None:
    assert result.took_action is False
    assert rest.deleted_messages == []
    assert rest.deleted_webhooks == []
    assert rest.created_messages == []
    assert filter_env.dispatched == []


async def test_hardcoded_staff_role_is_exempt_without_any_configured_role(filter_env):
    # A staff-role moderator with only Moderate Members, and no exempt roles set.
    filter_env.config = _config(staff_exempt_role_ids=[])
    rest = _FakeRest(roles={HARDCODED_STAFF_ROLE_ID: hikari.Permissions.MODERATE_MEMBERS})
    event = _make_event(EVERY_FILTER, role_ids=(HARDCODED_STAFF_ROLE_ID,))

    result = await check_content_filters(_bot(rest=rest), event)

    _untouched(result, rest, filter_env)


@pytest.mark.parametrize(
    "permission",
    [
        hikari.Permissions.MANAGE_MESSAGES,
        hikari.Permissions.MODERATE_MEMBERS,
        hikari.Permissions.KICK_MEMBERS,
        hikari.Permissions.ADMINISTRATOR,
    ],
)
async def test_staff_permission_author_is_exempt_from_every_filter(
    filter_env, permission
):
    rest = _FakeRest(roles={42: permission})
    event = _make_event(EVERY_FILTER, role_ids=(42,))

    result = await check_content_filters(_bot(rest=rest), event)

    _untouched(result, rest, filter_env)


async def test_staff_permission_in_one_channel_does_not_exempt(filter_env):
    # #303: an overwrite makes nobody staff.
    rest = _FakeRest(
        overwrites=[
            {},
            {AUTHOR_ID: (hikari.Permissions.MANAGE_MESSAGES, hikari.Permissions.NONE)},
        ]
    )

    result = await check_content_filters(_bot(rest=rest), _make_event(EVERY_FILTER))

    assert result.message_deleted is True


async def test_guild_owner_is_exempt(filter_env):
    rest = _FakeRest()
    event = _make_event(EVERY_FILTER)
    event.author.id = event.member.id = OWNER_ID

    result = await check_content_filters(_bot(rest=rest), event)

    _untouched(result, rest, filter_env)


async def test_ordinary_role_author_is_filtered(filter_env):
    rest = _FakeRest(roles={42: hikari.Permissions.SEND_MESSAGES})
    event = _make_event("https://evil.gay/free", role_ids=(42,))

    result = await check_content_filters(_bot(rest=rest), event)

    assert result.message_deleted is True
    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]


async def test_uncached_member_is_read_over_rest_and_filtered(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest),
        _make_event("https://evil.gay/free", member_cached=False),
    )

    assert result.blocked_tld_links == ("https://evil.gay/free",)
    assert result.message_deleted is True


async def test_an_author_whose_standing_cannot_be_checked_is_left_alone(filter_env):
    # Like every automated actor: unknown counts as protected.
    rest = _FakeRest(guild_error=_forbidden())

    result = await check_content_filters(
        _bot(rest=rest), _make_event("https://evil.gay/free")
    )

    _untouched(result, rest, filter_env)


async def test_clean_message_never_asks_the_staff_rule(filter_env):
    rest = _FakeRest(guild_error=AssertionError("the staff rule ran"))

    result = await check_content_filters(_bot(rest=rest), _make_event("hello there"))

    assert result == ContentFilterResult()


# --------------------------------------------------------------------------
# Listener shell: invites
# --------------------------------------------------------------------------


async def test_invite_link_deletes_notifies_and_logs(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event("join discord.gg/abc123")
    )

    assert result.invite_links == ("discord.gg/abc123",)
    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
    assert any(m[0] == MOD_LOG_CHANNEL_ID for m in rest.created_messages)
    assert [row.action_type for row in await filter_env.rows()] == ["delete"]


async def test_invite_in_exempt_category_is_allowed(filter_env):
    filter_env.config = _config(invite_filter_exempt_category_ids=[str(CATEGORY_ID)])
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event("join discord.gg/abc123")
    )

    assert result.took_action is False
    assert rest.deleted_messages == []


async def test_invite_category_is_resolved_by_rest_when_channel_is_uncached(filter_env):
    filter_env.config = _config(invite_filter_exempt_category_ids=[str(CATEGORY_ID)])
    rest = _FakeRest(channel=SimpleNamespace(id=CHANNEL_ID, parent_id=CATEGORY_ID))

    result = await check_content_filters(
        _bot(rest=rest),
        _make_event("join discord.gg/abc123", channel_cached=False),
    )

    assert rest.fetched_channels == [CHANNEL_ID]
    assert result.took_action is False


@pytest.mark.parametrize(
    "fetch_error", [_forbidden(), _rate_limited(), _not_found()], ids=["403", "429", "404"]
)
async def test_unresolvable_exemption_channel_never_aborts_the_other_filters(
    filter_env, fetch_error
):
    filter_env.config = _config(invite_filter_exempt_category_ids=[str(CATEGORY_ID)])
    rest = _FakeRest(fetch_channel_error=fetch_error)

    result = await check_content_filters(
        _bot(rest=rest),
        _make_event(
            f"https://evil.gay/free discord.gg/abc123 {WEBHOOK_URL}",
            channel_cached=False,
        ),
    )

    # Fail closed: an unprovable exemption is no exemption, and every other
    # filter still runs.
    assert result.blocked_tld_links == ("https://evil.gay/free",)
    assert result.invite_links == ("discord.gg/abc123",)
    assert result.killed_webhook_urls == (WEBHOOK_URL,)
    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
    assert rest.deleted_webhooks == [("123456789", "abcDEF-_token")]


async def test_disabled_invite_filter_leaves_invites_alone(filter_env):
    filter_env.config = _config(invite_filter_enabled=False)
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event("join discord.gg/abc123")
    )

    assert result.took_action is False
    assert rest.deleted_messages == []


# --------------------------------------------------------------------------
# Listener shell: webhook killer
# --------------------------------------------------------------------------


async def test_leaked_webhook_is_killed_audited_logged_and_the_message_removed(
    filter_env,
):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event(f"oops {WEBHOOK_URL} {WEBHOOK_URL}")
    )

    # The duplicate URL is killed once.
    assert rest.deleted_webhooks == [("123456789", "abcDEF-_token")]
    assert result.killed_webhook_urls == (WEBHOOK_URL,)
    # The URL is a standing hazard, so the message goes with the webhook.
    assert result.message_deleted is True
    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]

    channel_notices = [m for m in rest.created_messages if m[0] == CHANNEL_ID]
    assert len(channel_notices) == 1
    assert f"<@{AUTHOR_ID}>" in channel_notices[0][1]

    report = next(m for m in rest.created_messages if m[0] == MOD_LOG_CHANNEL_ID)
    assert "spammer" in report[1]
    assert f"<#{CHANNEL_ID}>" in report[1]
    assert "123456789" in report[1]
    assert "abcDEF-_token" not in report[1]

    [action] = await filter_env.rows()
    assert action.action_type == "delete"
    assert action.source == "handler"
    assert action.target_user_id == str(AUTHOR_ID)
    assert action.trigger_message_id == str(MESSAGE_ID)
    assert "123456789" in action.reason
    assert "abcDEF-_token" not in action.reason
    assert len(filter_env.dispatched) == 1


async def test_already_dead_webhook_is_still_audited_and_removed(filter_env):
    rest = _FakeRest(delete_webhook_error=_not_found())

    result = await check_content_filters(
        _bot(rest=rest), _make_event(f"oops {WEBHOOK_URL}")
    )

    assert result.already_dead_webhook_urls == (WEBHOOK_URL,)
    assert result.message_deleted is True
    assert "123456789" in (await filter_env.rows())[0].reason


@pytest.mark.parametrize(
    "leaked_url",
    [
        "discord.com/api/webhooks/123456789/abcDEF-_token",
        "HTTPS://discord.com/api/webhooks/123456789/abcDEF-_token",
        "https://DISCORD.com/api/webhooks/123456789/abcDEF-_token",
        "https://discord.com/api/webhooks/123456789/abcDEF-_token?wait=true",
    ],
)
async def test_common_leak_shapes_are_killed_not_silently_rejected(
    filter_env, leaked_url
):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event(f"oops {leaked_url}")
    )

    assert rest.deleted_webhooks == [("123456789", "abcDEF-_token")]
    assert result.killed_webhook_urls == (leaked_url,)
    assert result.rejected_webhook_urls == ()
    assert any(m[0] == CHANNEL_ID for m in rest.created_messages)


async def test_webhook_shaped_link_on_a_foreign_host_deletes_nothing(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest),
        _make_event("leak https://DISCORD.COM.evil.com/api/webhooks/1/tok"),
    )

    assert rest.deleted_webhooks == []
    assert rest.deleted_messages == []
    assert result.rejected_webhook_urls == (
        "https://DISCORD.COM.evil.com/api/webhooks/1/tok",
    )
    assert filter_env.dispatched == []


async def test_non_discord_webhook_url_is_rejected_without_any_rest_call(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        _bot(rest=rest),
        _make_event("leak https://evil.example.com/api/webhooks/1/tok"),
    )

    assert rest.deleted_webhooks == []
    assert result.killed_webhook_urls == ()
    assert result.rejected_webhook_urls == ("https://evil.example.com/api/webhooks/1/tok",)
    assert rest.created_messages == []


async def test_webhook_delete_404_is_an_expected_outcome_not_a_crash(filter_env):
    rest = _FakeRest(delete_webhook_error=_not_found())

    result = await check_content_filters(
        _bot(rest=rest), _make_event(f"oops {WEBHOOK_URL}")
    )

    assert result.killed_webhook_urls == ()
    assert result.already_dead_webhook_urls == (WEBHOOK_URL,)
    assert len([m for m in rest.created_messages if m[0] == CHANNEL_ID]) == 1


async def test_webhook_delete_401_is_treated_as_a_dead_webhook_not_a_crash(filter_env):
    rest = _FakeRest(
        delete_webhook_error=hikari.UnauthorizedError(
            url="https://discord.test", headers={}, raw_body=""
        )
    )

    result = await check_content_filters(
        _bot(rest=rest), _make_event(f"oops {WEBHOOK_URL}")
    )

    assert result.killed_webhook_urls == ()
    assert result.already_dead_webhook_urls == (WEBHOOK_URL,)


async def test_disabled_webhook_killer_leaves_the_webhook_alive(filter_env):
    filter_env.config = _config(webhook_killer_enabled=False)
    rest = _FakeRest()

    await check_content_filters(
        _bot(rest=rest), _make_event(f"oops {WEBHOOK_URL}")
    )

    assert rest.deleted_webhooks == []


# --------------------------------------------------------------------------
# Listener shell: failure paths
# --------------------------------------------------------------------------


async def test_already_deleted_message_does_not_abort_the_remaining_filters(filter_env):
    rest = _FakeRest(delete_message_error=_not_found())

    result = await check_content_filters(
        _bot(rest=rest),
        _make_event(f"https://evil.gay/free discord.gg/abc123 {WEBHOOK_URL}"),
    )

    assert result.message_deleted is False
    assert result.blocked_tld_links == ("https://evil.gay/free",)
    assert result.invite_links == ("discord.gg/abc123",)
    # The webhook kill still happened even though the delete 404'd first.
    assert rest.deleted_webhooks == [("123456789", "abcDEF-_token")]


async def test_one_delete_serves_both_content_filters(filter_env):
    rest = _FakeRest()

    await check_content_filters(
        _bot(rest=rest),
        _make_event("https://evil.gay/free discord.gg/abc123"),
    )

    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
    # One delete, one row, whose reason names both filters.
    [action] = await filter_env.rows()
    assert "Blocked TLD" in action.reason and "Invite" in action.reason


async def test_missing_send_permission_is_logged_not_raised(filter_env):
    class _ForbiddenRest(_FakeRest):
        async def create_message(self, channel, content=None, **kwargs):
            raise hikari.ForbiddenError(
                url="https://discord.test", headers={}, raw_body=""
            )

    rest = _ForbiddenRest()

    result = await check_content_filters(
        _bot(rest=rest), _make_event("https://evil.gay/free")
    )

    assert result.message_deleted is True


async def test_no_delete_without_a_durable_record(filter_env, monkeypatch):
    """The gateway writes its intent before any delete; no database, no delete."""
    from smarter_dev.web.moderation_gateway import gateway as gateway_module

    def broken_database():
        raise DatabaseOperationError("boom")

    monkeypatch.setattr(gateway_module, "SESSION_FACTORY_OVERRIDE", broken_database)
    rest = _FakeRest()

    with pytest.raises(DatabaseOperationError):
        await check_content_filters(
            _bot(rest=rest), _make_event("https://evil.gay/free")
        )

    assert rest.deleted_messages == []


async def test_a_message_another_detector_removed_gets_no_second_notice(filter_env):
    rest = _FakeRest()
    await check_content_filters(_bot(rest=rest), _make_event("https://evil.gay/free"))
    notices = [m for m in rest.created_messages if m[0] == CHANNEL_ID]

    await check_content_filters(_bot(rest=rest), _make_event("https://evil.gay/free"))

    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
    assert [m for m in rest.created_messages if m[0] == CHANNEL_ID] == notices


# --------------------------------------------------------------------------
# Skip logging (#168): an unconfigured guild says so once, not per message
# --------------------------------------------------------------------------


def _skip_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "smarter_dev.bot.moderation_config_skips"
        and record.getMessage().startswith("Content filters:")
    ]


async def test_missing_config_is_logged_once_per_guild_at_info(filter_env, caplog):
    content_filter.config_skip_log.reset()
    caplog.set_level(logging.INFO, logger="smarter_dev.bot.moderation_config_skips")
    filter_env.config = None
    bot = _bot(rest=_FakeRest())

    for _ in range(5):
        await check_content_filters(bot, _make_event("https://evil.gay/free"))

    assert _skip_lines(caplog) == [
        f"Content filters: skipping guild {GUILD_ID}, "
        "which has no moderation_filter_configs row."
    ]


async def test_skip_reason_change_and_rerun_are_logged_again(filter_env, caplog):
    content_filter.config_skip_log.reset()
    caplog.set_level(logging.INFO, logger="smarter_dev.bot.moderation_config_skips")
    bot = _bot(rest=_FakeRest())
    missing_line = (
        f"Content filters: skipping guild {GUILD_ID}, "
        "which has no moderation_filter_configs row."
    )
    disabled_line = (
        f"Content filters: skipping guild {GUILD_ID}, "
        "which has content_filter_enabled switched off."
    )

    filter_env.config = None
    await check_content_filters(bot, _make_event("hello there"))
    filter_env.config = _config(content_filter_enabled=False)
    await check_content_filters(bot, _make_event("hello there"))
    await check_content_filters(bot, _make_event("hello again"))
    filter_env.config = _config()
    await check_content_filters(bot, _make_event("hello there"))
    filter_env.config = _config(content_filter_enabled=False)
    await check_content_filters(bot, _make_event("hello there"))

    assert _skip_lines(caplog) == [missing_line, disabled_line, disabled_line]
