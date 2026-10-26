"""/tldr, /help and the timeout announcement on pydantic-ai.

Each test swaps a FunctionModel in for the routed model, so these check what
the agent sends, what it posts and what it reports as token usage.
"""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.agents import help_agent
from smarter_dev.bot.agents import tldr_agent
from smarter_dev.bot.agents import utility_model
from smarter_dev.bot.agents.models import DiscordMessage
from smarter_dev.bot.plugins import timeout


@pytest.fixture(autouse=True)
def _model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real model is built before the test swaps in its FunctionModel.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _prompt(messages: list[ModelMessage]) -> str:
    return "\n".join(
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


def _answering(payload: dict, seen: list[str]) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(_prompt(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, payload)])

    return FunctionModel(respond)


def _failing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise RuntimeError("provider down")


# -- the routed model ----------------------------------------------------------


def test_the_utility_model_is_claude_haiku_5_5_through_the_router() -> None:
    agent = utility_model.build_utility_agent(str, "be brief")

    assert isinstance(agent.model, AnthropicModel)
    assert agent.model.model_name == "claude-haiku-5-5"
    # The catalog's medium effort, as Luna ran at; one request, so no message
    # cache breakpoint; and the output tool is never forced.
    assert agent.model_settings == {
        "anthropic_thinking": {"type": "adaptive"},
        "anthropic_effort": "medium",
        "anthropic_cache_messages": False,
    }
    assert agent.model.profile["supports_forced_tool_choice"] is False


def test_render_inputs_labels_each_field() -> None:
    assert utility_model.render_inputs(a=1, b="two") == "## a\n1\n\n## b\ntwo"


# -- /tldr -----------------------------------------------------------------------


def _messages() -> list[DiscordMessage]:
    now = datetime.now(UTC)
    return [
        DiscordMessage(author="alice", timestamp=now, content="ship it?"),
        DiscordMessage(
            author="bob", timestamp=now + timedelta(seconds=5), content="after tests"
        ),
    ]


async def test_tldr_posts_the_summary_with_the_count_and_real_tokens() -> None:
    seen: list[str] = []
    model = _answering({"summary": "**Channel Summary**\nThey agreed to test first."}, seen)

    with tldr_agent.get_tldr_agent().override(model=model):
        summary, tokens, used = await tldr_agent.TLDRAgent().generate_summary_async(
            _messages()
        )

    assert summary == (
        "**Channel Summary**\nThey agreed to test first.\n\n*(Summarized 2 messages)*"
    )
    assert used == 2
    assert tokens > 0
    assert "<author>alice</author>" in seen[0]
    assert "<content>after tests</content>" in seen[0]


async def test_tldr_failure_returns_the_apology_and_no_tokens() -> None:
    with tldr_agent.get_tldr_agent().override(model=FunctionModel(_failing)):
        summary, tokens, used = await tldr_agent.TLDRAgent().generate_summary_async(
            _messages()
        )

    assert summary.startswith("**Channel Summary**\nSorry")
    assert (tokens, used) == (0, 0)


async def test_tldr_with_no_messages_makes_no_model_call() -> None:
    def never(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("no model call for an empty channel")

    with tldr_agent.get_tldr_agent().override(model=FunctionModel(never)):
        summary, tokens, used = await tldr_agent.TLDRAgent().generate_summary_async([])

    assert "No messages found" in summary
    assert (tokens, used) == (0, 0)


# -- /help -------------------------------------------------------------------------


_CONTEXT = {
    "conversation_timeline": "[NEW] alice: how do squads work?",
    "users": [{"user_id": "1", "discord_name": "alice", "role_names": ["dev"]}],
    "channel": {"name": "general", "description": "chat"},
    "me": {"bot_name": "Bot", "bot_id": "99"},
}


async def _help(model: FunctionModel) -> tuple[str, int]:
    builder = AsyncMock()
    builder.build_context.return_value = _CONTEXT
    with (
        patch(
            "smarter_dev.bot.utils.messages.ConversationContextBuilder",
            return_value=builder,
        ),
        help_agent.get_help_agent().override(model=model),
    ):
        return await help_agent.HelpAgent().generate_response(
            user_question="how do squads work?",
            bot=object(),
            channel_id=1,
            guild_id=2,
            messages_remaining=3,
        )


async def test_help_sends_every_input_and_returns_real_tokens() -> None:
    seen: list[str] = []

    response, tokens = await _help(_answering({"response": " Use /squads join. "}, seen))

    assert response == "Use /squads join."
    assert tokens > 0
    prompt = seen[0]
    assert "## conversation_timeline\n[NEW] alice: how do squads work?" in prompt
    assert f"## users\n{json.dumps(_CONTEXT['users'])}" in prompt
    assert f"## channel\n{json.dumps(_CONTEXT['channel'])}" in prompt
    assert f"## me\n{json.dumps(_CONTEXT['me'])}" in prompt
    assert "## user_question\nhow do squads work?" in prompt
    assert "## messages_remaining\n3" in prompt


async def test_help_truncates_to_discords_limit() -> None:
    response, _ = await _help(_answering({"response": "a" * 2500}, []))

    assert len(response) == 2000
    assert response.endswith("...")


async def test_help_failure_propagates_to_the_plugins_error_reply() -> None:
    with pytest.raises(RuntimeError):
        await _help(FunctionModel(_failing))


# -- timeout announcement ----------------------------------------------------------


@pytest.mark.parametrize(
    ("duration", "display"),
    [
        (timedelta(minutes=1), "1 minute"),
        (timedelta(minutes=10), "10 minutes"),
        (timedelta(hours=1), "1 hour"),
        (timedelta(hours=5), "5 hours"),
        (timedelta(days=2), "2 days"),
    ],
)
def test_format_duration(duration: timedelta, display: str) -> None:
    assert timeout.format_duration(duration) == display


async def test_the_timeout_announcement_rewrites_the_reason() -> None:
    seen: list[str] = []
    model = _answering(
        {"polite_message": " <@7> has been timed out for excessive messaging. "}, seen
    )

    with timeout.get_timeout_message_agent().override(model=model):
        message = await timeout.generate_timeout_message(
            "<@7>", "spamming like crazy", "10 minutes"
        )

    assert message == "<@7> has been timed out for excessive messaging."
    assert "## user_mention\n<@7>" in seen[0]
    assert "## raw_reason\nspamming like crazy" in seen[0]
    assert "## duration\n10 minutes" in seen[0]


def test_the_timeout_instructions_keep_the_moderators_wording() -> None:
    instructions = timeout.TIMEOUT_MESSAGE_INSTRUCTIONS

    assert "Keep the moderator's own keywords and key phrases" in instructions
    assert "into the third person" in instructions
    assert "Drop insults, profanity and personal jabs" in instructions
    assert "1-2 sentences" in instructions
    # The examples carry the reason's words through, never a generic summary.
    assert "not listening to others who know" in instructions
    assert "You don't know what you don't know" in instructions
    assert "has been timed out for inappropriate behavior" not in instructions
