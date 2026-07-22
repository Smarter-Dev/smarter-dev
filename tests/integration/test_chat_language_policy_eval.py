"""Live eval for the chat agent's English-only conversation policy.

This exercises the production prompt, structured output, and multi-turn message
history against both selectable Gemini Flash Lite generations. It deliberately
does not mock model output: the assertions describe the user-visible contract
we want to compare across models.

Run with a real Google model key:

    uv run pytest tests/integration/test_chat_language_policy_eval.py \
        -m llm -q -s

The eval is excluded from the default suite by the existing ``not llm`` pytest
selection and skips cleanly when no Gemini/Google key is available.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from smarter_dev.bot.agents.chat_agent import get_chat_agent
from smarter_dev.bot.agents.chat_input_format import render_input_xml
from smarter_dev.bot.agents.chat_models import Author
from smarter_dev.bot.agents.chat_models import ChannelInfo
from smarter_dev.bot.agents.chat_models import FollowupAgentInput
from smarter_dev.bot.agents.chat_models import InitialAgentInput
from smarter_dev.bot.agents.chat_models import Me
from smarter_dev.bot.agents.chat_models import Message
from smarter_dev.bot.agents.chat_tools import ChatDeps

try:
    import dotenv

    dotenv.load_dotenv()
except ImportError:
    pass


MODEL_IDS = (
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash-lite",
)

_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(
        not _API_KEY,
        reason="GEMINI_API_KEY or GOOGLE_API_KEY is required for this live eval",
    ),
]

_BOT_ID = "999"
_USER_ID = "1"
_CHANNEL_ID = "100"


@dataclass(frozen=True)
class LanguageExchange:
    name: str
    first_message: str
    continued_message: str


EXCHANGES = (
    LanguageExchange(
        name="spanish",
        first_message=(
            "@smarterbot ¿Cómo arreglo un KeyError en Python cuando leo un diccionario?"
        ),
        continued_message=(
            "No voy a usar inglés. Explícame la solución en español, por favor."
        ),
    ),
    LanguageExchange(
        name="japanese",
        first_message=(
            "@smarterbot Pythonで非同期関数を実行するときのエラーを直す方法を教えて。"
        ),
        continued_message="英語は使いません。日本語で答えてください。",
    ),
)


def _deps() -> ChatDeps:
    bot = MagicMock()
    bot.rest = AsyncMock()
    return ChatDeps(bot=bot, channel_id=int(_CHANNEL_ID), guild_id=200)


def _initial_input(body: str, message_id: str) -> InitialAgentInput:
    return InitialAgentInput(
        me=Me(user_id=_BOT_ID, username="smarterbot"),
        channel_history=[],
        activation_message=Message(
            message_id=message_id,
            author_id=_USER_ID,
            body=body,
            mentions_bot=True,
            sent_at=datetime.now(UTC),
        ),
        authors=[Author(user_id=_USER_ID, username="alice")],
        channel=ChannelInfo(channel_id=_CHANNEL_ID, name="dev-help"),
        now_utc=datetime.now(UTC),
    )


def _followup_input(body: str, message_id: str) -> FollowupAgentInput:
    return FollowupAgentInput(
        me=Me(user_id=_BOT_ID, username="smarterbot"),
        new_messages=[
            Message(
                message_id=message_id,
                author_id=_USER_ID,
                reply_to_message_id="bot-warning",
                reply_to_author_id=_BOT_ID,
                reply_to_is_self=True,
                body=body,
                sent_at=datetime.now(UTC),
            )
        ],
        authors=[Author(user_id=_USER_ID, username="alice")],
        channel=ChannelInfo(channel_id=_CHANNEL_ID, name="dev-help"),
        now_utc=datetime.now(UTC),
    )


def _response_text(output) -> str:
    if output.response is None:
        return ""
    return (output.response.message or output.response.voice_summary or "").strip()


def _rankings(output) -> list[dict]:
    return [ranking.model_dump(mode="json") for ranking in output.rankings]


async def _run_exchange(agent, exchange: LanguageExchange, sequence: int) -> dict:
    started = time.monotonic()
    first = await agent.run(
        user_prompt=render_input_xml(
            _initial_input(exchange.first_message, f"{sequence}01")
        ),
        deps=_deps(),
    )
    continued = await agent.run(
        user_prompt=render_input_xml(
            _followup_input(exchange.continued_message, f"{sequence}02")
        ),
        message_history=first.all_messages(),
        deps=_deps(),
    )
    reactivated = await agent.run(
        user_prompt=render_input_xml(
            _initial_input(exchange.continued_message, f"{sequence}03")
        ),
        deps=_deps(),
    )
    first_text = _response_text(first.output)
    continued_text = _response_text(continued.output)
    reactivated_text = _response_text(reactivated.output)
    return {
        "case": exchange.name,
        "first_language": first.output.response_language,
        "first_responded": first.output.response is not None,
        "first_text": first_text,
        "first_rankings": _rankings(first.output),
        "first_mentions_english": "english" in first_text.lower(),
        "first_is_short": len(first_text) <= 240,
        "continued_language": continued.output.response_language,
        "continued_silent": continued.output.response is None,
        "continued_text": continued_text,
        "continued_rankings": _rankings(continued.output),
        "reactivated_language": reactivated.output.response_language,
        "reactivated_silent": reactivated.output.response is None,
        "reactivated_rewarned": (
            reactivated.output.response is not None
            and "english" in reactivated_text.lower()
            and len(reactivated_text) <= 240
        ),
        "reactivated_text": reactivated_text,
        "reactivated_rankings": _rankings(reactivated.output),
        "seconds": round(time.monotonic() - started, 2),
    }


async def _run_embedded_foreign_text_control(agent) -> dict:
    body = (
        "@smarterbot My Java compiler prints `erreur: symbole introuvable` after "
        "I renamed a method. What does that usually mean?"
    )
    result = await agent.run(
        user_prompt=render_input_xml(_initial_input(body, "9001")),
        deps=_deps(),
    )
    text = _response_text(result.output)
    return {
        "case": "english_with_french_error",
        "response_language": result.output.response_language,
        "responded": result.output.response is not None,
        "text": text,
        "redirected_to_english": "english" in text.lower(),
    }


@pytest.mark.parametrize("model_id", MODEL_IDS)
async def test_chat_language_policy(model_id: str):
    """Compare warning, repeated-message silence, and false-positive behavior."""
    agent = get_chat_agent(model_id)
    results = [
        await _run_exchange(agent, exchange, index)
        for index, exchange in enumerate(EXCHANGES, start=1)
    ]
    control = await _run_embedded_foreign_text_control(agent)
    report = {"model": model_id, "exchanges": results, "control": control}
    print("\nLANGUAGE_POLICY_EVAL=" + json.dumps(report, ensure_ascii=False))

    failures: list[str] = []
    for result in results:
        case = result["case"]
        if result["first_language"] != case:
            failures.append(
                f"{case}: first message classified as "
                f"{result['first_language']!r}"
            )
        if not result["first_responded"]:
            failures.append(f"{case}: first non-English message got no warning")
        elif not result["first_mentions_english"]:
            failures.append(f"{case}: first response did not ask for English")
        if not result["first_is_short"]:
            failures.append(f"{case}: first warning exceeded 240 characters")
        if result["continued_language"] != case:
            failures.append(
                f"{case}: continued message classified as "
                f"{result['continued_language']!r}"
            )
        if not result["continued_silent"]:
            failures.append(
                f"{case}: repeated non-English message got another response"
            )
        if result["reactivated_language"] != case:
            failures.append(
                f"{case}: reactivated message classified as "
                f"{result['reactivated_language']!r}"
            )
        if not result["reactivated_rewarned"]:
            failures.append(
                f"{case}: fresh engagement did not receive an English warning"
            )
    if control["response_language"] != "english":
        failures.append(
            "control: English message classified as "
            f"{control['response_language']!r}"
        )
    if not control["responded"]:
        failures.append("control: English question with a French error was ignored")
    elif control["redirected_to_english"]:
        failures.append(
            "control: embedded French error triggered an English-only redirect"
        )

    assert not failures, "\n".join(failures)
