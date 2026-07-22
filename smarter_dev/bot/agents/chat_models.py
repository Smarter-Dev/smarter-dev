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


class BlogTopicCandidate(BaseModel):
    """A claim from this conversation a post could be built on — strictly
    observational, no angle or thesis (see the system prompt's blog
    section for the full rules)."""

    headline: str = Field(
        description=(
            "One descriptive, non-editorial line naming the topic — no "
            "take, no clickbait."
        ),
    )
    observation: str = Field(
        description=(
            "What was actually said / asked / surfaced, 2-4 sentences, "
            "quoted or paraphrased faithfully — no interpretation, no "
            "side-taking."
        ),
    )
    scope: str = Field(
        description=(
            "Neutral 1-3 sentences: the territory a post would cover, "
            "not the take."
        ),
    )
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "Discord message ids/links or URLs grounding the observation; "
            "empty is fine."
        ),
    )
    category: Literal["concept", "misconception", "news"] | None = Field(
        default=None,
        description=(
            "concept | misconception | news; None if it doesn't cleanly fit."
        ),
    )


class MessageScore(BaseModel):
    """DIRECTEDNESS score for one new `<message>` — direction only, not
    whether the content deserves an answer (that's `response`)."""

    message_id: str = Field(
        description="Id of a `<message>` in this turn's input.",
    )
    score: int = Field(
        ge=1,
        le=10,
        description=(
            "1-10: how directly this message was aimed at YOU, judged from "
            "structural attributes only — see the system prompt's anchors "
            "(10 = @mention/reply-to-self … 1-2 = clearly not for you)."
        ),
    )
    reasoning: str = Field(
        description=(
            "ONE sentence citing the structural attribute(s) that set the "
            "score — not what you'd say back."
        ),
    )


class ResponseBody(BaseModel):
    """The message the agent sends this turn. At least one of ``message``
    or ``voice_summary`` must be non-empty."""

    target_message_id: str = Field(
        description=(
            "Id of the message this answers — MUST match a ranking with "
            "`score >= 5` (highest when several qualify)."
        ),
    )
    reply_directly: bool = Field(
        default=False,
        description=(
            "True → send as a visible Discord reply to `target_message_id` "
            "(use when the conversation drifted past it). Default False: "
            "plain channel message."
        ),
    )
    message: str | None = Field(
        default=None,
        description=(
            "Plain-text reply to post — ONLY the prose meant for the user. "
            "Never echo other schema fields, their values, or any JSON/"
            "key-value structure into this string; that leaks raw schema "
            "into chat. None if the user only wanted voice."
        ),
    )
    voice_summary: str | None = Field(
        default=None,
        description=(
            "Short spoken-style TTS summary, a few sentences max — never "
            "paragraphs or code. Default None. Set ONLY when (a) the user "
            "asked for voice in the message you're answering RIGHT NOW "
            "(doesn't carry forward), (b) voice is genuinely the better "
            "medium (pronunciation, intonation), or (c) a one-line zinger "
            "lands better spoken (then send ONLY voice, no message). If "
            "detail is needed AND voice was asked for, set both: full text "
            "in `message`, 1-3 sentence digest here."
        ),
    )
    voice_instruction: str | None = Field(
        default=None,
        description=(
            "TTS stage direction — tone / pace / emotion / persona (e.g. "
            "\"mock-serious deadpan\"). Only meaningful with "
            "voice_summary; None = default warm casual voice."
        ),
    )
    not_cs_topic_brief_answer: bool = Field(
        default=False,
        description=(
            "True when the question is OUTSIDE software/CS (banter, trivia, "
            "life advice; borderline community/career counts too) — caps "
            "`message` at 2 sentences. False for coding questions, which "
            "get the depth they warrant."
        ),
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
        description=(
            "One MessageScore per NEW `<message>` this turn. Direction "
            "only; the response decision flows from it."
        ),
    )
    response_language: str = Field(
        description=(
            "Lowercase English name of the predominant natural language "
            "used by the highest-scoring NEW message driving this turn, "
            "not the language of your reply. Set exactly `english` when "
            "that message is English, including English with incidental "
            "foreign text, code, or logs. A normal content answer is only "
            "allowed when this field is `english`. For any other value, "
            "do not answer the content or call tools. If the message "
            "scored >= 5, send only a short English redirect asking the "
            "user to use English, or set response=None only if visible "
            "history contains an earlier English-only redirect from you "
            "to that same user."
        ),
    )
    response: ResponseBody | None = Field(
        default=None,
        description=(
            "Populate to speak; None to stay silent. Must be None when "
            "every ranking scored < 5; when the top NEW message scored "
            ">= 5 it was directed at you — respond unless you're "
            "deliberately letting off-topic chatter pass (system prompt). "
            "For a non-English prompting message scored >= 5, populate "
            "this with the required English-only redirect unless visible "
            "history proves that same user already received one."
        ),
    )
    continue_watching: bool = Field(
        default=True,
        description=(
            "Stay engaged for follow-ups. Set False only when the "
            "engagement is genuinely over (user dismissed you, "
            "conversation ran its course, continuing would be "
            "intrusive)."
        ),
    )
    topic: str = Field(
        description="1-2 sentence summary of the current conversation topic.",
    )
    notes: str | None = Field(
        default=None,
        description=(
            "Per-person thread tracker: 'alice: <thread + status>; bob: "
            "…'. Accumulate, don't replace; drop only concluded threads. "
            "Durable memory of WHO is asking about WHAT — not a summary "
            "of what was said. None = keep existing notes unchanged."
        ),
    )
    blog_topic_candidates: list[BlogTopicCandidate] = Field(
        default_factory=list,
        description=(
            "Blog-post ideas filed this turn. See the system prompt for "
            "the rules; the schema doesn't bias the count."
        ),
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
