"""The generative gate the message gate ran before Jev, kept for the eval.

Until 2026-09-24 ``smarter_dev/bot/agents/message_gate.py`` asked GPT-5.4 Nano
(reasoning NONE) for a list of allowed candidate ids with this system prompt,
output type and prompt layout. The quality eval keeps them so any catalog model
can still be scored against Jev on the same cases.
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic import Field

from smarter_dev.bot.agents.message_gate import GateMessage
from smarter_dev.bot.agents.message_gate import _render_message

SYSTEM_PROMPT = """\
You are a fast, cheap relevance gate for a Discord bot. An admin has restricted \
this channel so the bot only replies to messages that match their written \
INSTRUCTIONS. For each CANDIDATE message you decide whether the instructions \
allow the bot to spend an (expensive) reply on it.

You are given:
- INSTRUCTIONS: the admin's filter describing which messages the bot should \
respond to. They are about the TOPIC and CONTENT of a message, never about who \
wrote it — ignore the author when deciding.
- CHANNEL (optional): the name of the channel the messages were sent in. For a \
forum post or thread this is its title — often the clearest statement of what \
the conversation is about. Treat it like CONTEXT when interpreting candidates.
- CONTEXT: recent channel messages, oldest first, provided only so you can \
interpret the candidates. These are NOT candidates; never return a context id.
- CANDIDATES: the messages to judge. Return the ids of the candidates the \
instructions allow.

Rules:
- Judge each candidate on whether its topic/content matches the instructions.
- The point of this gate is to protect an expensive model from clearly \
off-topic messages, so lean DROP when a candidate is plainly off-topic.
- When a candidate is genuinely ambiguous or borderline — it could reasonably \
fall under the instructions — lean ALLOW.
- Use the CONTEXT only to interpret a candidate (e.g. a short reply that only \
makes sense given the prior messages); a candidate that is on-topic given the \
conversation should be allowed even if it looks thin in isolation.
- Some candidates REDIRECT rather than state a topic of their own: follow-ups, \
nudges, and questions about the conversation itself ("do you have an answer?", \
"any update on this?", "what do you think?"). Never judge these on their bare \
words, and never classify them as meta-chatter. Instead, judge such a \
candidate exactly as if it restated the conversation's current topic — taken \
from the CHANNEL title and the CONTEXT. If that topic matches the \
instructions the candidate is allowed; if it does not, drop it. Example: with \
instructions "only cooking questions", in a thread asking how to keep risotto \
creamy, the candidate "do you have an answer?" inherits the topic "keeping \
risotto creamy" — a cooking question — and is allowed; the same candidate \
amid car-repair chatter is dropped. Only when neither the channel nor the context reveals any topic does \
the ambiguity rule above apply.

Set allowed_message_ids to the ids of the candidates (and only the candidates) \
that the instructions allow, in any order."""


class GateDecision(BaseModel):
    """The gate's verdict: which candidate message ids the filter allows."""

    allowed_message_ids: list[str] = Field(
        description="Ids of the CANDIDATE messages the admin instructions allow.",
    )


def render_prompt(
    response_filter: str,
    candidates: list[GateMessage],
    grounding: list[GateMessage],
    channel_name: str | None,
) -> str:
    sections = [f"INSTRUCTIONS:\n{response_filter.strip()}"]
    if channel_name and channel_name.strip():
        sections.append(f"CHANNEL:\n{channel_name.strip()}")
    if grounding:
        rendered = "\n".join(_render_message(message) for message in grounding)
        sections.append(
            "CONTEXT (oldest first, for reference only — never return these ids):\n"
            + rendered
        )
    rendered_candidates = "\n".join(_render_message(message) for message in candidates)
    sections.append("CANDIDATES (judge these):\n" + rendered_candidates)
    return "\n\n".join(sections)
