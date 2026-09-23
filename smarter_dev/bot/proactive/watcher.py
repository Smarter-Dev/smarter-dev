"""Pass 1: the stateless watcher, plus the generative skim helper.

The watcher gets a fresh history every call: current wake criteria (seed
policy + the agent's addendum), a context tail, and the burst's new
messages. It decides whether to wake the K3 agent and selects the verbatim
snippets the wake brief carries. `SkimRunner` is the same model serving the
agent's skim tool: summarize a transcript with verbatim snippets and
message/user ids.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from pydantic import BaseModel
from pydantic import Field
from pydantic import PrivateAttr
from pydantic_ai import Agent
from pydantic_ai import PromptedOutput
from pydantic_ai.models import Model


class WatcherDecision(BaseModel):
    wake: bool
    reason: str = ""
    relevant_message_ids: list[str] = []
    summary: str = ""
    _classifier_metadata: dict[str, Any] = PrivateAttr(default_factory=dict)

    def with_classifier_metadata(self, metadata: dict[str, Any]) -> WatcherDecision:
        self._classifier_metadata = metadata
        return self

    def details(self) -> dict[str, Any]:
        details = self.model_dump()
        if self._classifier_metadata:
            details["classifier"] = self._classifier_metadata
        return details


class JevWatcherJudgments(BaseModel):
    """Independent judgments used to apply the proactive wake policy."""

    addressed_to_bot_by_name: bool = Field(
        description=(
            "Do any NEW MESSAGES address the bot by its display name, even "
            "without a Discord mention?"
        )
    )
    matches_watch_criteria: bool = Field(
        description=(
            "Do any NEW MESSAGES match the explicit wake criteria supplied "
            "with this classification?"
        )
    )
    useful_intervention: bool = Field(
        description=(
            "Do the NEW MESSAGES contain a concrete question, problem, or "
            "open discussion where this bot could provide useful help now?"
        )
    )
    specific_people_exchange: bool = Field(
        description=(
            "Are the NEW MESSAGES primarily a conversation addressed to one "
            "or more specific people other than the bot?"
        )
    )


WATCHER_SYSTEM_PROMPT = """\
You are the watch pass of a two-pass Discord bot. You review new channel
messages and decide whether to wake the chat agent (a slower, smarter
model). You never write channel messages yourself.

Wake the agent when a new message is directed at the bot (mention or
reply), when the wake criteria below say so, or when a conversation the
agent could genuinely help with is happening. Most bursts of chat are
between specific people or ambient noise: do not wake for those.

When you wake the agent, list the message ids it must look at and write a
one-paragraph summary of what is going on."""


def build_watcher_prompt(
    *,
    instructions: str,
    context_transcript: str,
    new_transcript: str,
    bot_user_id: str,
    bot_display_name: str = "the bot",
) -> str:
    return f"""\
The bot goes by the name "{bot_display_name}" and its Discord user id is \
{bot_user_id}: `<@{bot_user_id}>` inside a message is a mention of the bot, \
a message addressing "{bot_display_name}" by name (with or without an @) is \
directed at the bot, and transcript lines marked [BOT] are the bot's own \
messages.

WAKE CRITERIA:
{instructions}

RECENT CHANNEL CONTEXT (already handled on earlier wakes):
{context_transcript}

NEW MESSAGES since the last watch:
{new_transcript}

Decide: wake the agent or not."""


def build_jev_watcher_material(
    *, context_transcript: str, new_transcript: str
) -> str:
    """Only the material Jev judges; questions and policy live elsewhere."""
    return f"""\
RECENT CHANNEL CONTEXT (already handled on earlier wakes):
{context_transcript}

NEW MESSAGES since the last watch:
{new_transcript}"""


def build_jev_watcher_instructions(
    *,
    instructions: str,
    bot_user_id: str,
    bot_display_name: str,
) -> str:
    """Framing shared by Jev's field-level questions."""
    return f"""\
Classify Discord activity for a proactive bot. The bot's display name is
"{bot_display_name}" and its Discord user id is {bot_user_id}. Judge only the
section labeled NEW MESSAGES; use RECENT CHANNEL CONTEXT only for meaning.

Explicit wake criteria:
{instructions}"""


def usage_dict(usage) -> dict:
    if callable(usage):
        usage = usage()
    return {
        "input_tokens": usage.input_tokens or 0,
        "output_tokens": usage.output_tokens or 0,
        "cache_read_tokens": usage.cache_read_tokens or 0,
    }


@dataclass
class WatcherRunner:
    model: Model | str
    # Open-weight endpoints need the output schema prompted; tests pass
    # False so TestModel can use native structured output.
    prompted_output: bool = True
    _agent: Agent = field(init=False, repr=False)

    def __post_init__(self) -> None:
        output_type = (
            PromptedOutput(WatcherDecision)
            if self.prompted_output
            else WatcherDecision
        )
        self._agent = Agent(
            self.model,
            output_type=output_type,
            system_prompt=WATCHER_SYSTEM_PROMPT,
        )

    async def decide(
        self,
        *,
        instructions: str,
        context_transcript: str,
        new_transcript: str,
        bot_user_id: str,
        bot_display_name: str = "the bot",
        new_message_ids: list[str] | None = None,
    ) -> tuple[WatcherDecision, dict]:
        result = await self._agent.run(
            build_watcher_prompt(
                instructions=instructions,
                context_transcript=context_transcript,
                new_transcript=new_transcript,
                bot_user_id=bot_user_id,
                bot_display_name=bot_display_name,
            )
        )
        run_usage = result.usage() if callable(result.usage) else result.usage
        decision = result.output.with_classifier_metadata(
            {
                "provider": "generative",
                "resolved_model_name": result.response.model_name,
                "requests": getattr(run_usage, "requests", 0) or 0,
                "fallback_used": False,
                "abstained": False,
            }
        )
        return decision, usage_dict(run_usage)


def _minimum_confidence(
    provider_details: dict[str, Any],
) -> tuple[float | None, list[str]]:
    confidence = provider_details.get("confidence") or {}
    if not confidence:
        return None, []
    minimum = min(float(value) for value in confidence.values())
    return minimum, [
        name for name, value in confidence.items() if float(value) == minimum
    ]


def decision_from_jev(
    judgments: JevWatcherJudgments,
    *,
    new_message_ids: list[str],
    provider_details: dict[str, Any],
    resolved_model_name: str | None,
    minimum_confidence: float,
) -> WatcherDecision:
    """Turn Jev's atomic judgments into the existing watcher contract.

    Jev cannot generate the old free-text reason/summary or safely invent a
    subset of arbitrary message ids.  The wake brief therefore carries every
    message in the just-classified burst and a deterministic explanation.
    """
    confidence, least_confident_fields = _minimum_confidence(provider_details)
    low_confidence = confidence is not None and confidence < minimum_confidence
    policy_wake = (
        judgments.addressed_to_bot_by_name
        or judgments.matches_watch_criteria
        or (
            judgments.useful_intervention
            and not judgments.specific_people_exchange
        )
    )
    triggers = [
        name
        for name, value in judgments.model_dump().items()
        if value and name != "specific_people_exchange"
    ]
    wake = policy_wake and not low_confidence
    if low_confidence:
        reason = "Jev abstained because at least one judgment was low-confidence."
    elif triggers:
        reason = "Jev wake triggers: " + ", ".join(triggers) + "."
    else:
        reason = "Jev found no wake trigger."
    decision = WatcherDecision(
        wake=wake,
        reason=reason,
        relevant_message_ids=list(new_message_ids) if wake else [],
        summary=(
            "Review the complete newly classified message burst."
            if wake
            else ""
        ),
    )
    return decision.with_classifier_metadata(
        {
            "provider": "typesafe",
            "resolved_model_name": resolved_model_name,
            "judgments": judgments.model_dump(),
            "confidence": provider_details.get("confidence") or {},
            "probabilities": provider_details.get("probabilities") or {},
            "scores": provider_details.get("scores") or {},
            "minimum_confidence": confidence,
            "minimum_confidence_threshold": minimum_confidence,
            "least_confident_fields": least_confident_fields,
            "abstained": low_confidence,
            "fallback_used": False,
        }
    )


@dataclass
class JevWatcherRunner:
    """Classifier-only watcher using Pydantic AI's native TypeSafe model."""

    model: Model | str
    model_id: str = "typesafe:jev-latest"
    fallback: WatcherRunner | None = None
    fallback_model_id: str | None = None
    boolean_threshold: float = 0.5
    minimum_confidence: float = 0.2
    timeout_seconds: float = 30.0
    retries: int = 0
    _agent: Agent = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # A classifier failure is safer to expose as an abstention/fallback than
        # to hide behind an additional paid prediction.  Callers that explicitly
        # want result-validation retries can opt in.
        self._agent = Agent(
            self.model,
            output_type=JevWatcherJudgments,
            retries=self.retries,
        )

    async def _fallback(
        self,
        *,
        cause: str,
        instructions: str,
        context_transcript: str,
        new_transcript: str,
        bot_user_id: str,
        bot_display_name: str,
        new_message_ids: list[str],
        primary_usage: dict[str, int] | None = None,
    ) -> tuple[WatcherDecision, dict]:
        if self.fallback is None:
            decision = WatcherDecision(
                wake=False,
                reason=f"Jev abstained after {cause}; no fallback is configured.",
            ).with_classifier_metadata(
                {
                    "provider": "typesafe",
                    "abstained": True,
                    "fallback_used": False,
                    "failure": cause,
                }
            )
            return decision, primary_usage or {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
            }
        decision, usage = await self.fallback.decide(
            instructions=instructions,
            context_transcript=context_transcript,
            new_transcript=new_transcript,
            bot_user_id=bot_user_id,
            bot_display_name=bot_display_name,
            new_message_ids=new_message_ids,
        )
        fallback_metadata = dict(decision._classifier_metadata)
        decision = decision.with_classifier_metadata(
            {
                "provider": "typesafe",
                "abstained": True,
                "fallback_used": True,
                "failure": cause,
                "fallback": fallback_metadata,
                "requests": fallback_metadata.get("requests", 0),
            }
        )
        if self.fallback_model_id is None:
            return decision, usage
        usage_by_model = {self.fallback_model_id: usage}
        if primary_usage is not None:
            usage_by_model[self.model_id] = primary_usage
        totals = {
            key: sum(item.get(key, 0) for item in usage_by_model.values())
            for key in ("input_tokens", "output_tokens", "cache_read_tokens")
        }
        return decision, {**totals, "usage_by_model": usage_by_model}

    async def decide(
        self,
        *,
        instructions: str,
        context_transcript: str,
        new_transcript: str,
        bot_user_id: str,
        bot_display_name: str = "the bot",
        new_message_ids: list[str] | None = None,
    ) -> tuple[WatcherDecision, dict]:
        message_ids = list(new_message_ids or [])
        try:
            async with asyncio.timeout(self.timeout_seconds):
                result = await self._agent.run(
                    build_jev_watcher_material(
                        context_transcript=context_transcript,
                        new_transcript=new_transcript,
                    ),
                    instructions=build_jev_watcher_instructions(
                        instructions=instructions,
                        bot_user_id=bot_user_id,
                        bot_display_name=bot_display_name,
                    ),
                    model_settings={
                        "timeout": self.timeout_seconds,
                        "typesafe_boolean_threshold": self.boolean_threshold,
                    },
                )
        except TimeoutError:
            return await self._fallback(
                cause="timeout",
                instructions=instructions,
                context_transcript=context_transcript,
                new_transcript=new_transcript,
                bot_user_id=bot_user_id,
                bot_display_name=bot_display_name,
                new_message_ids=message_ids,
            )
        except Exception as error:  # noqa: BLE001 - optional safe fallback
            return await self._fallback(
                cause=f"{type(error).__name__}",
                instructions=instructions,
                context_transcript=context_transcript,
                new_transcript=new_transcript,
                bot_user_id=bot_user_id,
                bot_display_name=bot_display_name,
                new_message_ids=message_ids,
            )

        provider_details = result.response.provider_details or {}
        decision = decision_from_jev(
            result.output,
            new_message_ids=message_ids,
            provider_details=provider_details,
            resolved_model_name=result.response.model_name,
            minimum_confidence=self.minimum_confidence,
        )
        jev_usage = usage_dict(result.usage)
        run_usage = result.usage() if callable(result.usage) else result.usage
        decision._classifier_metadata["requests"] = (
            getattr(run_usage, "requests", 0) or 0
        )
        if decision._classifier_metadata.get("abstained"):
            fallback_decision, fallback_usage = await self._fallback(
                cause="low_confidence",
                instructions=instructions,
                context_transcript=context_transcript,
                new_transcript=new_transcript,
                bot_user_id=bot_user_id,
                bot_display_name=bot_display_name,
                new_message_ids=message_ids,
                primary_usage=jev_usage,
            )
            if self.fallback is not None:
                fallback_decision._classifier_metadata["jev"] = (
                    decision._classifier_metadata
                )
                return fallback_decision, fallback_usage
        return decision, jev_usage


SKIM_SYSTEM_PROMPT = """\
You skim Discord transcripts for a chat agent. Summarize what is happening
in a short paragraph, then list the load-bearing messages VERBATIM as
`[id=<message id>] <display name> (user id <author id>): <content>` lines.
Keep it brief; the agent can look up anything by id."""


@dataclass
class SkimRunner:
    model: Model | str
    _agent: Agent = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = Agent(
            self.model, output_type=str, system_prompt=SKIM_SYSTEM_PROMPT
        )

    async def skim(self, transcript: str) -> tuple[str, dict]:
        result = await self._agent.run(transcript)
        return result.output, usage_dict(result.usage)
