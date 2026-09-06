"""Tests for the write-time redaction of verbatim Discord message text."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelRequestPart,
    ModelResponse,
    ModelResponsePart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    ToolSearchReturnPart,
    UserPromptPart,
)

from smarter_dev.bot.agents.chat_models import Message, MessageAttachment
from smarter_dev.shared.message_content import (
    CONTENT_RETENTION_WINDOW,
    MESSAGE_CONTENT_PLACEHOLDER,
    oldest_retained_stream_id,
    redact_chat_agent_messages,
    redact_forum_post,
    redact_help_context_messages,
    redact_help_question,
    redact_model_message_parts,
    redact_text,
    redact_trigger_context,
)


def chat_message_dict(**overrides) -> dict:
    """A ``ChatAgentTurn.triggering_messages`` entry as the bot serialises it."""
    message = Message(
        message_id="444",
        author_id="333",
        body="what someone said",
        reactions=["👍"],
        attachments=[MessageAttachment(url="https://cdn/x.png", filename="x.png")],
        sent_at=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        mentions_bot=True,
        reply_to_message_id="443",
        reply_to_author_id="222",
        reply_to_is_self=True,
    )
    return message.model_dump(mode="json") | overrides


def model_messages_dump() -> list[dict]:
    """A real pydantic-ai serialised ``ModelMessage`` list."""
    return ModelMessagesTypeAdapter.dump_python(
        [
            ModelRequest(
                parts=[
                    SystemPromptPart(content="you are a bot"),
                    UserPromptPart(content="what someone said"),
                ]
            ),
            ModelResponse(
                parts=[
                    TextPart(content="the agent reply"),
                    ToolCallPart(
                        tool_name="search", args={"query": "a phrase"}, tool_call_id="c1"
                    ),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="search",
                        content={"messages": ["what someone said"]},
                        tool_call_id="c1",
                    )
                ]
            ),
        ],
        mode="json",
    )


def part_kinds_of(part_union) -> set[str]:
    """The ``part_kind`` tags pydantic-ai discriminates a part union on."""
    return {
        meta.tag
        for member in get_args(get_args(part_union)[0])
        for meta in member.__metadata__
        if hasattr(meta, "tag")
    }


def help_context_message(**overrides) -> dict:
    """A ``HelpConversation.context_messages`` entry as the help plugin builds it."""
    return {
        "author": "someone",
        "timestamp": "2026-07-26T12:00:00+00:00",
        "content": "what someone said",
    } | overrides


class TestRedactText:
    def test_replaces_non_empty_text_with_the_placeholder(self):
        assert redact_text("what someone said") == MESSAGE_CONTENT_PLACEHOLDER

    def test_empty_stays_empty(self):
        assert redact_text("") == ""

    def test_none_stays_none(self):
        assert redact_text(None) is None

    def test_is_idempotent(self):
        once = redact_text("what someone said")
        assert redact_text(once) == MESSAGE_CONTENT_PLACEHOLDER

    def test_whitespace_is_text(self):
        assert redact_text("   ") == MESSAGE_CONTENT_PLACEHOLDER


class TestRedactChatAgentMessages:
    def test_replaces_the_body(self):
        [redacted] = redact_chat_agent_messages([chat_message_dict()])
        assert redacted["body"] == MESSAGE_CONTENT_PLACEHOLDER

    def test_drops_attachments(self):
        [redacted] = redact_chat_agent_messages([chat_message_dict()])
        assert redacted["attachments"] == []

    def test_keeps_everything_the_detail_view_renders(self):
        original = chat_message_dict()
        [redacted] = redact_chat_agent_messages([original])
        assert redacted["message_id"] == "444"
        assert redacted["author_id"] == "333"
        assert redacted["reply_to_message_id"] == "443"
        assert redacted["reply_to_author_id"] == "222"
        assert redacted["reply_to_is_self"] is True
        assert redacted["reactions"] == ["👍"]
        assert redacted["sent_at"] == original["sent_at"]
        assert redacted["mentions_bot"] is True

    def test_empty_body_stays_empty(self):
        [redacted] = redact_chat_agent_messages([chat_message_dict(body="")])
        assert redacted["body"] == ""

    def test_none_becomes_an_empty_list(self):
        assert redact_chat_agent_messages(None) == []

    def test_does_not_mutate_its_argument(self):
        original = chat_message_dict()
        redact_chat_agent_messages([original])
        assert original["body"] == "what someone said"
        assert original["attachments"] != []

    def test_is_idempotent(self):
        once = redact_chat_agent_messages([chat_message_dict()])
        assert redact_chat_agent_messages(once) == once

    def test_a_help_shaped_dict_loses_its_content(self):
        [redacted] = redact_chat_agent_messages([help_context_message()])
        assert redacted["content"] == MESSAGE_CONTENT_PLACEHOLDER

    def test_redacts_a_key_chat_models_add_later(self):
        [redacted] = redact_chat_agent_messages(
            [chat_message_dict(reply_to_body="what someone else said")]
        )
        assert redacted["reply_to_body"] == MESSAGE_CONTENT_PLACEHOLDER

    def test_the_serialised_shape_has_not_drifted(self):
        # A field added to chat_models.Message reaches chat_agent_turns
        # verbatim unless it is redacted here, so adding one must fail this
        # test and force that decision.
        assert set(chat_message_dict()) == {
            "message_id",
            "author_id",
            "reply_to_message_id",
            "reply_to_author_id",
            "reply_to_is_self",
            "body",
            "reactions",
            "attachments",
            "sent_at",
            "mentions_bot",
        }


class TestRedactModelMessageParts:
    def test_replaces_user_prompt_content(self):
        messages = redact_model_message_parts(model_messages_dump())
        user_prompt = messages[0]["parts"][1]
        assert user_prompt["part_kind"] == "user-prompt"
        assert user_prompt["content"] == MESSAGE_CONTENT_PLACEHOLDER

    def test_empties_tool_return_content(self):
        messages = redact_model_message_parts(model_messages_dump())
        tool_return = messages[2]["parts"][0]
        assert tool_return["part_kind"] == "tool-return"
        assert tool_return["content"] == {}
        assert tool_return["tool_name"] == "search"
        assert tool_return["tool_call_id"] == "c1"

    def test_keeps_system_text_and_tool_call_parts(self):
        original = model_messages_dump()
        messages = redact_model_message_parts(original)
        assert messages[0]["parts"][0] == original[0]["parts"][0]
        assert messages[1]["parts"] == original[1]["parts"]

    def test_keeps_message_level_fields(self):
        original = model_messages_dump()
        messages = redact_model_message_parts(original)
        assert messages[1]["usage"] == original[1]["usage"]
        assert messages[1]["kind"] == "response"

    def test_empties_a_multimodal_user_prompt_list(self):
        dump = ModelMessagesTypeAdapter.dump_python(
            [ModelRequest(parts=[UserPromptPart(content=["said this", "and this"])])],
            mode="json",
        )
        [message] = redact_model_message_parts(dump)
        assert message["parts"][0]["content"] == []

    def test_empty_user_prompt_stays_empty(self):
        dump = ModelMessagesTypeAdapter.dump_python(
            [ModelRequest(parts=[UserPromptPart(content="")])], mode="json"
        )
        [message] = redact_model_message_parts(dump)
        assert message["parts"][0]["content"] == ""

    def test_none_stays_none(self):
        assert redact_model_message_parts(None) is None

    def test_tolerates_a_message_without_parts(self):
        assert redact_model_message_parts([{"kind": "response"}]) == [
            {"kind": "response"}
        ]

    def test_does_not_mutate_its_argument(self):
        original = model_messages_dump()
        redact_model_message_parts(original)
        assert original[0]["parts"][1]["content"] == "what someone said"
        assert original[2]["parts"][0]["content"] == {"messages": ["what someone said"]}

    def test_is_idempotent(self):
        once = redact_model_message_parts(model_messages_dump())
        assert redact_model_message_parts(once) == once

    def test_stays_valid_pydantic_ai_messages(self):
        redacted = redact_model_message_parts(model_messages_dump())
        assert len(list(ModelMessagesTypeAdapter.validate_python(redacted))) == 3

    def test_a_redacted_multimodal_prompt_stays_valid(self):
        dump = ModelMessagesTypeAdapter.dump_python(
            [ModelRequest(parts=[UserPromptPart(content=["said this", "and this"])])],
            mode="json",
        )
        redacted = redact_model_message_parts(dump)
        assert len(list(ModelMessagesTypeAdapter.validate_python(redacted))) == 1

    def test_reasoning_is_ai_authored_and_passes_through(self):
        # Reasoning summaries are the model's own text, like agent_output; the
        # retention sweep bounds them at 48h with the rest of the delta.
        dump = ModelMessagesTypeAdapter.dump_python(
            [ModelResponse(parts=[ThinkingPart(content="the user asked about uv")])],
            mode="json",
        )
        [message] = redact_model_message_parts(dump)
        assert message["parts"][0]["content"] == "the user asked about uv"

    def test_redacts_a_retry_prompt(self):
        dump = ModelMessagesTypeAdapter.dump_python(
            [
                ModelRequest(
                    parts=[
                        RetryPromptPart(
                            content="echoed what someone said",
                            tool_name="search",
                            tool_call_id="c1",
                        )
                    ]
                )
            ],
            mode="json",
        )
        [message] = redact_model_message_parts(dump)
        assert message["parts"][0]["content"] == MESSAGE_CONTENT_PLACEHOLDER
        assert message["parts"][0]["tool_name"] == "search"

    def test_redacts_a_part_kind_it_has_never_seen(self):
        # A kind pydantic-ai adds later is redacted until someone decides it
        # is model-authored and adds it to the preserved set.
        [message] = redact_model_message_parts(
            [{"parts": [{"part_kind": "future-return", "content": "said this"}]}]
        )
        assert message["parts"][0]["content"] == MESSAGE_CONTENT_PLACEHOLDER

    def test_empties_tool_return_metadata_and_keeps_its_bookkeeping(self):
        dump = ModelMessagesTypeAdapter.dump_python(
            [
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name="search",
                            content={"messages": ["what someone said"]},
                            tool_call_id="c1",
                            metadata={"raw": "what someone said"},
                        )
                    ]
                )
            ],
            mode="json",
        )
        [message] = redact_model_message_parts(dump)
        part = message["parts"][0]
        assert part["metadata"] == {}
        assert part["outcome"] == "success"
        assert part["tool_kind"] == dump[0]["parts"][0]["tool_kind"]
        assert part["timestamp"] == dump[0]["parts"][0]["timestamp"]
        assert len(list(ModelMessagesTypeAdapter.validate_python([message]))) == 1

    def test_a_redacted_part_without_content_stays_without_it(self):
        [message] = redact_model_message_parts(
            [{"parts": [{"part_kind": "future-return", "tool_call_id": "c1"}]}]
        )
        assert message["parts"][0] == {"part_kind": "future-return", "tool_call_id": "c1"}

    def test_redacts_a_field_a_redacted_part_kind_gains_later(self):
        [message] = redact_model_message_parts(
            [
                {
                    "parts": [
                        {
                            "part_kind": "user-prompt",
                            "content": "what someone said",
                            "raw_text": "what someone said",
                        }
                    ]
                }
            ]
        )
        assert message["parts"][0]["raw_text"] == MESSAGE_CONTENT_PLACEHOLDER

    def test_the_redacted_part_fields_have_not_drifted(self):
        # Every field a redacted part kind carries has been classified as
        # either bookkeeping to keep or text to redact. A new field must fail
        # here and force that decision.
        assert {
            field.name
            for part_type in (
                UserPromptPart,
                ToolReturnPart,
                ToolSearchReturnPart,
                RetryPromptPart,
            )
            for field in fields(part_type)
        } == {
            "part_kind",
            "tool_name",
            "tool_call_id",
            "tool_kind",
            "timestamp",
            "outcome",
            "content",
            "metadata",
        }

    def test_the_library_part_kinds_have_not_drifted(self):
        # Every kind pydantic-ai can emit has been classified as either
        # carrying what a member said or being model-authored. A new kind
        # must fail here and force that decision.
        assert part_kinds_of(ModelRequestPart) == {
            "system-prompt",
            "user-prompt",
            "tool-return",
            "tool-search-return",
            "retry-prompt",
        }
        assert part_kinds_of(ModelResponsePart) == {
            "text",
            "thinking",
            "tool-call",
            "tool-search-call",
            "builtin-tool-call",
            "builtin-tool-search-call",
            "builtin-tool-return",
            "builtin-tool-search-return",
            "compaction",
            "file",
        }


class TestRedactHelpContextMessages:
    def test_replaces_the_content(self):
        [redacted] = redact_help_context_messages([help_context_message()])
        assert redacted["content"] == MESSAGE_CONTENT_PLACEHOLDER

    def test_keeps_author_and_timestamp(self):
        [redacted] = redact_help_context_messages([help_context_message()])
        assert redacted["author"] == "someone"
        assert redacted["timestamp"] == "2026-07-26T12:00:00+00:00"

    def test_empty_content_stays_empty(self):
        [redacted] = redact_help_context_messages([help_context_message(content="")])
        assert redacted["content"] == ""

    def test_none_becomes_an_empty_list(self):
        assert redact_help_context_messages(None) == []

    def test_a_structured_content_value_becomes_an_empty_container(self):
        [redacted] = redact_help_context_messages(
            [help_context_message(content=["a line", "another line"])]
        )
        assert redacted["content"] == []

    def test_does_not_mutate_its_argument(self):
        original = help_context_message()
        redact_help_context_messages([original])
        assert original["content"] == "what someone said"

    def test_is_idempotent(self):
        once = redact_help_context_messages([help_context_message()])
        assert redact_help_context_messages(once) == once

    def test_redacts_a_key_the_help_plugins_add_later(self):
        [redacted] = redact_help_context_messages(
            [help_context_message(reply_to_content="what someone else said")]
        )
        assert redacted["reply_to_content"] == MESSAGE_CONTENT_PLACEHOLDER

    def test_a_chat_shaped_dict_loses_its_body(self):
        [redacted] = redact_help_context_messages([chat_message_dict()])
        assert redacted["body"] == MESSAGE_CONTENT_PLACEHOLDER


class TestRedactHelpQuestion:
    def test_a_slash_command_argument_is_an_explicit_submission(self):
        question = "how do I use uv?"
        assert redact_help_question(question, "slash_command") == question

    @pytest.mark.parametrize("interaction_type", ["mention", "streak_celebration"])
    def test_a_discord_message_is_redacted(self, interaction_type):
        assert (
            redact_help_question("how do I use uv?", interaction_type)
            == MESSAGE_CONTENT_PLACEHOLDER
        )

    def test_an_unknown_interaction_type_is_redacted(self):
        assert (
            redact_help_question("how do I use uv?", "some_future_trigger")
            == MESSAGE_CONTENT_PLACEHOLDER
        )

    def test_an_empty_question_stays_empty(self):
        assert redact_help_question("", "mention") == ""


class TestRedactTriggerContext:
    def test_replaces_message_text(self):
        assert redact_trigger_context(
            {
                "trigger_type": "message",
                "message_content": "what someone said",
                "message_id": "444",
                "author_id": "333",
                "author_is_bot": False,
            }
        ) == {
            "trigger_type": "message",
            "message_content": MESSAGE_CONTENT_PLACEHOLDER,
            "message_id": "444",
            "author_id": "333",
            "author_is_bot": False,
        }

    def test_replaces_edit_before_and_after(self):
        assert redact_trigger_context(
            {
                "trigger_type": "message_edit",
                "message_content": "after",
                "old_content": "before",
                "author_id": "333",
            }
        ) == {
            "trigger_type": "message_edit",
            "message_content": MESSAGE_CONTENT_PLACEHOLDER,
            "old_content": MESSAGE_CONTENT_PLACEHOLDER,
            "author_id": "333",
        }

    def test_empties_dm_content_attachments_and_thread_titles(self):
        assert redact_trigger_context(
            {
                "trigger_type": "dm_message",
                "content": "a DM",
                "attachment_urls": [{"url": "https://cdn", "filename": "x.png"}],
                "attachments": [{"filename": "y.png"}],
                "embeds": [{"title": "quoted thing"}],
                "thread_name": "a title someone typed",
                "starter_message_content": "the opening post",
                "dm_channel_id": "555",
            }
        ) == {
            "trigger_type": "dm_message",
            "content": MESSAGE_CONTENT_PLACEHOLDER,
            "attachment_urls": [],
            "attachments": [],
            "embeds": [],
            "thread_name": MESSAGE_CONTENT_PLACEHOLDER,
            "starter_message_content": MESSAGE_CONTENT_PLACEHOLDER,
            "dm_channel_id": "555",
        }

    def test_empties_a_script_authored_timer_payload(self):
        # A handler script chooses what it carries across a schedule_timer, so
        # the payload it built may quote the message the fire was reacting to.
        assert redact_trigger_context(
            {
                "trigger_type": "timer",
                "payload": {"user_id": "333", "quote": "what someone said"},
                "scheduled_at": "2026-09-06T12:00:00+00:00",
            }
        ) == {
            "trigger_type": "timer",
            "payload": {},
            "scheduled_at": "2026-09-06T12:00:00+00:00",
        }

    def test_covers_unknown_keys_following_the_content_convention(self):
        # A trigger type added later gets covered without touching this module.
        assert redact_trigger_context(
            {"trigger_type": "future", "poll_answer_content": "text", "poll_id": "1"}
        ) == {
            "trigger_type": "future",
            "poll_answer_content": MESSAGE_CONTENT_PLACEHOLDER,
            "poll_id": "1",
        }

    def test_keeps_ids_flags_and_role_lists(self):
        context = {
            "trigger_type": "member_join",
            "member_id": "333",
            "guild_id": "111",
            "role_ids": ["1", "2"],
            "has_custom_avatar": True,
            "guild_member_count": 42,
        }
        assert redact_trigger_context(context) == context

    def test_empty_content_stays_empty(self):
        assert redact_trigger_context({"content": ""}) == {"content": ""}

    def test_null_content_stays_null(self):
        assert redact_trigger_context({"old_content": None}) == {"old_content": None}

    def test_an_empty_context_stays_empty(self):
        assert redact_trigger_context({}) == {}

    def test_does_not_mutate_its_argument(self):
        # The verbatim context still runs the handler; only the audit row is
        # redacted.
        context = {"content": "a DM", "attachments": [{"filename": "y.png"}]}
        redact_trigger_context(context)
        assert context == {"content": "a DM", "attachments": [{"filename": "y.png"}]}

    def test_is_idempotent(self):
        once = redact_trigger_context({"trigger_type": "message", "content": "hi"})
        assert redact_trigger_context(once) == once

    def test_the_redacted_copy_shares_nothing_with_the_original(self):
        # The caller keeps using the verbatim context after taking this copy —
        # a handler script runs against it — so a kept value that aliased the
        # original would still be changing after the audit copy was taken.
        context = {"author_role_ids": ["R1"], "payload": {"user_id": "U1"}}
        redacted = redact_trigger_context(context)
        context["author_role_ids"].append("what the script quoted back")
        assert redacted["author_role_ids"] == ["R1"]


def forum_post_request(**overrides) -> dict:
    """A forum-agent response request, the shape ``record_agent_response`` reads."""
    request = {
        "channel_id": "222222222222222222",
        "thread_id": "333333333333333333",
        "post_title": "Bot crashes on startup",
        "post_content": "here is my whole main.py and the traceback",
        "author_display_name": "Alice",
        "post_tags": ["python", "help"],
        "attachments": ["https://cdn.discordapp.com/attachments/1/2/traceback.txt"],
        "decision_reason": "question matches the agent's topic",
        "response_content": "check your event loop setup",
    }
    request.update(overrides)
    return request


class TestRedactForumPost:
    def test_replaces_the_title_a_member_typed(self):
        assert (
            redact_forum_post(forum_post_request())["post_title"]
            == MESSAGE_CONTENT_PLACEHOLDER
        )

    def test_replaces_the_body_a_member_typed(self):
        assert (
            redact_forum_post(forum_post_request())["post_content"]
            == MESSAGE_CONTENT_PLACEHOLDER
        )

    def test_drops_the_attachment_urls(self):
        assert redact_forum_post(forum_post_request())["attachments"] == []

    def test_returns_only_the_member_authored_columns(self):
        # The route spreads this into the row, so a request key that reached
        # the result would be stored under whatever name the sender chose.
        assert set(redact_forum_post(forum_post_request())) == {
            "post_title",
            "post_content",
            "attachments",
        }

    @pytest.mark.parametrize("field", ["post_title", "post_content"])
    def test_empty_text_stays_empty(self, field):
        redacted = redact_forum_post(forum_post_request(**{field: ""}))
        assert redacted[field] == ""

    @pytest.mark.parametrize("field", ["post_title", "post_content"])
    def test_null_text_becomes_the_empty_string_the_row_requires(self, field):
        # An image-only starter post sends null; both columns are not-null.
        redacted = redact_forum_post(forum_post_request(**{field: None}))
        assert redacted[field] == ""

    @pytest.mark.parametrize("field", ["post_title", "post_content"])
    def test_absent_text_becomes_the_empty_string_the_row_requires(self, field):
        request = forum_post_request()
        del request[field]
        assert redact_forum_post(request)[field] == ""

    def test_an_absent_attachment_list_is_still_empty(self):
        request = forum_post_request()
        del request["attachments"]
        assert redact_forum_post(request)["attachments"] == []

    def test_does_not_mutate_its_argument(self):
        request = forum_post_request()
        redact_forum_post(request)
        assert request["post_title"] == "Bot crashes on startup"
        assert request["post_content"] == (
            "here is my whole main.py and the traceback"
        )
        assert request["attachments"] == [
            "https://cdn.discordapp.com/attachments/1/2/traceback.txt"
        ]

    def test_is_idempotent(self):
        once = redact_forum_post(forum_post_request())
        assert redact_forum_post(once) == once


class TestOldestRetainedStreamId:
    def test_is_the_millisecond_id_of_the_retention_cutoff(self):
        now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        cutoff = now - CONTENT_RETENTION_WINDOW
        assert oldest_retained_stream_id(now) == f"{int(cutoff.timestamp() * 1000)}-0"

    def test_the_window_is_forty_eight_hours(self):
        assert CONTENT_RETENTION_WINDOW.total_seconds() == 48 * 60 * 60

    def test_rejects_a_naive_datetime(self):
        with pytest.raises(ValueError):
            oldest_retained_stream_id(datetime(2026, 7, 26, 12, 0))

    def test_moves_forward_with_the_clock(self):
        earlier = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        later = datetime(2026, 7, 26, 13, 0, tzinfo=UTC)
        assert oldest_retained_stream_id(later) > oldest_retained_stream_id(earlier)
