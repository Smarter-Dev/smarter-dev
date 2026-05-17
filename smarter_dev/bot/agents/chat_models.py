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


class AgentInput(BaseModel):
    """The full input passed to the chat agent on every turn."""

    me: Me
    messages: list[Message]
    authors: list[Author]
    channel: ChannelInfo
    now_utc: datetime
    topic: str | None = None
    notes: str | None = None


class NoResponse(BaseModel):
    """Agent decided not to send a message this turn."""

    kind: Literal["no_response"] = "no_response"
    continue_watching: bool = True
    topic: str = Field(
        description="1-2 sentence summary of the current conversation topic.",
    )


class SendResponse(BaseModel):
    """Agent decided to send something this turn.

    Text and voice are independent channels — set ``message`` for a Discord text
    message, ``voice_summary`` for a voice message, or both for both. At least
    one must be non-empty.
    """

    kind: Literal["send_response"] = "send_response"
    reply_to_message_id: str | None = None
    message: str | None = Field(
        default=None,
        description=(
            "Plain-text message body to send to Discord. Leave None if the user "
            "only wanted a voice reply."
        ),
    )
    voice_summary: str | None = Field(
        default=None,
        description=(
            "Short, spoken-style SUMMARY of the response to send as a Discord voice "
            "message via TTS. Only include when the user asked for voice. Voice "
            "messages should almost always be a *summary* — a few sentences max — "
            "not the full reply. Set ``message`` to the full text alongside this if "
            "the user would still benefit from reading the long form."
        ),
    )
    continue_watching: bool = True
    topic: str = Field(
        description="1-2 sentence summary of the current conversation topic.",
    )
    notes: str = Field(
        description=(
            "1-5 sentences of working notes carried into the next turn. Track ONLY "
            "the conversation threads you were activated for (the ones where users "
            "have directly engaged you via @mention or reply). Capture the salient "
            "points users have made, open questions, and where each thread is "
            "heading. Ignore unrelated channel chatter unless a new message in your "
            "input explicitly references one of your tracked topics."
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
