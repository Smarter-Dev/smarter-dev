"""Pass 1: the stateless DeepSeek V4 Flash watcher, plus the skim helper.

The watcher gets a fresh history every call: current wake criteria (seed
policy + the agent's addendum), a context tail, and the burst's new
messages. It decides whether to wake the K3 agent and selects the verbatim
snippets the wake brief carries. `SkimRunner` is the same model serving the
agent's skim tool: summarize a transcript with verbatim snippets and
message/user ids.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models import Model


class WatcherDecision(BaseModel):
    wake: bool
    reason: str = ""
    relevant_message_ids: list[str] = []
    summary: str = ""


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


def usage_dict(usage) -> dict:
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
        return result.output, usage_dict(result.usage())


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
        return result.output, usage_dict(result.usage())
