"""Live ping-pong proof that every DigitalOcean-routed catalog model can drive
the tool-calling chat agent.

The chat agent returns its structured ``TurnDecision`` via a function/tool call
and registers a full tool surface. A review finding warned that some models
served on DigitalOcean's OpenAI-compatible endpoint may not support function
calling — which would break every turn in an overridden channel. This module
exercises the REAL agent against each DO catalog model with a short exchange and
asserts the run completes with a non-empty structured reply and no tool-calling
errors.

The parametrization is derived from the catalog at collection time (every model
whose provider is DIGITALOCEAN), so new/removed DO models flow through
automatically without editing this file.

This is a LIVE test: it is marked ``do_live`` and skipped whenever
``DIGITALOCEAN_INFERENCE_API_KEY`` is absent, so it is safely collectable with no
network and no key and never runs in the default suite.

Run it (needs a real DigitalOcean inference key in the environment):
    uv run pytest tests/integration/test_do_model_pingpong.py -q
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic_ai.messages import ModelRequest, RetryPromptPart

from smarter_dev.bot.agents.chat_agent import get_chat_agent
from smarter_dev.bot.agents.chat_input_format import render_input_xml
from smarter_dev.bot.agents.chat_models import (
    Author,
    ChannelInfo,
    FollowupAgentInput,
    InitialAgentInput,
    Me,
    Message,
    TurnDecision,
)
from smarter_dev.bot.agents.chat_tools import ChatDeps
from smarter_dev.bot.agents.model_router import DIGITALOCEAN_API_KEY_ENV_VAR
from smarter_dev.shared.model_catalog import MODEL_CATALOG, CatalogModel, ModelProvider

try:  # make a local .env's DO key visible to skipif at collection time
    import dotenv

    dotenv.load_dotenv()
except ImportError:  # dotenv is an optional dev convenience
    pass


# Derived from the catalog so catalog changes flow through automatically — never
# hardcode model ids here.
DIGITALOCEAN_MODELS: tuple[CatalogModel, ...] = tuple(
    model for model in MODEL_CATALOG if model.provider is ModelProvider.DIGITALOCEAN
)

pytestmark = [
    pytest.mark.do_live,
    pytest.mark.skipif(
        not os.getenv(DIGITALOCEAN_API_KEY_ENV_VAR),
        reason=f"{DIGITALOCEAN_API_KEY_ENV_VAR} not set — live DO model test skipped",
    ),
]

# Bot ids are irrelevant to a pure text exchange, but the agent must be able to
# recognise its own identity and score the activation message as aimed at it.
_BOT_USER_ID = "1"
_HUMAN_USER_ID = "2"
_CHANNEL_ID = 100
_GUILD_ID = 200


def _mock_deps() -> ChatDeps:
    """A ChatDeps whose bot never touches the network.

    The ping-pong prompts don't need any Discord tool, but a mock REST client
    keeps an unexpected best-effort status post from crashing the run.
    """
    bot = MagicMock()
    bot.rest = AsyncMock()
    return ChatDeps(bot=bot, channel_id=_CHANNEL_ID, guild_id=_GUILD_ID)


def _initial_input(activation_body: str) -> InitialAgentInput:
    """Build a minimal first-turn input whose activation message pings the bot."""
    return InitialAgentInput(
        me=Me(user_id=_BOT_USER_ID, username="smarterbot"),
        channel_history=[],
        activation_message=Message(
            message_id="10",
            author_id=_HUMAN_USER_ID,
            body=activation_body,
            mentions_bot=True,
            sent_at=datetime.now(timezone.utc),
        ),
        authors=[Author(user_id=_HUMAN_USER_ID, username="alice")],
        channel=ChannelInfo(channel_id=str(_CHANNEL_ID), name="general"),
        now_utc=datetime.now(timezone.utc),
    )


def _followup_input(new_body: str) -> FollowupAgentInput:
    """Build a follow-up input carrying a single new pinging message."""
    return FollowupAgentInput(
        me=Me(user_id=_BOT_USER_ID, username="smarterbot"),
        new_messages=[
            Message(
                message_id="11",
                author_id=_HUMAN_USER_ID,
                body=new_body,
                mentions_bot=True,
                sent_at=datetime.now(timezone.utc),
            )
        ],
        authors=[Author(user_id=_HUMAN_USER_ID, username="alice")],
        channel=ChannelInfo(channel_id=str(_CHANNEL_ID), name="general"),
        now_utc=datetime.now(timezone.utc),
    )


def _tool_error_retries(messages: list) -> list[RetryPromptPart]:
    """Return every RetryPromptPart in ``messages``.

    pydantic_ai emits a RetryPromptPart when a tool call or the structured
    output tool fails validation and the model is asked to try again — the
    surfaced signature of a function-calling problem on the provider endpoint.
    A clean single-pass structured reply produces none.
    """
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, RetryPromptPart)
    ]


def _assert_structured_reply(result, model: CatalogModel) -> None:
    """Assert the run produced a non-empty TurnDecision with no tool errors."""
    output = result.output
    assert isinstance(output, TurnDecision), (
        f"{model.key}: expected a TurnDecision, got {type(output)!r}"
    )
    assert output.rankings, f"{model.key}: structured reply had no rankings"
    assert output.topic.strip(), f"{model.key}: structured reply had an empty topic"
    # A single recovered retry is tolerable (pydantic_ai nudges the model and
    # the turn still succeeds); repeated retries mean structured output is
    # effectively broken for this model.
    retries = _tool_error_retries(result.all_messages())
    assert len(retries) <= 1, (
        f"{model.key}: model surfaced {len(retries)} structured-output/tool "
        f"error(s): {[part.tool_name for part in retries]}"
    )


@pytest.mark.parametrize(
    "model",
    DIGITALOCEAN_MODELS,
    ids=[model.key for model in DIGITALOCEAN_MODELS],
)
async def test_digitalocean_model_drives_tool_calling_agent(model: CatalogModel):
    """Each DO model runs a two-turn ping-pong through the real chat agent.

    Turn one elicits a structured reply; turn two feeds the agent's own reply
    back so the follow-up path (with prior tool history) is exercised too. The
    run must not raise, must return a non-empty structured ``TurnDecision``, and
    must surface no tool-calling/function-calling errors.
    """
    agent = get_chat_agent(model.model_id)

    first = await agent.run(
        user_prompt=render_input_xml(_initial_input("Hey bot, say hi in one sentence.")),
        deps=_mock_deps(),
    )
    _assert_structured_reply(first, model)

    second = await agent.run(
        user_prompt=render_input_xml(_followup_input("Thanks! Now count to three.")),
        deps=_mock_deps(),
        message_history=first.all_messages(),
    )
    _assert_structured_reply(second, model)
