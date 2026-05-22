"""Pydantic models for the Discord chat agent.

Input and output shapes for the single chat agent that handles all
@mention and reply-driven conversations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator


class Author(BaseModel):
    """A Discord user referenced in the conversation."""

    user_id: str
    username: str
    nickname: str | None = None
    role_names: list[str] = Field(default_factory=list)


class Message(BaseModel):
    """A single Discord message in the conversation."""

    message_id: str
    author_id: str
    reply_to_message_id: str | None = None
    body: str
    reactions: list[str] = Field(default_factory=list)
    has_attachments: bool = False
    sent_at: datetime | None = Field(
        default=None,
        description=(
            "When the message was sent in UTC. Optional because some "
            "synthetic / test paths may construct a Message without one."
        ),
    )
    mentions_bot: bool = Field(
        default=False,
        description=(
            "True if this message either @mentions the bot or is a Discord "
            "reply to one of the bot's messages — i.e. a direct attempt to "
            "engage with you."
        ),
    )


class ChannelInfo(BaseModel):
    """Channel metadata passed to the agent."""

    channel_id: str
    name: str
    description: str | None = None


class Me(BaseModel):
    """The bot's own identity, so the agent can recognise its own messages."""

    user_id: str
    username: str


class InitialAgentInput(BaseModel):
    """Input for the first turn of an engagement.

    Carries the activation trigger separately from the pre-engagement
    context so the agent can clearly distinguish "the message that woke me
    up" from "what was happening before I was called in".
    """

    kind: Literal["initial"] = "initial"
    me: Me
    channel_history: list[Message] = Field(
        description=(
            "The most recent channel messages BEFORE the activation message, "
            "oldest first. Use this to read the room — see who's talking, "
            "what they're discussing, what tone fits."
        ),
    )
    activation_message: Message = Field(
        description=(
            "The @mention or reply that triggered this engagement. This is "
            "the message you're being asked to respond to."
        ),
    )
    authors: list[Author]
    channel: ChannelInfo
    now_utc: datetime
    topic: str | None = None
    notes: str | None = None


class FollowupAgentInput(BaseModel):
    """Input for a follow-up turn inside an active engagement.

    Carries ONLY the messages that arrived since the agent's last turn. The
    rest of the conversation (channel history from the initial activation,
    the agent's own prior replies, tool calls/returns) lives in the agent's
    Pydantic AI message history.

    ``topic`` and ``notes`` are passed on every turn — they're the most
    recently durable view of memory and may have been updated since the
    last turn (or since the engagement started).
    """

    kind: Literal["followup"] = "followup"
    me: Me
    new_messages: list[Message]
    authors: list[Author]
    channel: ChannelInfo
    now_utc: datetime
    topic: str | None = None
    notes: str | None = None


class BlogTopicCandidate(BaseModel):
    """A blog-post idea shaken loose by this conversation.

    The bar: would I want to publish this in three months? Skip charity
    entries — if the topic isn't actually post-worthy, don't file it just
    to please whoever asked. Filing one in a substantive engagement is
    healthy; two is fine when the conversation produced two distinct
    things worth writing.
    """

    headline: str = Field(
        description=(
            "One line — the title you'd actually publish. No marketing "
            "fluff ('the ultimate guide to…'), no clickbait. Written for "
            "a human reviewer skimming a queue."
        ),
    )
    pitch: str = Field(
        description=(
            "Two to four sentences sketching the angle: what the post "
            "would cover and what makes it worth reading. Include the "
            "concrete moment from chat that sparked it — 'X said Y; "
            "here's what's actually going on' lands harder than 'explain "
            "Z'. When the idea came from a user request, attribute them "
            "here ('@alice asked for this in #python')."
        ),
    )
    category: Literal["concept", "misconception", "news"] | None = Field(
        default=None,
        description=(
            "'concept' — a coding concept worth explaining ('Why CRDTs "
            "aren't magic', 'What `await` actually does'). "
            "'misconception' — a wrong mental model worth correcting; the "
            "instructive part is showing *why* the wrong model breaks "
            "('await makes things parallel', 'more indexes is always "
            "better'). "
            "'news' — recent coding news the chat had a real take on "
            "('Why the latest Postgres release matters for pgbouncer "
            "users'). Not rehashed headlines — a take. "
            "Leave None if it doesn't cleanly fit."
        ),
    )


class NoResponse(BaseModel):
    """Agent decided not to send a message this turn."""

    kind: Literal["no_response"] = "no_response"
    continue_watching: bool = Field(
        default=True,
        description=(
            "Stay engaged so future messages will trigger you. Set False only "
            "if the engagement is genuinely over (user dismissed you, "
            "conversation ran its course)."
        ),
    )
    topic: str = Field(
        description="1-2 sentence summary of the current conversation topic.",
    )
    blog_topic_candidates: list[BlogTopicCandidate] = Field(
        default_factory=list,
        description=(
            "Blog-post ideas filed this turn. See the system prompt for the "
            "rules; the schema doesn't bias the count."
        ),
    )


class SendResponse(BaseModel):
    """Agent decided to send something this turn.

    Text and voice are independent channels — set ``message`` for a Discord text
    message, ``voice_summary`` for a voice message, or both for both. At least
    one must be non-empty.
    """

    kind: Literal["send_response"] = "send_response"
    reply_to_message_id: str | None = Field(
        default=None,
        description=(
            "Only set when answering a message that is NOT the most recent one "
            "or two in your input. Setting it on the latest message just renders "
            "a redundant reply pointer back to the message right above yours, "
            "which looks noisy. Use it to disambiguate when you're answering an "
            "older question that's drifted up in the channel; leave None for "
            "replies to the freshest message."
        ),
    )
    message: str | None = Field(
        default=None,
        description=(
            "Plain-text message body to post to Discord. Leave None if the user "
            "only wanted a voice reply."
        ),
    )
    voice_summary: str | None = Field(
        default=None,
        description=(
            "Short, spoken-style SUMMARY to send as a Discord voice message via "
            "TTS. Default to None. Only set when ONE of these is true: (a) the "
            "user explicitly asked for voice in the message you're responding to "
            "RIGHT NOW (a previous voice exchange does NOT carry forward), (b) "
            "voice is genuinely the best medium for this specific answer (e.g. "
            "pronouncing a word, demonstrating intonation), or (c) the bit lands "
            "better spoken — a one-line zinger / punchline where the surprise "
            "IS the audio; in that case send ONLY voice_summary, no message. "
            "Otherwise leave None and use `message`. Voice should be a few "
            "sentences max, never paragraphs / code / long-form. If the reply "
            "needs detail AND voice was requested, set BOTH: `message` for the "
            "full text, `voice_summary` for a 1-3 sentence spoken digest."
        ),
    )
    voice_instruction: str | None = Field(
        default=None,
        description=(
            "Stage direction passed to the TTS model to shape HOW the voice "
            "sounds — tone, pace, energy, emotion, accent, persona. Only "
            "meaningful alongside voice_summary. Examples: \"mock-serious "
            "deadpan delivery\", \"excitedly, like sharing good news\", \"slow "
            "and considered with a pause before the punch line\", \"overly-"
            "caffeinated tech-bro persona, slightly frantic\". Leave None for "
            "the default warm, casual, peer-developer voice."
        ),
    )
    continue_watching: bool = Field(
        default=True,
        description=(
            "Stay engaged for follow-ups. Set False only when the conversation "
            "is genuinely done, the user dismissed you, or continuing would be "
            "intrusive."
        ),
    )
    topic: str = Field(
        description="1-2 sentence summary of the current conversation topic.",
    )
    notes: str = Field(
        description=(
            "Per-person thread tracker. Format: 'alice: <thread + status>; "
            "bob: <thread + status>; carol: <thread + status>'. Accumulate as "
            "new threads emerge — don't replace. Drop a thread only when it has "
            "clearly concluded. This is durable memory across engagements; "
            "write it for your future self to remember WHO is asking about WHAT, "
            "not to summarise what was said (your conversation history covers "
            "that)."
        ),
    )
    blog_topic_candidates: list[BlogTopicCandidate] = Field(
        default_factory=list,
        description=(
            "Blog-post ideas filed this turn. See the system prompt for the "
            "rules; the schema doesn't bias the count."
        ),
    )

    @model_validator(mode="after")
    def _require_message_or_voice(self) -> "SendResponse":
        if not (self.message and self.message.strip()) and not (
            self.voice_summary and self.voice_summary.strip()
        ):
            raise ValueError(
                "SendResponse requires at least one of `message` or `voice_summary` "
                "to be a non-empty string."
            )
        return self


AgentReturn = Annotated[
    Union[NoResponse, SendResponse],
    Field(discriminator="kind"),
]
