"""Tests for the per-turn ``<your-model>`` metadata tag in the agent input.

The model identity rides in every turn's metadata block — NOT the system
prompt — because pydantic-ai only applies the system prompt when the message
history is empty, so a mid-engagement turn (or a mid-engagement model switch)
would otherwise leave the agent not knowing what it runs on.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

from smarter_dev.bot.agents.chat_input_format import build_agent_call
from smarter_dev.bot.agents.chat_models import Author
from smarter_dev.bot.agents.chat_models import ChannelInfo
from smarter_dev.bot.agents.chat_models import InitialAgentInput
from smarter_dev.bot.agents.chat_models import Me
from smarter_dev.bot.agents.chat_models import Message


def _initial_input() -> InitialAgentInput:
    return InitialAgentInput(
        me=Me(user_id="999", username="bot"),
        channel_history=[],
        activation_message=Message(
            message_id="101",
            author_id="200",
            body="@bot which model are you?",
            mentions_bot=True,
        ),
        authors=[Author(user_id="200", username="alice")],
        channel=ChannelInfo(channel_id="1", name="general"),
        now_utc=datetime.now(UTC),
    )


def test_metadata_carries_model_identity_with_reasoning():
    user_prompt, _ = build_agent_call(
        _initial_input(), [], model_name="gpt-5.4", reasoning_level="high"
    )
    assert (
        '<your-model id="gpt-5.4" name="GPT-5.4" reasoning-level="high"/>'
        in user_prompt
    )


def test_metadata_model_without_reasoning_omits_the_attribute():
    user_prompt, _ = build_agent_call(
        _initial_input(), [], model_name="kimi-k2.6", reasoning_level=None
    )
    assert '<your-model id="kimi-k2.6" name="Kimi K2.6 (Moonshot)"/>' in user_prompt


def test_metadata_adhoc_model_id_renders_without_label():
    user_prompt, _ = build_agent_call(
        _initial_input(), [], model_name="some-unlisted-model"
    )
    assert '<your-model id="some-unlisted-model"/>' in user_prompt


def test_metadata_omits_your_model_when_name_unset():
    user_prompt, _ = build_agent_call(_initial_input(), [])
    assert "<your-model" not in user_prompt
