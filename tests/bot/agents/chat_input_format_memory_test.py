"""Tests for the three memory blocks in the chat agent's metadata.

``<what-i-remember>`` (the long-term blob), ``<from-today>`` (notes the agent
kept since midnight UTC) and ``<what-i-did>`` (the rolling hour of things the
bot's account actually did) sit between ``<image-quota/>`` and ``<topic>`` —
deliberately far-to-near, with the per-channel scratchpad staying nearest.

Every block is omitted entirely when it has nothing in it: an empty memory tag
is an invitation for the model to remark on its own amnesia.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from smarter_dev.bot.agents.chat_input_format import EVENT_WINDOW_DELTA
from smarter_dev.bot.agents.chat_input_format import EVENT_WINDOW_FULL
from smarter_dev.bot.agents.chat_input_format import MAX_EVENT_LINES
from smarter_dev.bot.agents.chat_input_format import TRUNCATED_EVENTS_LINE
from smarter_dev.bot.agents.chat_input_format import build_agent_call
from smarter_dev.bot.agents.chat_models import Author
from smarter_dev.bot.agents.chat_models import ChannelInfo
from smarter_dev.bot.agents.chat_models import FollowupAgentInput
from smarter_dev.bot.agents.chat_models import GuildEventView
from smarter_dev.bot.agents.chat_models import InitialAgentInput
from smarter_dev.bot.agents.chat_models import Me
from smarter_dev.bot.agents.chat_models import MemoryNote
from smarter_dev.bot.agents.chat_models import Message
from smarter_dev.shared.guild_event_log import mod_action_event

NOW = datetime(2026, 8, 6, 14, 52, tzinfo=UTC)

BLOB = "## Who's here\nalice (id 1) — shaders, very giddy about soft shadows."


def _message() -> Message:
    return Message(
        message_id="101",
        author_id="200",
        body="@bot you around?",
        mentions_bot=True,
    )


def _initial(**overrides) -> InitialAgentInput:
    fields = {
        "me": Me(user_id="999", username="bot"),
        "channel_history": [],
        "activation_message": _message(),
        "authors": [Author(user_id="200", username="alice")],
        "channel": ChannelInfo(channel_id="1", name="general"),
        "now_utc": NOW,
        **overrides,
    }
    return InitialAgentInput(**fields)


def _followup(**overrides) -> FollowupAgentInput:
    fields = {
        "me": Me(user_id="999", username="bot"),
        "new_messages": [_message()],
        "authors": [Author(user_id="200", username="alice")],
        "channel": ChannelInfo(channel_id="1", name="general"),
        "now_utc": NOW,
        **overrides,
    }
    return FollowupAgentInput(**fields)


def _note(text: str, *, at: datetime, channel_name: str | None = "dev-help") -> MemoryNote:
    return MemoryNote(
        text=text,
        channel_id="55",
        channel_name=channel_name,
        created_at=at,
    )


def _timeout_event() -> GuildEventView:
    return GuildEventView(
        at=datetime(2026, 8, 6, 14, 2, tzinfo=UTC),
        kind="mod_action",
        channel_id="1",
        channel_name="general",
        action="timeout",
        target_username="mallory",
        target_user_id="8",
        reason="invite-link spam after a warning",
        duration_seconds=600,
        source="ai",
    )


# -- the blob ------------------------------------------------------------


def test_long_term_memory_renders_verbatim_with_its_update_date():
    prompt, _ = build_agent_call(
        _initial(
            long_term_memory=BLOB,
            long_term_memory_updated_at=datetime(2026, 8, 6, 0, 21, tzinfo=UTC),
        ),
        [],
    )

    assert '<what-i-remember updated="2026-08-06">' in prompt
    assert BLOB in prompt
    assert "</what-i-remember>" in prompt


def test_long_term_memory_without_a_date_drops_the_attribute():
    prompt, _ = build_agent_call(_initial(long_term_memory=BLOB), [])

    assert "<what-i-remember>" in prompt


def test_no_long_term_memory_omits_the_block_entirely():
    prompt, _ = build_agent_call(_initial(), [])

    assert "what-i-remember" not in prompt


def test_blank_long_term_memory_omits_the_block_entirely():
    prompt, _ = build_agent_call(_initial(long_term_memory="   "), [])

    assert "what-i-remember" not in prompt


# -- today's notes -------------------------------------------------------


def test_todays_notes_render_oldest_first_with_time_and_channel():
    prompt, _ = build_agent_call(
        _initial(
            memory_notes=[
                _note("bob (id 2) shipped the parser.", at=datetime(2026, 8, 6, 11, 5, tzinfo=UTC)),
                _note(
                    "alice (id 1) got soft shadows working and was genuinely giddy about it.",
                    at=datetime(2026, 8, 6, 9, 41, tzinfo=UTC),
                ),
            ]
        ),
        [],
    )

    body = prompt.split("<from-today>")[1].split("</from-today>")[0].strip()
    assert body.splitlines() == [
        "09:41Z #dev-help — alice (id 1) got soft shadows working and was "
        "genuinely giddy about it.",
        "11:05Z #dev-help — bob (id 2) shipped the parser.",
    ]


def test_a_note_without_a_channel_name_drops_the_channel():
    prompt, _ = build_agent_call(
        _initial(
            memory_notes=[
                _note("something I kept", at=datetime(2026, 8, 6, 9, 41, tzinfo=UTC), channel_name=None)
            ]
        ),
        [],
    )

    assert "09:41Z — something I kept" in prompt


def test_no_notes_omits_the_block_entirely():
    prompt, _ = build_agent_call(_initial(), [])

    assert "from-today" not in prompt


# -- what the bot's account did ------------------------------------------


def test_moderation_event_renders_with_duration_channel_and_reason():
    prompt, _ = build_agent_call(_initial(guild_events=[_timeout_event()]), [])

    assert f'<what-i-did window="{EVENT_WINDOW_FULL}">' in prompt
    assert (
        "14:02Z (50m ago) — I timed out mallory (id 8) for 10m in #general: "
        "invite-link spam after a warning." in prompt
    )


def test_manual_slash_action_is_attributed_to_the_moderator():
    event = _timeout_event().model_copy(
        update={"source": "manual", "moderator_username": "zech"}
    )
    prompt, _ = build_agent_call(_initial(guild_events=[event]), [])

    assert (
        "14:02Z (50m ago) — I timed out mallory (id 8) for 10m in #general for "
        "@zech: invite-link spam after a warning." in prompt
    )


def test_an_action_without_a_reason_ends_cleanly():
    event = _timeout_event().model_copy(update={"reason": None, "duration_seconds": None})
    prompt, _ = build_agent_call(_initial(guild_events=[event]), [])

    assert "14:02Z (50m ago) — I timed out mallory (id 8) in #general." in prompt


def test_a_dm_carries_its_purpose_and_no_channel():
    event = GuildEventView(
        at=datetime(2026, 8, 6, 14, 2, tzinfo=UTC),
        kind="bot_dm",
        target_username="mallory",
        target_user_id="8",
        summary="the timeout notice",
    )
    prompt, _ = build_agent_call(_initial(guild_events=[event]), [])

    assert "14:02Z (50m ago) — I DM'd mallory (id 8): the timeout notice." in prompt


def test_an_announcement_renders_as_something_posted():
    event = GuildEventView(
        at=datetime(2026, 8, 6, 14, 22, tzinfo=UTC),
        kind="bot_message",
        channel_id="1",
        channel_name="announcements",
        summary='the scheduled message "standup"',
    )
    prompt, _ = build_agent_call(_initial(guild_events=[event]), [])

    assert (
        "14:22Z (30m ago) — I posted in #announcements: the scheduled message "
        '"standup".' in prompt
    )


def test_event_block_keeps_the_newest_lines_and_says_it_truncated():
    events = [
        _timeout_event().model_copy(
            update={
                "at": datetime(2026, 8, 6, 14, 0, tzinfo=UTC).replace(minute=index % 60),
                "target_username": f"user{index}",
                "target_user_id": str(index),
            }
        )
        for index in range(MAX_EVENT_LINES + 5)
    ]
    prompt, _ = build_agent_call(_initial(guild_events=events), [])

    block = prompt.split("<what-i-did")[1].split("</what-i-did>")[0]
    lines = [line for line in block.splitlines() if line.strip()][1:]
    assert lines[0] == TRUNCATED_EVENTS_LINE
    assert len(lines) == MAX_EVENT_LINES + 1
    assert "user0" not in block
    assert f"user{MAX_EVENT_LINES + 4}" in block


def test_no_events_omits_the_block_entirely():
    prompt, _ = build_agent_call(_initial(), [])

    assert "what-i-did" not in prompt


def test_a_guild_event_view_is_built_from_a_recorded_event():
    event = mod_action_event(
        {
            "action_type": "warn",
            "target_username": "mallory",
            "target_user_id": "8",
            "reason": "third strike",
            "source": "manual",
            "moderator_username": "zech",
            "channel_id": "1",
            "created_at": "2026-08-06T14:02:00+00:00",
        },
        guild_id="99",
        channel_name="general",
    )
    view = GuildEventView.from_guild_event(event)

    prompt, _ = build_agent_call(_initial(guild_events=[view]), [])

    # The event log has no user id to carry, so the line names the person
    # without one; ``target_user_id`` is rendered only when a capture site
    # actually knows it.
    assert (
        "14:02Z (50m ago) — I warned mallory in #general for @zech: third strike."
        in prompt
    )


# -- follow-up turns -----------------------------------------------------


def test_followup_events_are_labelled_as_a_delta():
    prompt, _ = build_agent_call(_followup(new_guild_events=[_timeout_event()]), [])

    assert f'<what-i-did window="{EVENT_WINDOW_DELTA}">' in prompt
    assert "I timed out mallory (id 8)" in prompt


def test_followup_carries_no_blob_or_notes_by_default():
    prompt, _ = build_agent_call(_followup(), [])

    assert "what-i-remember" not in prompt
    assert "from-today" not in prompt


def test_followup_re_emits_the_blob_after_compaction():
    prompt, _ = build_agent_call(_followup(long_term_memory=BLOB), [])

    assert "<what-i-remember>" in prompt
    assert BLOB in prompt


# -- placement -----------------------------------------------------------


def test_memory_blocks_sit_between_the_image_quota_and_the_topic():
    prompt, _ = build_agent_call(
        _initial(
            topic="soft shadows",
            notes="alice: shaders",
            long_term_memory=BLOB,
            memory_notes=[_note("alice (id 1) is giddy", at=datetime(2026, 8, 6, 9, 41, tzinfo=UTC))],
            guild_events=[_timeout_event()],
        ),
        [],
        image_quota={"remaining": 2, "limit": 3, "resets_at": "2026-08-06T15:00Z"},
    )

    order = [
        prompt.index("<image-quota"),
        prompt.index("<what-i-remember"),
        prompt.index("<from-today>"),
        prompt.index("<what-i-did"),
        prompt.index("<topic>"),
        prompt.index("<notes>"),
    ]
    assert order == sorted(order)
