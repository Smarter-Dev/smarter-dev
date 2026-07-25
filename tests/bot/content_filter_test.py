"""Tests for the bot-core content filters (blocked TLD / webhook killer / invites).

The pure layer (link extraction, TLD matching, webhook-URL validation, invite
detection, sanitization, notice + mod-log formatting) is exercised directly with
plain strings — no Discord, no database. The listener shell
(:func:`check_content_filters`) is then tested over fakes to prove the wiring:
every toggle, every exemption, and the failure paths that must not abort the
remaining filters.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import hikari
import pytest

from smarter_dev.bot import content_filter
from smarter_dev.bot.content_filter import ContentFilterResult
from smarter_dev.bot.content_filter import author_is_staff_exempt
from smarter_dev.bot.content_filter import build_blocked_tld_notice
from smarter_dev.bot.content_filter import build_invite_notice
from smarter_dev.bot.content_filter import build_mod_log_line
from smarter_dev.bot.content_filter import build_webhook_kill_mod_log_line
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
from smarter_dev.bot.content_filter import member_has_manage_messages
from smarter_dev.bot.content_filter import normalize_webhook_url
from smarter_dev.bot.content_filter import parse_discord_webhook_url
from smarter_dev.bot.content_filter import sanitize_links
from smarter_dev.bot.content_filter import truncate_content
from smarter_dev.web.crud import DatabaseOperationError
from smarter_dev.web.models import ModerationFilterConfig

GUILD_ID = 555
CHANNEL_ID = 777
CATEGORY_ID = 888
AUTHOR_ID = 111
MESSAGE_ID = 999
STAFF_ROLE_ID = 222
MOD_LOG_CHANNEL_ID = 333

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


def test_sanitize_links_makes_links_unclickable_and_leaves_prose_alone():
    sanitized = sanitize_links("look at https://evil.gay/free now")

    assert "https://evil.gay" not in sanitized
    assert sanitized == "look at https:// evil . gay/free now"


def test_truncate_content_keeps_short_text_and_caps_long_text():
    assert truncate_content("short", limit=800) == "short"

    truncated = truncate_content("x" * 900, limit=800)

    assert len(truncated) == 800
    assert truncated.endswith("…")


def test_build_mod_log_line_reports_author_channel_and_sanitized_content():
    line = build_mod_log_line(
        filter_name="Blocked TLD",
        author_id=str(AUTHOR_ID),
        author_username="spammer",
        channel_id=str(CHANNEL_ID),
        links=["https://evil.gay/free"],
        content="buy now https://evil.gay/free " + "z" * 900,
    )

    assert "Blocked TLD" in line
    assert "spammer" in line
    assert str(AUTHOR_ID) in line
    assert f"<#{CHANNEL_ID}>" in line
    # No clickable form of the link survives anywhere in the log line.
    assert "https://evil.gay" not in line
    assert "https:// evil . gay/free" in line
    assert "…" in line


def test_build_mod_log_line_never_republishes_a_leaked_webhook_token():
    line = build_mod_log_line(
        filter_name="Blocked TLD",
        author_id=str(AUTHOR_ID),
        author_username="spammer",
        channel_id=str(CHANNEL_ID),
        links=["https://evil.gay/free"],
        content=f"free stuff https://evil.gay/free and {WEBHOOK_URL}",
    )

    assert "abcDEF-_token" not in line
    # The id survives so a moderator can still identify the webhook.
    assert "123456789" in line


def test_notice_builders_mention_the_author_and_describe_the_kill():
    assert f"<@{AUTHOR_ID}>" in build_blocked_tld_notice(str(AUTHOR_ID))
    assert f"<@{AUTHOR_ID}>" in build_invite_notice(str(AUTHOR_ID))

    notice = build_webhook_killed_notice(
        author_id=str(AUTHOR_ID), killed_count=1, already_dead_count=1
    )

    assert f"<@{AUTHOR_ID}>" in notice
    assert "1" in notice
    assert "webhook" in notice.lower()


def test_build_webhook_kill_mod_log_line_names_ids_but_never_tokens():
    line = build_webhook_kill_mod_log_line(
        author_id=str(AUTHOR_ID),
        author_username="spammer",
        channel_id=str(CHANNEL_ID),
        webhook_ids=["123456789"],
    )

    assert "spammer" in line
    assert str(AUTHOR_ID) in line
    assert f"<#{CHANNEL_ID}>" in line
    assert "123456789" in line
    assert "abcDEF-_token" not in line


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


def test_author_is_staff_exempt_compares_ids_as_strings():
    assert author_is_staff_exempt(["222"], ["222", "333"]) is True
    assert author_is_staff_exempt(["999"], ["222"]) is False
    assert author_is_staff_exempt([], ["222"]) is False


def test_category_is_invite_exempt_handles_missing_parent():
    assert category_is_invite_exempt("888", ["888"]) is True
    assert category_is_invite_exempt("777", ["888"]) is False
    assert category_is_invite_exempt(None, ["888"]) is False


def _role(permissions: hikari.Permissions) -> SimpleNamespace:
    return SimpleNamespace(permissions=permissions)


def test_member_has_manage_messages_via_role_owner_and_administrator():
    manage_role = _role(hikari.Permissions.MANAGE_MESSAGES)
    admin_role = _role(hikari.Permissions.ADMINISTRATOR)
    plain_role = _role(hikari.Permissions.SEND_MESSAGES)

    def guild_with(roles: dict) -> SimpleNamespace:
        return SimpleNamespace(owner_id=1, get_role=roles.get)

    member = SimpleNamespace(id=AUTHOR_ID, role_ids=[7])

    assert member_has_manage_messages(guild_with({7: manage_role}), member) is True
    assert member_has_manage_messages(guild_with({7: admin_role}), member) is True
    assert member_has_manage_messages(guild_with({7: plain_role}), member) is False
    # Unknown role id resolves to None and must not explode.
    assert member_has_manage_messages(guild_with({}), member) is False

    owner = SimpleNamespace(id=1, role_ids=[])
    assert member_has_manage_messages(guild_with({}), owner) is True


def test_member_has_manage_messages_fails_closed_without_a_member():
    guild = SimpleNamespace(owner_id=1, get_role=lambda role_id: None)

    assert member_has_manage_messages(guild, None) is False
    assert member_has_manage_messages(None, SimpleNamespace(id=2, role_ids=[])) is False


# --------------------------------------------------------------------------
# Listener shell fakes
# --------------------------------------------------------------------------


class _FakeRest:
    """Records every REST call the filters make, optionally failing them."""

    def __init__(
        self,
        *,
        delete_message_error: Exception | None = None,
        delete_webhook_error: Exception | None = None,
        fetch_channel_error: Exception | None = None,
        channel: object | None = None,
    ) -> None:
        self.created_messages: list[tuple[int, str, dict]] = []
        self.deleted_messages: list[tuple[int, int]] = []
        self.deleted_webhooks: list[tuple[str, str]] = []
        self.fetched_channels: list[int] = []
        self._delete_message_error = delete_message_error
        self._delete_webhook_error = delete_webhook_error
        self._fetch_channel_error = fetch_channel_error
        self._channel = channel

    async def create_message(self, channel, content=None, **kwargs):
        self.created_messages.append((int(channel), content, kwargs))
        return SimpleNamespace(id=1)

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
    roles: dict | None = None,
    parent_id: int | None = CATEGORY_ID,
    member_cached: bool = True,
    channel_cached: bool = True,
) -> SimpleNamespace:
    guild = SimpleNamespace(owner_id=1, get_role=(roles or {}).get)
    channel = SimpleNamespace(id=CHANNEL_ID, parent_id=parent_id)
    member = (
        SimpleNamespace(id=AUTHOR_ID, role_ids=list(role_ids)) if member_cached else None
    )
    return SimpleNamespace(
        is_bot=is_bot,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        author=SimpleNamespace(id=AUTHOR_ID, username="spammer", is_bot=is_bot),
        author_id=AUTHOR_ID,
        member=member,
        message=SimpleNamespace(id=MESSAGE_ID, content=content),
        get_guild=lambda: guild,
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
def filter_env(monkeypatch):
    """Stubs the config read, the action write, and the mod_action dispatch."""
    state = SimpleNamespace(
        config=_config(),
        create_action_error=None,
        recorded_actions=[],
        dispatched=[],
        commits=0,
    )

    async def fake_get_config(session, guild_id):
        return state.config

    async def fake_create_action(session, **kwargs):
        if state.create_action_error is not None:
            raise state.create_action_error
        state.recorded_actions.append(kwargs)
        return SimpleNamespace(**kwargs)

    async def fake_dispatch(action) -> None:
        state.dispatched.append(action)

    async def fake_commit() -> None:
        state.commits += 1

    @contextlib.asynccontextmanager
    async def fake_session_context():
        yield SimpleNamespace(commit=fake_commit)

    monkeypatch.setattr(
        content_filter.moderation_filter_config_ops, "get_config", fake_get_config
    )
    monkeypatch.setattr(content_filter.mod_action_ops, "create_action", fake_create_action)
    monkeypatch.setattr(content_filter, "dispatch_mod_action", fake_dispatch)
    monkeypatch.setattr(content_filter, "get_db_session_context", fake_session_context)
    return state


# --------------------------------------------------------------------------
# Listener shell: gates
# --------------------------------------------------------------------------


async def test_bot_authored_message_is_never_filtered(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest), _make_event("https://evil.gay/free", is_bot=True)
    )

    assert result == ContentFilterResult()
    assert rest.deleted_messages == []


async def test_missing_config_short_circuits(filter_env):
    filter_env.config = None
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest), _make_event("https://evil.gay/free")
    )

    assert result.took_action is False
    assert rest.deleted_messages == []


async def test_disabled_content_filter_short_circuits(filter_env):
    filter_env.config = _config(content_filter_enabled=False)
    rest = _FakeRest()

    await check_content_filters(
        SimpleNamespace(rest=rest), _make_event(f"https://evil.gay/free {WEBHOOK_URL}")
    )

    assert rest.deleted_messages == []
    assert rest.deleted_webhooks == []


# --------------------------------------------------------------------------
# Listener shell: blocked TLD
# --------------------------------------------------------------------------


async def test_blocked_tld_deletes_notifies_logs_and_records_action(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest), _make_event("free stuff https://evil.gay/free")
    )

    assert result.blocked_tld_links == ("https://evil.gay/free",)
    assert result.message_deleted is True
    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]

    channel_notice = next(m for m in rest.created_messages if m[0] == CHANNEL_ID)
    assert f"<@{AUTHOR_ID}>" in channel_notice[1]
    assert channel_notice[2]["user_mentions"] == [AUTHOR_ID]

    mod_log = next(m for m in rest.created_messages if m[0] == MOD_LOG_CHANNEL_ID)
    assert "https://evil.gay" not in mod_log[1]
    assert mod_log[2]["user_mentions"] is False
    assert mod_log[2]["mentions_everyone"] is False

    assert len(filter_env.recorded_actions) == 1
    action = filter_env.recorded_actions[0]
    assert action["source"] == "handler"
    assert action["guild_id"] == str(GUILD_ID)
    assert action["target_user_id"] == str(AUTHOR_ID)
    assert action["trigger_message_id"] == str(MESSAGE_ID)
    assert "https://evil.gay" not in action["reason"]
    assert filter_env.commits == 1
    assert len(filter_env.dispatched) == 1


async def test_blocked_tld_without_mod_log_channel_still_deletes(filter_env):
    filter_env.config = _config(mod_log_channel_id=None)
    rest = _FakeRest()

    await check_content_filters(
        SimpleNamespace(rest=rest), _make_event("https://evil.gay/free")
    )

    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
    assert [m[0] for m in rest.created_messages] == [CHANNEL_ID]


async def test_staff_role_author_is_exempt_from_every_filter(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest),
        _make_event(
            f"https://evil.gay/free discord.gg/abc123 {WEBHOOK_URL}",
            role_ids=(STAFF_ROLE_ID,),
        ),
    )

    assert result.took_action is False
    assert rest.deleted_messages == []
    assert rest.deleted_webhooks == []
    assert filter_env.recorded_actions == []


async def test_manage_messages_author_is_exempt_from_every_filter(filter_env):
    rest = _FakeRest()
    event = _make_event(
        f"https://evil.gay/free discord.gg/abc123 {WEBHOOK_URL}",
        role_ids=(42,),
        roles={42: _role(hikari.Permissions.MANAGE_MESSAGES)},
    )

    result = await check_content_filters(SimpleNamespace(rest=rest), event)

    assert result.took_action is False
    assert rest.deleted_messages == []


async def test_uncached_member_fails_closed_and_message_is_scanned(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest),
        _make_event("https://evil.gay/free", member_cached=False),
    )

    assert result.blocked_tld_links == ("https://evil.gay/free",)


# --------------------------------------------------------------------------
# Listener shell: invites
# --------------------------------------------------------------------------


async def test_invite_link_deletes_notifies_and_logs(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest), _make_event("join discord.gg/abc123")
    )

    assert result.invite_links == ("discord.gg/abc123",)
    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
    assert any(m[0] == MOD_LOG_CHANNEL_ID for m in rest.created_messages)
    assert filter_env.recorded_actions[0]["action_type"] == "delete"


async def test_invite_in_exempt_category_is_allowed(filter_env):
    filter_env.config = _config(invite_filter_exempt_category_ids=[str(CATEGORY_ID)])
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest), _make_event("join discord.gg/abc123")
    )

    assert result.took_action is False
    assert rest.deleted_messages == []


async def test_invite_category_is_resolved_by_rest_when_channel_is_uncached(filter_env):
    filter_env.config = _config(invite_filter_exempt_category_ids=[str(CATEGORY_ID)])
    rest = _FakeRest(channel=SimpleNamespace(id=CHANNEL_ID, parent_id=CATEGORY_ID))

    result = await check_content_filters(
        SimpleNamespace(rest=rest),
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
        SimpleNamespace(rest=rest),
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
        SimpleNamespace(rest=rest), _make_event("join discord.gg/abc123")
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
        SimpleNamespace(rest=rest), _make_event(f"oops {WEBHOOK_URL} {WEBHOOK_URL}")
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

    mod_log = next(m for m in rest.created_messages if m[0] == MOD_LOG_CHANNEL_ID)
    assert "spammer" in mod_log[1]
    assert f"<#{CHANNEL_ID}>" in mod_log[1]
    assert "123456789" in mod_log[1]
    assert "abcDEF-_token" not in mod_log[1]

    assert len(filter_env.recorded_actions) == 1
    action = filter_env.recorded_actions[0]
    assert action["action_type"] == "delete"
    assert action["source"] == "handler"
    assert action["target_user_id"] == str(AUTHOR_ID)
    assert action["trigger_message_id"] == str(MESSAGE_ID)
    assert "123456789" in action["reason"]
    assert "abcDEF-_token" not in action["reason"]
    assert len(filter_env.dispatched) == 1


async def test_already_dead_webhook_is_still_audited_and_removed(filter_env):
    rest = _FakeRest(delete_webhook_error=_not_found())

    result = await check_content_filters(
        SimpleNamespace(rest=rest), _make_event(f"oops {WEBHOOK_URL}")
    )

    assert result.already_dead_webhook_urls == (WEBHOOK_URL,)
    assert result.message_deleted is True
    assert "123456789" in filter_env.recorded_actions[0]["reason"]


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
        SimpleNamespace(rest=rest), _make_event(f"oops {leaked_url}")
    )

    assert rest.deleted_webhooks == [("123456789", "abcDEF-_token")]
    assert result.killed_webhook_urls == (leaked_url,)
    assert result.rejected_webhook_urls == ()
    assert any(m[0] == CHANNEL_ID for m in rest.created_messages)


async def test_webhook_shaped_link_on_a_foreign_host_deletes_nothing(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest),
        _make_event("leak https://DISCORD.COM.evil.com/api/webhooks/1/tok"),
    )

    assert rest.deleted_webhooks == []
    assert rest.deleted_messages == []
    assert result.rejected_webhook_urls == (
        "https://DISCORD.COM.evil.com/api/webhooks/1/tok",
    )
    assert filter_env.recorded_actions == []


async def test_non_discord_webhook_url_is_rejected_without_any_rest_call(filter_env):
    rest = _FakeRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest),
        _make_event("leak https://evil.example.com/api/webhooks/1/tok"),
    )

    assert rest.deleted_webhooks == []
    assert result.killed_webhook_urls == ()
    assert result.rejected_webhook_urls == ("https://evil.example.com/api/webhooks/1/tok",)
    assert rest.created_messages == []


async def test_webhook_delete_404_is_an_expected_outcome_not_a_crash(filter_env):
    rest = _FakeRest(delete_webhook_error=_not_found())

    result = await check_content_filters(
        SimpleNamespace(rest=rest), _make_event(f"oops {WEBHOOK_URL}")
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
        SimpleNamespace(rest=rest), _make_event(f"oops {WEBHOOK_URL}")
    )

    assert result.killed_webhook_urls == ()
    assert result.already_dead_webhook_urls == (WEBHOOK_URL,)


async def test_disabled_webhook_killer_leaves_the_webhook_alive(filter_env):
    filter_env.config = _config(webhook_killer_enabled=False)
    rest = _FakeRest()

    await check_content_filters(
        SimpleNamespace(rest=rest), _make_event(f"oops {WEBHOOK_URL}")
    )

    assert rest.deleted_webhooks == []


# --------------------------------------------------------------------------
# Listener shell: failure paths
# --------------------------------------------------------------------------


async def test_already_deleted_message_does_not_abort_the_remaining_filters(filter_env):
    rest = _FakeRest(delete_message_error=_not_found())

    result = await check_content_filters(
        SimpleNamespace(rest=rest),
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
        SimpleNamespace(rest=rest),
        _make_event("https://evil.gay/free discord.gg/abc123"),
    )

    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
    assert len(filter_env.recorded_actions) == 2


async def test_missing_send_permission_is_logged_not_raised(filter_env):
    class _ForbiddenRest(_FakeRest):
        async def create_message(self, channel, content=None, **kwargs):
            raise hikari.ForbiddenError(
                url="https://discord.test", headers={}, raw_body=""
            )

    rest = _ForbiddenRest()

    result = await check_content_filters(
        SimpleNamespace(rest=rest), _make_event("https://evil.gay/free")
    )

    assert result.message_deleted is True


async def test_database_failure_recording_the_action_is_not_swallowed(filter_env):
    filter_env.create_action_error = DatabaseOperationError("boom")
    rest = _FakeRest()

    with pytest.raises(DatabaseOperationError):
        await check_content_filters(
            SimpleNamespace(rest=rest), _make_event("https://evil.gay/free")
        )

    # The user-visible enforcement still happened before the write failed.
    assert rest.deleted_messages == [(CHANNEL_ID, MESSAGE_ID)]
