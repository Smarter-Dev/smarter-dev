"""Tests for the event-dispatch cheap-guard cache."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import hikari

from smarter_dev.bot.plugins import handler_events
from smarter_dev.bot.plugins.handler_events import (
    ActiveChannelsCache,
    _snowflake_created_at,
    author_has_manage_messages,
    dispatch_message,
    message_context,
    message_interaction_user_id,
    resolve_channel_parent_id,
    serialize_message_embeds,
)


class _Resp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeAPI:
    def __init__(
        self,
        channels,
        guild_triggers=None,
        bot_message_channels=None,
        bot_message_guild_triggers=None,
    ):
        self.channels = channels
        self.guild_triggers = guild_triggers or []
        self.bot_message_channels = bot_message_channels or []
        self.bot_message_guild_triggers = bot_message_guild_triggers or []
        self.get_calls = 0

    async def get(self, path):
        self.get_calls += 1
        return _Resp({
            "channels": self.channels,
            "guild_triggers": self.guild_triggers,
            "bot_message_channels": self.bot_message_channels,
            "bot_message_guild_triggers": self.bot_message_guild_triggers,
        })


async def test_cache_reports_channel_membership():
    api = _FakeAPI([["C1", "message"], ["C2", "reaction"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    assert await cache.has(api, "C1", "G1", "message") is True
    assert await cache.has(api, "C1", "G1", "reaction") is False
    assert await cache.has(api, "C9", "G1", "message") is False


async def test_cache_reports_guild_wide_admin_trigger():
    # An all-channel admin handler => every channel in that guild dispatches.
    api = _FakeAPI(channels=[], guild_triggers=[["G1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    assert await cache.has(api, "ANY", "G1", "message") is True
    assert await cache.has(api, "ANY", "G2", "message") is False  # different guild
    assert await cache.has(api, "ANY", "G1", "reaction") is False


async def test_cache_avoids_refetch_within_ttl():
    api = _FakeAPI([["C1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    await cache.has(api, "C1", "G1", "message")
    await cache.has(api, "C1", "G1", "message")
    assert api.get_calls == 1


async def test_invalidate_forces_refetch():
    api = _FakeAPI([["C1", "message"]])
    cache = ActiveChannelsCache(ttl_seconds=999)
    await cache.has(api, "C1", "G1", "message")
    cache.invalidate()
    await cache.has(api, "C1", "G1", "message")
    assert api.get_calls == 2


def test_snowflake_created_at():
    # Discord snowflake -> ISO creation time (2015+).
    iso = _snowflake_created_at(733364234141827073)
    assert iso.startswith("20")  # a real UTC timestamp


# -- message trigger context enrichment (§3.1) ------------------------------

_AUTHOR_ID = "733364234141827073"


class _Author:
    def __init__(self, user_id: str = _AUTHOR_ID, username: str = "poster"):
        self.id = int(user_id)
        self.username = username


class _Message:
    def __init__(self, content: str = "hello", message_id: str = "M1"):
        self.content = content
        self.id = message_id
        self.author = _Author()


class _Role:
    def __init__(self, permissions: hikari.Permissions):
        self.permissions = permissions


class _Member:
    """Minimal member stand-in that ``lightbulb.utils.permissions_for`` accepts."""

    def __init__(self, roles: list[_Role], member_id: int = 1, owner: bool = False):
        self._roles = roles
        self.id = member_id
        self._owner = owner

    def get_roles(self):
        return self._roles

    def get_guild(self):
        return None  # no guild in cache -> owner short-circuit is skipped


def _base_context(**overrides) -> dict:
    kwargs = {
        "author_is_bot": False,
        "author_role_ids": [],
        "author_has_manage_messages": False,
        "mentioned_user_ids": [],
        "mentioned_role_ids": [],
        "mentions_everyone": False,
        "channel_parent_id": None,
        "author_joined_at": None,
        "attachments": [],
        "embeds": [],
        "interaction_user_id": None,
        "thread_fields": {"is_thread": False},
    }
    kwargs.update(overrides)
    return message_context(_Message(), **kwargs)


def test_message_context_includes_author_roles_and_permissions():
    context = _base_context(
        author_role_ids=["644390354157568014"],
        author_has_manage_messages=author_has_manage_messages(
            _Member([_Role(hikari.Permissions.MANAGE_MESSAGES)])
        ),
    )
    assert context["author_role_ids"] == ["644390354157568014"]
    assert context["author_has_manage_messages"] is True


def test_message_context_admin_permission_implies_manage_messages():
    # ADMINISTRATOR alone (no explicit MANAGE_MESSAGES) still reads as staff.
    assert (
        author_has_manage_messages(_Member([_Role(hikari.Permissions.ADMINISTRATOR)]))
        is True
    )
    # A plain member without either permission is NOT staff.
    assert (
        author_has_manage_messages(_Member([_Role(hikari.Permissions.SEND_MESSAGES)]))
        is False
    )


def test_message_context_fails_closed_when_member_uncached():
    # No cached member => scanned, never exempted: [] roles and NOT staff.
    assert author_has_manage_messages(None) is False
    context = _base_context(author_role_ids=[], author_has_manage_messages=False)
    assert context["author_role_ids"] == []
    assert context["author_has_manage_messages"] is False


def test_message_context_lists_mentioned_user_and_role_ids():
    context = _base_context(
        mentioned_user_ids=["111", "222"],
        mentioned_role_ids=["333"],
        mentions_everyone=True,
    )
    assert context["mentioned_user_ids"] == ["111", "222"]
    assert context["mentioned_role_ids"] == ["333"]
    assert context["mentions_everyone"] is True


@dataclass
class _FakeCache:
    guild_channels: dict = field(default_factory=dict)
    threads: dict = field(default_factory=dict)

    def get_guild_channel(self, channel_id):
        return self.guild_channels.get(int(channel_id))

    def get_thread(self, channel_id):
        return self.threads.get(int(channel_id))


@dataclass
class _FakeBot:
    cache: _FakeCache


class _Channel:
    def __init__(self, channel_type, parent_id):
        self.type = channel_type
        self.parent_id = parent_id


def test_message_context_channel_parent_id_is_category_for_top_level_channel():
    cache = _FakeCache(
        guild_channels={100: _Channel(hikari.ChannelType.GUILD_TEXT, 900)}
    )
    assert resolve_channel_parent_id(_FakeBot(cache), "100") == "900"


def test_message_context_channel_parent_id_none_on_cache_miss():
    assert resolve_channel_parent_id(_FakeBot(_FakeCache()), "100") is None


def test_message_context_thread_message_resolves_parent_category():
    # A message in a thread reports the thread's parent channel's category
    # (the grandparent), not the thread id.
    cache = _FakeCache(
        threads={50: _Channel(hikari.ChannelType.GUILD_PUBLIC_THREAD, 100)},
        guild_channels={100: _Channel(hikari.ChannelType.GUILD_TEXT, 900)},
    )
    assert resolve_channel_parent_id(_FakeBot(cache), "50") == "900"
    # Parent channel uncached => None (fail closed), never the thread id.
    thread_only = _FakeCache(
        threads={50: _Channel(hikari.ChannelType.GUILD_PUBLIC_THREAD, 100)}
    )
    assert resolve_channel_parent_id(_FakeBot(thread_only), "50") is None


def test_message_context_preserves_existing_fields():
    context = _base_context(
        author_joined_at="2026-01-01T00:00:00+00:00",
        attachments=[{"url": "u", "content_type": "image/png", "filename": "f"}],
        thread_fields={"is_thread": True, "thread_id": "T1", "thread_name": "chat"},
    )
    assert context["message_content"] == "hello"
    assert context["message_id"] == "M1"
    assert context["author_id"] == _AUTHOR_ID
    assert context["author_name"] == "poster"
    assert context["author_account_created_at"] == _snowflake_created_at(
        int(_AUTHOR_ID)
    )
    assert context["author_joined_at"] == "2026-01-01T00:00:00+00:00"
    assert context["attachments"][0]["filename"] == "f"
    assert context["is_thread"] is True
    assert context["thread_id"] == "T1"


# -- Disboard bump context: embeds + interaction_user_id (E1) ---------------


def test_serialize_message_embeds_empty_for_no_embeds():
    # A plain human message carries no embeds -> empty list (never None).
    assert serialize_message_embeds([]) == []
    assert serialize_message_embeds(None) == []


def test_serialize_message_embeds_keeps_title_and_description():
    # Disboard's confirmation is detected via embeds[0]["description"].
    embed = SimpleNamespace(title="Bump", description="Bump done! :thumbsup:", fields=[])
    serialized = serialize_message_embeds([embed])
    assert serialized == [
        {"title": "Bump", "description": "Bump done! :thumbsup:", "fields": []}
    ]


def test_serialize_message_embeds_keeps_field_name_value_pairs():
    embed = SimpleNamespace(
        title=None,
        description=None,
        fields=[
            SimpleNamespace(name="Next bump", value="in 2 hours"),
            SimpleNamespace(name="Streak", value="5"),
        ],
    )
    serialized = serialize_message_embeds([embed])
    assert serialized[0]["title"] is None
    assert serialized[0]["description"] is None
    assert serialized[0]["fields"] == [
        {"name": "Next bump", "value": "in 2 hours"},
        {"name": "Streak", "value": "5"},
    ]


def test_serialize_message_embeds_truncates_long_strings():
    # A fat embed must not bloat the fire context — each string is capped at 1KB.
    embed = SimpleNamespace(title="t" * 5000, description="d" * 5000, fields=[])
    serialized = serialize_message_embeds([embed])
    assert len(serialized[0]["title"]) == 1024
    assert len(serialized[0]["description"]) == 1024


def test_message_interaction_user_id_none_for_plain_message():
    # No slash-command interaction -> None (JSON-safe null).
    assert message_interaction_user_id(SimpleNamespace(interaction=None)) is None


def test_message_interaction_user_id_reads_slash_command_invoker():
    # Disboard's /bump reply exposes the invoker via Message.interaction.user.id.
    invoker = SimpleNamespace(id=733364234141827073)
    message = SimpleNamespace(interaction=SimpleNamespace(user=invoker))
    assert message_interaction_user_id(message) == "733364234141827073"


def test_message_context_defaults_embeds_empty_and_interaction_none():
    context = _base_context()
    assert context["embeds"] == []
    assert context["interaction_user_id"] is None


def test_message_context_carries_embeds_and_interaction_user_id():
    context = _base_context(
        embeds=[{"title": None, "description": "Bump done!", "fields": []}],
        interaction_user_id="733364234141827073",
    )
    assert context["embeds"][0]["description"] == "Bump done!"
    assert context["interaction_user_id"] == "733364234141827073"


# -- bot-message opt-in cache (has_bot_message) -----------------------------


async def test_active_channels_cache_has_bot_message():
    api = _FakeAPI(
        channels=[["C1", "message"]],
        bot_message_channels=["C1"],
        bot_message_guild_triggers=["G9"],
    )
    cache = ActiveChannelsCache(ttl_seconds=999)
    # True for a channel or a guild in the bot-message sets.
    assert await cache.has_bot_message(api, "C1", "G1") is True
    assert await cache.has_bot_message(api, "ANY", "G9") is True
    # False when neither the channel nor the guild opted in.
    assert await cache.has_bot_message(api, "C2", "G1") is False


class _FailAPI:
    async def get(self, path):
        raise RuntimeError("active-channels unavailable")


async def test_has_bot_message_false_on_refresh_failure():
    cache = ActiveChannelsCache(ttl_seconds=999)
    # A refresh failure never crashes dispatch — it reads as "not opted in".
    assert await cache.has_bot_message(_FailAPI(), "C1", "G1") is False


# -- dispatch_message bot-message routing -----------------------------------

_BOT_ME_ID = 999000111222333444


def _msg_bot_and_event(*, author_id, is_human, embeds=(), interaction_user_id=None):
    author = SimpleNamespace(id=author_id, username="poster")
    interaction = (
        SimpleNamespace(user=SimpleNamespace(id=int(interaction_user_id)))
        if interaction_user_id is not None
        else None
    )
    message = SimpleNamespace(
        id=1234,
        content="Bump done!",
        author=author,
        attachments=[],
        user_mentions_ids=[],
        role_mention_ids=[],
        mentions_everyone=False,
        embeds=list(embeds),
        interaction=interaction,
    )
    bot = SimpleNamespace(
        cache=_FakeCache(),
        get_me=lambda: SimpleNamespace(id=_BOT_ME_ID),
    )
    event = SimpleNamespace(
        is_human=is_human,
        guild_id=42,
        channel_id=555,
        message=message,
        member=None,
    )
    return bot, event


@dataclass
class _DispatchCapture:
    calls: list = field(default_factory=list)

    async def __call__(
        self, channel_id, guild_id, trigger_type, context, *, bot_message=False
    ):
        self.calls.append(
            {
                "channel_id": channel_id,
                "trigger_type": trigger_type,
                "context": context,
                "bot_message": bot_message,
            }
        )


async def test_dispatch_message_ignores_own_bot_message(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # A message authored by the bot itself never dispatches, human-looking or not.
    for is_human in (True, False):
        bot, event = _msg_bot_and_event(author_id=_BOT_ME_ID, is_human=is_human)
        await dispatch_message(bot, event)
    assert capture.calls == []


async def test_dispatch_message_bot_message_dispatches_when_opted_in(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # A foreign bot's message (is_human False, not our id) dispatches via the
    # bot-message route with author_is_bot True.
    bot, event = _msg_bot_and_event(author_id=302050872383242240, is_human=False)
    await dispatch_message(bot, event)
    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["bot_message"] is True
    assert call["context"]["author_is_bot"] is True
    assert call["channel_id"] == "555"


async def test_dispatch_message_bot_message_carries_disboard_bump_fields(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # A Disboard-style /bump confirmation: an embed with the "Bump done!"
    # description plus the interaction invoker who is credited with the bump.
    embed = SimpleNamespace(title=None, description="Bump done! :thumbsup:", fields=[])
    bot, event = _msg_bot_and_event(
        author_id=302050872383242240,
        is_human=False,
        embeds=[embed],
        interaction_user_id=733364234141827073,
    )
    await dispatch_message(bot, event)
    assert len(capture.calls) == 1
    context = capture.calls[0]["context"]
    assert context["embeds"] == [
        {"title": None, "description": "Bump done! :thumbsup:", "fields": []}
    ]
    assert context["interaction_user_id"] == "733364234141827073"


async def test_dispatch_message_human_flags_not_bot_and_records_activity(monkeypatch):
    capture = _DispatchCapture()
    recorded: list = []
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    monkeypatch.setattr(
        handler_events._activity,
        "record",
        lambda g, u, at: recorded.append((g, u)),
    )
    bot, event = _msg_bot_and_event(author_id=733364234141827073, is_human=True)
    await dispatch_message(bot, event)
    assert len(capture.calls) == 1
    call = capture.calls[0]
    assert call["bot_message"] is False
    assert call["context"]["author_is_bot"] is False
    # A human message is recorded to the activity batcher.
    assert recorded == [("42", "733364234141827073")]


async def test_dispatch_message_bot_message_not_recorded_to_activity(monkeypatch):
    capture = _DispatchCapture()
    recorded: list = []
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    monkeypatch.setattr(
        handler_events._activity,
        "record",
        lambda g, u, at: recorded.append((g, u)),
    )
    bot, event = _msg_bot_and_event(author_id=302050872383242240, is_human=False)
    await dispatch_message(bot, event)
    # A bot/webhook is not a guild member — never recorded as activity.
    assert recorded == []


async def test_dispatch_message_bot_message_skipped_when_get_me_none(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # Pre-READY: get_me() is None, so the own-bot invariant can't be verified and
    # a bot message is dropped (fail closed).
    bot, event = _msg_bot_and_event(author_id=302050872383242240, is_human=False)
    bot.get_me = lambda: None
    await dispatch_message(bot, event)
    assert capture.calls == []


# ---------------------------------------------------------------------------
# dm_message — context, mutual-guild routing, and the DM listener (E1)
# ---------------------------------------------------------------------------

from smarter_dev.bot.plugins.handler_events import (
    dispatch_dm_message,
    dm_message_context,
    route_dm_guilds,
)

_DM_SNOWFLAKE = 733364234141827073


def _dm_message(content="hi there", attachments=()):
    author = SimpleNamespace(
        id=_DM_SNOWFLAKE, username="alice", display_name="Alice"
    )
    message = SimpleNamespace(
        id=987654321,
        channel_id=555000,
        content=content,
        author=author,
        # attachments are (url, filename) pairs, mirroring a hikari Attachment.
        attachments=[
            SimpleNamespace(url=url, filename=filename) for url, filename in attachments
        ],
    )
    return message, author


def test_dm_message_context_shape():
    message, author = _dm_message(
        content="need help",
        attachments=(("https://cdn/a.png", "a.png"), ("https://cdn/b.pdf", "b.pdf")),
    )
    ctx = dm_message_context(message, author)
    assert ctx["trigger_type"] == "dm_message"
    assert ctx["content"] == "need help"
    assert ctx["message_id"] == "987654321"
    assert ctx["dm_channel_id"] == "555000"
    assert ctx["author_id"] == str(_DM_SNOWFLAKE)
    assert ctx["author_username"] == "alice"
    assert ctx["author_display_name"] == "Alice"
    assert ctx["author_account_created_at"] == _snowflake_created_at(_DM_SNOWFLAKE)
    # attachment_urls carries {url, filename} dicts so a relay handler can re-post
    # the DM's files into the forum post under their original names (relay-support e).
    assert ctx["attachment_urls"] == [
        {"url": "https://cdn/a.png", "filename": "a.png"},
        {"url": "https://cdn/b.pdf", "filename": "b.pdf"},
    ]
    # A DM has no guild member, so there are NO role fields.
    assert "author_role_ids" not in ctx


def test_dm_message_context_attachment_filename_defaults_empty():
    # A hikari Attachment without a filename still yields a well-formed dict.
    author = SimpleNamespace(id=_DM_SNOWFLAKE, username="alice", display_name="Alice")
    message = SimpleNamespace(
        id=1,
        channel_id=2,
        content="",
        author=author,
        attachments=[SimpleNamespace(url="https://cdn/x.bin", filename=None)],
    )
    ctx = dm_message_context(message, author)
    assert ctx["attachment_urls"] == [{"url": "https://cdn/x.bin", "filename": ""}]


def test_dm_message_context_empty_content_and_no_attachments():
    message, author = _dm_message(content="", attachments=())
    ctx = dm_message_context(message, author)
    assert ctx["content"] == ""
    assert ctx["attachment_urls"] == []


def test_route_dm_guilds_single_mutual_guild():
    assert route_dm_guilds(["G1"], {"G1"}) == ["G1"]


def test_route_dm_guilds_multiple_intersect_only_handler_guilds():
    # Author is in G1, G2, G3 but only G1 and G3 have a dm_message handler.
    assert route_dm_guilds(["G1", "G2", "G3"], {"G1", "G3"}) == ["G1", "G3"]


def test_route_dm_guilds_none_when_no_mutual_handler_guild():
    # A guild the user isn't in never sees their DM; no handler guild -> empty.
    assert route_dm_guilds(["G9"], {"G1"}) == []
    assert route_dm_guilds([], {"G1"}) == []


class _DmCache:
    """Stand-in ActiveChannelsCache exposing only guilds_with_trigger."""

    def __init__(self, guilds):
        self._guilds = set(guilds)

    async def guilds_with_trigger(self, api, trigger_type):
        return self._guilds if trigger_type == "dm_message" else set()


class _DmBotCache:
    def __init__(self, guild_ids, member_guild_ids):
        self._guild_ids = guild_ids
        self._member_guild_ids = set(member_guild_ids)

    def get_guilds_view(self):
        return {gid: object() for gid in self._guild_ids}

    def get_member(self, guild_id, user_id):
        return object() if guild_id in self._member_guild_ids else None


async def test_dm_listener_ignores_bot_author(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    monkeypatch.setattr(handler_events, "_cache", _DmCache({"G1"}))
    monkeypatch.setattr(handler_events, "_get_api_client", lambda: object())
    message, _ = _dm_message()
    bot = SimpleNamespace(cache=_DmBotCache([1], [1]))
    event = SimpleNamespace(is_human=False, message=message)
    await dispatch_dm_message(bot, event)
    assert capture.calls == []  # a bot-authored DM never relays (no loop)


async def test_bot_send_dm_output_does_not_refire_dm_message(monkeypatch):
    # relay-support (f), DM direction: when the relay handler DMs the member back
    # (send_dm), that outbound DM is authored by the bot, so the DM listener's
    # is_human guard drops it — the bot's own send_dm can never re-fire dm_message
    # and loop the relay. Mirrors the on_message own-bot guard for the DM surface.
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    monkeypatch.setattr(handler_events, "_cache", _DmCache({"1"}))
    monkeypatch.setattr(handler_events, "_get_api_client", lambda: object())
    message, _ = _dm_message(content="relayed reply from staff")
    bot = SimpleNamespace(cache=_DmBotCache([1], [1]))
    # is_human is False for the bot's own outbound DM (the send_dm relay output).
    event = SimpleNamespace(is_human=False, message=message)
    await dispatch_dm_message(bot, event)
    assert capture.calls == []


async def test_dm_listener_dispatches_per_mutual_guild(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # Both mutual guilds have a dm_message handler; a third guild (no membership)
    # must not see the DM.
    monkeypatch.setattr(handler_events, "_cache", _DmCache({"1", "2", "3"}))
    monkeypatch.setattr(handler_events, "_get_api_client", lambda: object())
    message, _ = _dm_message()
    bot = SimpleNamespace(cache=_DmBotCache([1, 2, 3], [1, 2]))
    event = SimpleNamespace(is_human=True, message=message)
    await dispatch_dm_message(bot, event)
    # One dispatch per routed mutual guild (G1, G2); the un-joined G3 is dropped.
    assert len(capture.calls) == 2
    # Every dispatch has NO home channel and the dm_message trigger.
    for call in capture.calls:
        assert call["channel_id"] == ""
        assert call["trigger_type"] == "dm_message"


async def test_dm_listener_no_dispatch_when_no_mutual_handler_guild(monkeypatch):
    capture = _DispatchCapture()
    monkeypatch.setattr(handler_events, "_dispatch", capture)
    # The author's only mutual guild has no dm_message handler -> dropped.
    monkeypatch.setattr(handler_events, "_cache", _DmCache(set()))
    monkeypatch.setattr(handler_events, "_get_api_client", lambda: object())
    message, _ = _dm_message()
    bot = SimpleNamespace(cache=_DmBotCache([1], [1]))
    event = SimpleNamespace(is_human=True, message=message)
    await dispatch_dm_message(bot, event)
    assert capture.calls == []


async def test_cache_guilds_with_trigger_filters_by_trigger():
    api = _FakeAPI(
        channels=[], guild_triggers=[["G1", "dm_message"], ["G2", "member_join"]]
    )
    cache = ActiveChannelsCache(ttl_seconds=999)
    assert await cache.guilds_with_trigger(api, "dm_message") == {"G1"}
    assert await cache.guilds_with_trigger(api, "member_join") == {"G2"}
