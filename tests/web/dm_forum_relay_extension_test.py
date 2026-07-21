"""Behaviour tests for the dm-forum-relay catalog extension (forum-post-per-member).

Renders the shipped manifest's two ``.monty`` scripts with a concrete config and
runs them through the real handler runtime with a stubbed emitter/actor — the
same offline harness pattern as ``handler_runtime_test.py`` — so the mirror/relay
handoff over guild-shared memory is exercised end to end, not just linted.

Only this extension's own catalog dir + this file are touched (concurrency rule);
the manifest is imported directly, never through a whole-catalog scan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from smarter_dev.extensions.catalog.dm_forum_relay import MANIFEST
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.web.handler_budget import admin_budget
from smarter_dev.web.handler_runtime import run_handler_script

_CATALOG_DIR = (
    Path(__file__).resolve().parents[2]
    / "smarter_dev"
    / "extensions"
    / "catalog"
    / "dm_forum_relay"
)

FORUM = "123456789012345678"
MEMBER = "555555555555555555"
POSTS_KEY = "dm_forum_posts"


# -- stubs --------------------------------------------------------------------


@dataclass
class _Emitter:
    created_posts: list = field(default_factory=list)   # (channel, title, content, tags)
    messages: list = field(default_factory=list)         # (channel, content) actually sent
    tolerated: list = field(default_factory=list)        # (channel, content, tolerate_flag)
    reactions: list = field(default_factory=list)        # (channel, message_id, emoji)
    dm_sends: list = field(default_factory=list)         # (user_id, content)
    missing_targets: set = field(default_factory=set)    # explicit targets that 404 -> False
    dm_result: object = "dm-msg-id"                      # send_dm return (False = closed door)

    async def create_message(
        self, channel_id, content, ping_role_id=None, tolerate_missing_target=False
    ):
        self.tolerated.append((channel_id, content, tolerate_missing_target))
        if tolerate_missing_target and channel_id in self.missing_targets:
            return False
        self.messages.append((channel_id, content))
        return f"msg{len(self.messages)}"

    async def add_reaction(self, channel_id, message_id, emoji):
        self.reactions.append((channel_id, message_id, emoji))

    async def create_post(self, channel_id, title, content, tag_names=None):
        self.created_posts.append((channel_id, title, content, tag_names))
        return f"post{len(self.created_posts)}"

    async def send_dm(self, user_id, content):
        self.dm_sends.append((user_id, content))
        return self.dm_result

    async def get_thread_parent_id(self, thread_id):
        return None


@dataclass
class _Actor:
    calls: list = field(default_factory=list)
    gone: set = field(default_factory=set)

    async def reopen_thread(self, thread_id):
        self.calls.append(("reopen", thread_id))
        return thread_id not in self.gone


@dataclass
class _Limiter:
    allow: bool = True
    calls: list = field(default_factory=list)

    async def hit(self, key, limit, window_seconds=None):
        self.calls.append((key, limit, window_seconds))
        return self.allow


# -- harness ------------------------------------------------------------------


def _rendered(key, config):
    scripts = {h.key: (_CATALOG_DIR / h.script_file).read_text() for h in MANIFEST.handlers}
    bundle = render_bundle(MANIFEST, config, scripts)
    return next(item for item in bundle if item.key == key)


def _config(notify=True):
    return {"forum_channel_id": FORUM, "notify_on_first_dm": notify}


async def _fire(item, context, *, emitter, actor=None, guild_memory=None, memory=None):
    return await run_handler_script(
        item.script,
        context,
        channel_id=FORUM,               # the worker resolves channel_ids[0] as home
        guild_id="G1",
        emitter=emitter,
        limiter=_Limiter(),
        budget=admin_budget(),
        actor=actor or _Actor(),
        channel_ids=item.channel_ids,
        guild_memory=guild_memory if guild_memory is not None else {},
        memory=memory if memory is not None else {},
    )


def _dm_context(*, content="hello", attachments=None, member=MEMBER):
    return {
        "trigger_type": "dm_message",
        "content": content,
        "message_id": "M1",
        "dm_channel_id": "DM1",
        "author_id": member,
        "author_username": "alice",
        "author_display_name": "Alice",
        "attachment_urls": attachments or [],
    }


def _forum_context(*, thread_id="post1", content="reply text", attachments=None, is_thread=True):
    return {
        "trigger_type": "message",
        "message_content": content,
        "message_id": "M9",
        "author_id": "staff1",
        "is_thread": is_thread,
        "thread_id": thread_id,
        "attachments": attachments or [],
    }


# -- manifest render / lint ----------------------------------------------------


def test_manifest_renders_and_lints_clean():
    scripts = {h.key: (_CATALOG_DIR / h.script_file).read_text() for h in MANIFEST.handlers}
    bundle = render_bundle(MANIFEST, MANIFEST.example_config, scripts)
    by_key = {item.key: item for item in bundle}
    assert set(by_key) == {"dm-mirror", "forum-relay"}
    # Both rows are scoped to the forum channel: for dm-mirror that becomes the
    # fire's home channel (so create_post targets the forum); for forum-relay it
    # is the dispatch scope. Neither grants roles, so no allowlist is needed.
    assert by_key["dm-mirror"].channel_ids == [FORUM]
    assert by_key["forum-relay"].channel_ids == [FORUM]
    assert by_key["dm-mirror"].settings == {}
    assert by_key["forum-relay"].settings == {}
    # The legacy staff-role gate / attribution footer are gone from the config.
    assert {f.name for f in MANIFEST.config} == {"forum_channel_id", "notify_on_first_dm"}


# -- dm-mirror -----------------------------------------------------------------


async def test_first_dm_creates_post_maps_and_notices():
    emitter = _Emitter()
    actor = _Actor()
    result = await _fire(
        _rendered("dm-mirror", _config()),
        _dm_context(content="hello"),
        emitter=emitter,
        actor=actor,
        guild_memory={},
    )
    assert result.outcome == "ok", result.error
    # A post is opened on the forum (home) channel; its content is the first DM.
    assert emitter.created_posts == [(FORUM, f"DM: Alice ({MEMBER})", "hello", None)]
    # The member -> post mapping is written to the one shared key.
    assert result.guild_memory_writes == {POSTS_KEY: {MEMBER: "post1"}}
    # First DM ever from this member: the one-time monitoring notice is DM'd.
    assert emitter.dm_sends and emitter.dm_sends[0][0] == MEMBER
    # No reopen and no relay-into-existing-post: the create IS the relay.
    assert actor.calls == []


async def test_existing_post_reopens_then_relays_no_new_post():
    emitter = _Emitter()
    actor = _Actor()
    result = await _fire(
        _rendered("dm-mirror", _config()),
        _dm_context(content="second message"),
        emitter=emitter,
        actor=actor,
        guild_memory={POSTS_KEY: {MEMBER: "post-existing"}},
        memory={"warned_ids": [MEMBER]},   # already warned -> no notice noise
    )
    assert result.outcome == "ok", result.error
    assert actor.calls == [("reopen", "post-existing")]        # unarchive first
    assert emitter.messages == [("post-existing", "second message")]
    assert emitter.created_posts == []                         # reused, not recreated
    assert result.guild_memory_writes == {}                    # map unchanged
    assert emitter.dm_sends == []                              # already warned


async def test_deleted_post_send_false_mints_fresh_post_and_remaps():
    emitter = _Emitter(missing_targets={"post-gone"})          # relay send -> False
    actor = _Actor()
    result = await _fire(
        _rendered("dm-mirror", _config()),
        _dm_context(content="third"),
        emitter=emitter,
        actor=actor,
        guild_memory={POSTS_KEY: {MEMBER: "post-gone"}},
        memory={"warned_ids": [MEMBER]},
    )
    assert result.outcome == "ok", result.error
    # The relay attempt into the gone post requested tolerance and returned False.
    assert ("post-gone", "third", True) in emitter.tolerated
    # A fresh post is minted and the mapping repointed at it.
    assert emitter.created_posts == [(FORUM, f"DM: Alice ({MEMBER})", "third", None)]
    assert result.guild_memory_writes == {POSTS_KEY: {MEMBER: "post1"}}


async def test_attachment_urls_appended_to_relay_body():
    emitter = _Emitter()
    result = await _fire(
        _rendered("dm-mirror", _config()),
        _dm_context(
            content="look",
            attachments=[{"url": "http://cdn/x.png", "filename": "x.png"}],
        ),
        emitter=emitter,
        guild_memory={},
        memory={"warned_ids": [MEMBER]},
    )
    assert result.outcome == "ok", result.error
    assert emitter.created_posts[0][2] == "look\nattachment: http://cdn/x.png"


async def test_long_dm_body_capped_for_post_creation():
    # A Nitro member can DM 4000 chars; the emitter's create_post does NOT
    # truncate content host-side, so the script must cap the body at Discord's
    # 2000-char message limit or the create 400s and errors the fire.
    emitter = _Emitter()
    result = await _fire(
        _rendered("dm-mirror", _config()),
        _dm_context(content="x" * 4000),
        emitter=emitter,
        guild_memory={},
        memory={"warned_ids": [MEMBER]},
    )
    assert result.outcome == "ok", result.error
    assert len(emitter.created_posts[0][2]) == 2000


async def test_empty_dm_body_renders_placeholder():
    emitter = _Emitter()
    result = await _fire(
        _rendered("dm-mirror", _config()),
        _dm_context(content=""),
        emitter=emitter,
        guild_memory={},
        memory={"warned_ids": [MEMBER]},
    )
    assert result.outcome == "ok", result.error
    assert emitter.created_posts[0][2] == "*(no text)*"


async def test_notice_not_resent_when_already_warned():
    emitter = _Emitter()
    result = await _fire(
        _rendered("dm-mirror", _config(notify=True)),
        _dm_context(),
        emitter=emitter,
        guild_memory={},
        memory={"warned_ids": [MEMBER]},
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends == []


async def test_notify_disabled_sends_no_notice():
    emitter = _Emitter()
    result = await _fire(
        _rendered("dm-mirror", _config(notify=False)),
        _dm_context(),
        emitter=emitter,
        guild_memory={},
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends == []
    # Rendered constant reflects the disabled config.
    assert "NOTIFY_ON_FIRST_DM = False" in _rendered("dm-mirror", _config(notify=False)).script


async def test_first_notice_records_warned_id():
    emitter = _Emitter()
    result = await _fire(
        _rendered("dm-mirror", _config()),
        _dm_context(),
        emitter=emitter,
        guild_memory={},
        memory={},
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends and emitter.dm_sends[0][0] == MEMBER
    assert result.memory.get("warned_ids") == [MEMBER]


async def test_map_prunes_oldest_beyond_cap():
    # Seed a full map (100 members); a brand-new member's first DM must prune the
    # single oldest entry so the stored value stays bounded.
    seeded = {f"{1000000000000000000 + i}": f"oldpost{i}" for i in range(100)}
    oldest_key = next(iter(seeded))
    emitter = _Emitter()
    result = await _fire(
        _rendered("dm-mirror", _config()),
        _dm_context(member=MEMBER, content="new"),
        emitter=emitter,
        guild_memory={POSTS_KEY: dict(seeded)},
        memory={"warned_ids": [MEMBER]},
    )
    assert result.outcome == "ok", result.error
    written = result.guild_memory_writes[POSTS_KEY]
    assert len(written) == 100                       # capped
    assert oldest_key not in written                 # oldest dropped
    assert written[MEMBER] == "post1"                # newcomer present at the tail


# -- forum-relay ---------------------------------------------------------------


async def test_reply_in_mapped_post_dms_member_and_confirms_in_post():
    emitter = _Emitter(dm_result="delivered")
    result = await _fire(
        _rendered("forum-relay", _config()),
        _forum_context(thread_id="post1", content="how can we help?"),
        emitter=emitter,
        guild_memory={POSTS_KEY: {MEMBER: "post1"}},
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends == [(MEMBER, "how can we help?")]
    # Delivery signal is a 📤 reaction on the staff reply itself, targeted at
    # the post thread (the fire's home is the parent forum, so the thread id
    # must be passed explicitly). No status message on success.
    assert emitter.reactions == [("post1", "M9", "📤")]
    assert emitter.messages == []


async def test_reply_dm_closed_flags_failure_in_post():
    emitter = _Emitter(dm_result=False)                # member's DMs are closed
    result = await _fire(
        _rendered("forum-relay", _config()),
        _forum_context(thread_id="post1", content="hi"),
        emitter=emitter,
        guild_memory={POSTS_KEY: {MEMBER: "post1"}},
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends == [(MEMBER, "hi")]
    # Failure keeps the explanatory message alongside the ❌ reaction.
    assert emitter.reactions == [("post1", "M9", "❌")]
    assert len(emitter.messages) == 1
    channel, content = emitter.messages[0]
    assert channel == "post1" and content.startswith("❌")


async def test_reply_in_unmapped_post_flags_failure_no_dm():
    emitter = _Emitter()
    result = await _fire(
        _rendered("forum-relay", _config()),
        _forum_context(thread_id="orphan-post", content="hello?"),
        emitter=emitter,
        guild_memory={POSTS_KEY: {MEMBER: "post1"}},   # orphan-post not mapped
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends == []
    assert emitter.reactions == [("orphan-post", "M9", "❌")]
    assert len(emitter.messages) == 1
    channel, content = emitter.messages[0]
    assert channel == "orphan-post" and content.startswith("❌")


async def test_status_send_tolerates_post_gone_mid_fire():
    # The post vanishes between the staff reply and the delivery signal: the
    # DM still goes out, the 📤 reaction attempt returns False (best-effort,
    # emitter 404 tolerance), and the fire completes clean.
    emitter = _Emitter(dm_result="delivered", missing_targets={"post1"})
    result = await _fire(
        _rendered("forum-relay", _config()),
        _forum_context(thread_id="post1", content="hi"),
        emitter=emitter,
        guild_memory={POSTS_KEY: {MEMBER: "post1"}},
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends == [(MEMBER, "hi")]
    assert emitter.messages == []                      # no status message on success


async def test_non_thread_message_is_noop():
    emitter = _Emitter()
    result = await _fire(
        _rendered("forum-relay", _config()),
        _forum_context(is_thread=False, thread_id=None, content="channel chatter"),
        emitter=emitter,
        guild_memory={POSTS_KEY: {MEMBER: "post1"}},
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends == []
    assert emitter.reactions == []
    assert emitter.messages == []


async def test_reply_attachments_appended_and_empty_text_placeholder():
    emitter = _Emitter(dm_result="ok")
    result = await _fire(
        _rendered("forum-relay", _config()),
        _forum_context(
            thread_id="post1",
            content="",
            attachments=[{"url": "http://cdn/a.png", "content_type": "image/png", "filename": "a.png"}],
        ),
        emitter=emitter,
        guild_memory={POSTS_KEY: {MEMBER: "post1"}},
    )
    assert result.outcome == "ok", result.error
    assert emitter.dm_sends == [(MEMBER, "attachment: http://cdn/a.png")]
    assert emitter.reactions == [("post1", "M9", "📤")]
