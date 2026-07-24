"""Pydantic models for the Discord chat agent.

Input and output shapes for the single chat agent that handles all
@mention and reply-driven conversations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator


class Author(BaseModel):
    """A Discord user referenced in the conversation."""

    user_id: str
    username: str
    nickname: str | None = None
    role_names: list[str] = Field(default_factory=list)


class MessageAttachment(BaseModel):
    """A file attached to a Discord message, surfaced so the agent can read it.

    The agent never receives the bytes inline — it sees the ``url`` (and a
    coarse ``kind``) and can call the ``web_read`` tool to read/summarize it.
    """

    url: str
    media_type: str | None = None  # e.g. "image/png", "audio/ogg"
    filename: str | None = None

    @property
    def kind(self) -> str:
        """Coarse category for rendering: image | audio | pdf | file."""
        mt = (self.media_type or "").lower()
        if mt.startswith("image/"):
            return "image"
        if mt.startswith("audio/"):
            return "audio"
        if mt == "application/pdf" or (self.filename or "").lower().endswith(".pdf"):
            return "pdf"
        return "file"


class Message(BaseModel):
    """A single Discord message in the conversation."""

    message_id: str
    author_id: str
    reply_to_message_id: str | None = None
    reply_to_author_id: str | None = Field(
        default=None,
        description=(
            "Discord user id of the author of the message being Discord-replied "
            "to, when this message is a Discord reply. None for plain channel "
            "posts. Lets the renderer emit reply-to-self / reply-to-user-id "
            "without forcing the model to cross-reference ids in history."
        ),
    )
    reply_to_is_self: bool = Field(
        default=False,
        description=(
            "True when this message is a Discord reply specifically to one of "
            "the bot's messages."
        ),
    )
    body: str
    reactions: list[str] = Field(default_factory=list)
    attachments: list[MessageAttachment] = Field(default_factory=list)
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
            "True if this message @mentions the bot. Discord-replies to the "
            "bot are tracked separately via reply_to_is_self."
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


class MessageScore(BaseModel):
    """DIRECTEDNESS score for one new `<message>` — direction only, not
    whether the content deserves an answer (that's `response`)."""

    message_id: str
    score: int = Field(
        ge=1,
        le=10,
        description="1-10, direction only: how directly this message was aimed at YOU (10 = mention/reply-to-you, 1-2 = not for you). Exception: non-English from a user you already redirected to English scores 1-2, even on a mention or reply-to-you.",
    )
    reasoning: str = Field(
        description="One sentence citing the structural attribute(s) behind the score.",
    )


class ResponseBody(BaseModel):
    """The message the agent sends this turn. At least one of ``message``
    or ``voice_summary`` must be non-empty."""

    target_message_id: str = Field(
        description="Id of the message being answered — must have scored >= 5.",
    )
    reply_directly: bool = Field(
        default=False,
        description="True to send as a visible Discord reply (when the conversation drifted past the target). Default False.",
    )
    message: str | None = Field(
        default=None,
        description="The reply text — prose only, never schema fields or JSON.",
    )
    voice_summary: str | None = Field(
        default=None,
        description="Short spoken TTS digest, few sentences max. Only when the user asked for voice this turn or it clearly lands better spoken. Default None.",
    )
    voice_instruction: str | None = Field(
        default=None,
        description="TTS tone/pace direction; only meaningful with voice_summary.",
    )
    not_cs_topic_brief_answer: bool = Field(
        default=False,
        description="True for non-CS topics (caps reply at 2 sentences); False for coding questions, which get real depth.",
    )

    @model_validator(mode="after")
    def _require_message_or_voice(self) -> "ResponseBody":
        if not (self.message and self.message.strip()) and not (
            self.voice_summary and self.voice_summary.strip()
        ):
            raise ValueError(
                "ResponseBody requires at least one of `message` or "
                "`voice_summary` to be a non-empty string."
            )
        return self


class TurnDecision(BaseModel):
    """One turn's decision: score each new message's direction, then decide
    what (if anything) to say — the funnel the system prompt describes.
    Silence is a choice you make by setting `response = None`, never a
    field you forget to fill."""

    rankings: list[MessageScore] = Field(
        description="One MessageScore per NEW <message> this turn.",
    )
    response_language: str = Field(
        description="Lowercase language name of the highest-scoring NEW message. Exactly `english` for English, including English with incidental foreign text, code, or logs.",
    )
    response: ResponseBody | None = Field(
        default=None,
        description="Populate to speak; None to stay silent. Must be None when every ranking scored < 5, and ALWAYS None for continued non-English from a user you already redirected to English — no second warning, no answer.",
    )
    continue_watching: bool = Field(
        default=True,
        description="Set False only when the engagement is genuinely over.",
    )
    topic: str = Field(
        description="1-2 sentence summary of the current conversation topic.",
    )
    notes: str | None = Field(
        default=None,
        description="Per-person thread tracker ('alice: …; bob: …'). Accumulate, don't replace; None = keep existing notes.",
    )

    @field_validator("response_language")
    @classmethod
    def _normalize_response_language(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("TurnDecision.response_language cannot be empty.")
        return normalized

    @model_validator(mode="after")
    def _validate_response_against_rankings(self) -> "TurnDecision":
        if not self.rankings:
            raise ValueError(
                "TurnDecision.rankings must contain at least one entry — "
                "every turn delivers at least one new <message> to score."
            )
        max_score = max(r.score for r in self.rankings)
        if self.response is None:
            return self
        if max_score < 5:
            raise ValueError(
                f"TurnDecision.response is populated but every ranking "
                f"scored below 5 (max={max_score}). Respond only when a "
                f"new message scored >= 5."
            )
        target_id = self.response.target_message_id
        match = next(
            (r for r in self.rankings if r.message_id == target_id),
            None,
        )
        if match is None:
            raise ValueError(
                f"TurnDecision.response.target_message_id={target_id!r} "
                f"does not match any MessageScore.message_id in this "
                f"turn's rankings."
            )
        if match.score < 5:
            raise ValueError(
                f"TurnDecision.response.target_message_id={target_id!r} "
                f"refers to a message scored {match.score} (<5). Pick a "
                f"target that scored >= 5, or set response=None."
            )
        if self.response_language != "english":
            message = (self.response.message or "").strip()
            if not message or "english" not in message.lower():
                raise ValueError(
                    "A non-English prompting message may only receive a "
                    "text redirect that explicitly asks for English."
                )
            if len(message) > 240:
                raise ValueError(
                    "A non-English prompting message's English redirect "
                    "must be no longer than 240 characters."
                )
            if self.response.voice_summary is not None:
                raise ValueError(
                    "A non-English prompting message's English redirect "
                    "must be text-only."
                )
        return self


AgentReturn = TurnDecision
