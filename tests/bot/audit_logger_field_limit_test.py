"""Audit-log embeds stay inside Discord's 1,024-character field limit.

A field one character over makes Discord refuse the whole embed (400), and the
entry is lost. The Oct 23 2026 case: the bot's own mod-log report grew by an
873-character triage assessment, and the "Changes" diff plus its fence came to
more than 1,024.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from smarter_dev.bot import audit_logger

GUILD_ID = 555
CHANNEL_ID = 777
MESSAGE_ID = 888


def _author():
    return SimpleNamespace(id=111, username="poster", mention="<@111>", avatar_url=None)


def _message(content):
    return SimpleNamespace(id=MESSAGE_ID, content=content, author=_author())


def _edit(old, new):
    return SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message=_message(new),
        old_message=None if old is None else _message(old),
    )


@pytest.fixture
def sent(monkeypatch):
    embeds = []

    async def should_log(*args, **kwargs):
        return True

    async def send(bot, guild_id, embed):
        embeds.append(embed)
        return True

    monkeypatch.setattr(audit_logger, "should_log_event", should_log)
    monkeypatch.setattr(audit_logger, "send_audit_log", send)
    return embeds


def _field(embed, name):
    (field,) = [field for field in embed.fields if field.name == name]
    return field.value


def _all_within_limit(embed):
    return all(
        len(field.value) <= audit_logger.FIELD_VALUE_LIMIT for field in embed.fields
    )


def _report(lines):
    return "\n".join(f"line {index}: " + "x" * 60 for index in range(lines))


async def test_a_long_edit_of_a_cached_message_keeps_the_fence_inside_the_limit(sent):
    before = _report(12)
    after = before + "\n**Assessment:** " + "y" * 873 + "\n- note one\n- note two"

    await audit_logger.log_message_edit(None, _edit(before, after))

    (embed,) = sent
    changes = _field(embed, "Changes")
    assert len(changes) <= audit_logger.FIELD_VALUE_LIMIT
    assert changes.startswith("```diff\n")
    assert changes.endswith("...\n```")
    assert _all_within_limit(embed)


async def test_a_long_edit_of_an_uncached_message_keeps_the_labels_inside_the_limit(
    sent,
):
    await audit_logger.log_message_edit(None, _edit(None, "z" * 2000))

    (embed,) = sent
    changes = _field(embed, "Changes")
    assert len(changes) <= audit_logger.FIELD_VALUE_LIMIT
    assert changes.startswith("**New Content:**\nzzz")
    assert changes.endswith(
        "...\n\n*Original content not available (message not cached)*"
    )
    assert _all_within_limit(embed)


async def test_a_short_edit_is_shown_whole(sent):
    await audit_logger.log_message_edit(None, _edit("hello", "hello there"))

    (embed,) = sent
    assert _field(embed, "Changes") == "```diff\n- hello\n+ hello there\n```"


async def test_a_long_deleted_message_is_cut_to_the_limit(sent):
    event = SimpleNamespace(
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message_id=MESSAGE_ID,
        old_message=_message("w" * 4000),
    )

    await audit_logger.log_message_delete(None, event)

    (embed,) = sent
    content = _field(embed, "Content")
    assert len(content) == audit_logger.FIELD_VALUE_LIMIT
    assert content.endswith("...")


async def test_many_role_changes_list_whole_mentions_and_count_the_rest(sent):
    old_roles = [10**18 + index for index in range(60)]
    new_roles = [2 * 10**18 + index for index in range(60)]

    def member(role_ids):
        return SimpleNamespace(
            id=111,
            username="poster",
            nickname=None,
            mention="<@111>",
            avatar_url=None,
            role_ids=role_ids,
            communication_disabled_until=None,
        )

    event = SimpleNamespace(
        guild_id=GUILD_ID, member=member(new_roles), old_member=member(old_roles)
    )

    await audit_logger.log_member_update(None, event)

    (embed,) = sent
    assert _all_within_limit(embed)
    for name in ("Roles Added", "Roles Removed"):
        value = _field(embed, name)
        *mentions, _, count, _ = value.split(" ")
        assert value.endswith(" more")
        assert all(m.startswith("<@&") and m.endswith(">") for m in mentions)
        assert len(mentions) + int(count) == 60


def test_join_within_keeps_everything_that_fits():
    assert audit_logger.join_within(["a", "b", "c"]) == "a b c"
    assert (
        audit_logger.join_within(["aaaa", "bbbb", "cccc"], limit=14) == "aaaa bbbb cccc"
    )
    items = ["aaaa", "bbbb", "cccccccccc"]
    assert audit_logger.join_within(items, limit=16) == "aaaa and 2 more"
    assert audit_logger.join_within(items, limit=14) == "and 3 more"
