"""Bot adapters for the activation simulator.

`SilentAdapter` never speaks (harness smoke tests, lower cost bound).
`BaselineAdapter` is a placeholder for the unbuilt proactive bot: one
pydantic-ai agent call per activation, proving the harness end-to-end and
producing a realistic cost floor. The real bot later implements the same
`ProactiveBotAdapter` protocol from simulation.py.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel
from pydantic_ai import Agent

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.proactive_eval.labels import (  # noqa: E402
    render_transcript_line,
    speaker_tags,
)
from scripts.proactive_eval.simulation import (  # noqa: E402
    ActivationContext,
    ActivationResult,
    ProposedResponse,
)
from smarter_dev.bot.agents.chat_agent import build_agent_model  # noqa: E402

MAX_RESPONSES_PER_ACTIVATION = 2


@dataclass
class SilentAdapter:
    """Always stays silent; costs nothing."""

    model_id: str = "none"

    async def activate(self, context: ActivationContext) -> ActivationResult:
        return ActivationResult(
            responses=[],
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            model_id=self.model_id,
        )


class BaselineResponse(BaseModel):
    reply_to_id: str | None = None
    content: str


class BaselineOutput(BaseModel):
    responses: list[BaselineResponse] = []


def proposed_responses(output: BaselineOutput) -> list[ProposedResponse]:
    """Clamp the model's responses to the per-activation maximum."""
    return [
        ProposedResponse(reply_to_id=r.reply_to_id, content=r.content)
        for r in output.responses[:MAX_RESPONSES_PER_ACTIVATION]
    ]


def build_baseline_system_prompt(
    *,
    bot_display_name: str,
    guild_name: str,
    channel_name: str,
    cadence_seconds: int,
) -> str:
    cadence_minutes = max(1, round(cadence_seconds / 60))
    return f"""\
You are {bot_display_name}, a member of the #{channel_name} channel in the \
{guild_name} Discord server. You wake every {cadence_minutes} minutes and \
review the messages sent since your last wake.

Rules:
- You may reply ONLY to messages addressed to the whole room or to you.
- Never insert yourself into an exchange between specific people — replies \
to someone else, @mentions of someone else, or an ongoing back-and-forth \
between two people are not yours to answer.
- Most wakes nothing warrants a reply: return an empty responses list.
- At most {MAX_RESPONSES_PER_ACTIVATION} responses per wake.
- Set reply_to_id to the id of the message you are responding to, or null \
for a standalone message to the room."""


def render_activation_prompt(context: ActivationContext) -> str:
    """Transcript prompt: HISTORY block, then the NEW MESSAGES to consider."""
    records = [m.to_record() for m in context.history + context.new_messages]
    tags = speaker_tags(records)
    history_lines = "\n".join(
        render_transcript_line(m.to_record(), tags) for m in context.history
    ) or "(none)"
    new_lines = "\n".join(
        render_transcript_line(m.to_record(), tags) for m in context.new_messages
    )
    return f"""\
HISTORY (already seen on earlier wakes, oldest first):
{history_lines}

NEW MESSAGES since your last wake:
{new_lines}"""


@dataclass
class BaselineAdapter:
    """One structured pydantic-ai call per activation."""

    model_id: str
    bot_display_name: str
    guild_name: str
    channel_name: str
    cadence_seconds: int
    _agent: Agent = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._agent = Agent(
            build_agent_model(self.model_id),
            output_type=BaselineOutput,
            system_prompt=build_baseline_system_prompt(
                bot_display_name=self.bot_display_name,
                guild_name=self.guild_name,
                channel_name=self.channel_name,
                cadence_seconds=self.cadence_seconds,
            ),
        )

    async def activate(self, context: ActivationContext) -> ActivationResult:
        run = await self._agent.run(render_activation_prompt(context))
        usage = run.usage()
        return ActivationResult(
            responses=proposed_responses(run.output),
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_tokens or 0,
            model_id=self.model_id,
        )
