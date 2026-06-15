"""Pydantic models for the Discord chat agent.

Input and output shapes for the single chat agent that handles all
@mention and reply-driven conversations.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

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
    """A claim shaken loose by this conversation that a post could be built on.

    Strictly observational. You are NOT picking the angle, deciding on a
    thesis, or pitching a take — you're filing what was said / asked /
    observed so a downstream agent can form a falsifiable hypothesis
    from it. Skip charity entries; the bar is "would this still be a
    real question worth investigating in three months?".
    """

    headline: str = Field(
        description=(
            "One line — descriptive, not editorial. Names the topic; "
            "doesn't pitch a take. No marketing fluff, no clickbait."
        ),
    )
    observation: str = Field(
        description=(
            "What was actually observed in chat. For a user question: "
            "the question itself plus what they're tripping on. For a "
            "misconception: the wrong belief, spelled out. For news: the "
            "specific event / change someone surfaced. Two to four "
            "sentences. Quote or paraphrase faithfully — do NOT add "
            "interpretation or argue a side."
        ),
    )
    scope: str = Field(
        description=(
            "Neutral surface-area description. One to three sentences: "
            "what a post on this would cover. NOT 'the take' — just "
            "the territory."
        ),
    )
    evidence: list[str] = Field(
        default_factory=list,
        description=(
            "References that ground the observation. Discord-message "
            "links / message ids when the source is the conversation, "
            "or URLs when someone shared a link. Empty list is OK if "
            "the observation stands on its own ('Bob asked X')."
        ),
    )
    category: Literal["concept", "misconception", "news"] | None = Field(
        default=None,
        description=(
            "'concept' — a coding concept the conversation pointed at. "
            "'misconception' — a wrong mental model someone is holding. "
            "'news' — a current event / release / change. "
            "Leave None if it doesn't cleanly fit."
        ),
    )


class MessageScore(BaseModel):
    """Per-message engagement score for a single new `<message>` this turn.

    Forces the agent to explicitly classify each new message before
    deciding whether (and what) to respond. Without this step the model
    tends to chime in on conversations it wasn't invited to.
    """

    message_id: str = Field(
        description="Discord message id this score refers to. Must match a `<message>` in this turn's input.",
    )
    score: int = Field(
        ge=1,
        le=10,
        description=(
            "1-10 score for how strongly this message was intended for "
            "YOU to respond to. Anchor points: "
            "10 = explicit direct address (@mention, reply-to-self with a "
            "clear question); "
            "7-9 = clearly your turn (continuation of an exchange you were "
            "having, or @mention without a direct question); "
            "5-6 = arguably for you, you could go either way; "
            "3-4 = related to your context but not actually addressed to "
            "you (a user replying to another user about a topic you helped "
            "with); "
            "1-2 = clearly NOT for you (unrelated banter, two users "
            "talking to each other, bystander observation)."
        ),
    )
    reasoning: str = Field(
        description=(
            "ONE sentence justifying the score in plain language. Cite the "
            "attribute(s) you used (e.g. 'has mentions-bot=true', "
            "'reply-to-user-id=2, not me', 'continues exchange with the "
            "self=true message above')."
        ),
    )


class ResponseBody(BaseModel):
    """The actual message the agent wants to send this turn.

    Populated on a `TurnDecision` only when the agent decides to respond
    (at least one `MessageScore.score >= 5`). At least one of
    ``message`` or ``voice_summary`` must be non-empty.
    """

    target_message_id: str = Field(
        description=(
            "The message id this response is answering. MUST be the id of "
            "a `MessageScore` entry in this turn's rankings with "
            "`score >= 5`. Pick the highest-scoring one when several "
            "qualify."
        ),
    )
    reply_directly: bool = Field(
        default=False,
        description=(
            "True → send as a Discord reply that points at "
            "`target_message_id` (a visible reply pointer in the UI). "
            "Use this when the conversation has drifted and a plain "
            "channel message would lose the connection (e.g. several "
            "messages have arrived since the one you're answering). "
            "Default False: send as a regular channel message — clean "
            "and natural when you're answering the freshest message and "
            "your reply will land directly under it anyway."
        ),
    )
    message: str | None = Field(
        default=None,
        description=(
            "Plain-text message body to post to Discord. Leave None if "
            "the user only wanted a voice reply."
        ),
    )
    voice_summary: str | None = Field(
        default=None,
        description=(
            "Short, spoken-style SUMMARY to send as a Discord voice "
            "message via TTS. Default to None. Only set when ONE of "
            "these is true: (a) the user explicitly asked for voice in "
            "the message you're responding to RIGHT NOW (a previous "
            "voice exchange does NOT carry forward), (b) voice is "
            "genuinely the best medium for this specific answer (e.g. "
            "pronouncing a word, demonstrating intonation), or (c) the "
            "bit lands better spoken — a one-line zinger / punchline "
            "where the surprise IS the audio; in that case send ONLY "
            "voice_summary, no message. Otherwise leave None and use "
            "`message`. Voice should be a few sentences max, never "
            "paragraphs / code / long-form. If the reply needs detail "
            "AND voice was requested, set BOTH: `message` for the full "
            "text, `voice_summary` for a 1-3 sentence spoken digest."
        ),
    )
    voice_instruction: str | None = Field(
        default=None,
        description=(
            "Stage direction passed to the TTS model to shape HOW the "
            "voice sounds — tone, pace, energy, emotion, accent, "
            "persona. Only meaningful alongside voice_summary. Examples: "
            "\"mock-serious deadpan delivery\", \"excitedly, like "
            "sharing good news\", \"slow and considered with a pause "
            "before the punch line\", \"overly-caffeinated tech-bro "
            "persona, slightly frantic\". Leave None for the default "
            "warm, casual, peer-developer voice."
        ),
    )
    not_cs_topic_brief_answer: bool = Field(
        default=False,
        description=(
            "Set True when the user is asking about something OUTSIDE "
            "software / computer science (life advice, sports, trivia, "
            "recipe help, banter, hellos, opinions on movies, random "
            "shower thoughts, etc.). When True, your `message` MUST be "
            "**at most 2 sentences** — friendly and in-and-out, no "
            "paragraph essays. Leave False for coding / debugging / "
            "tooling / architecture / AI / dev-ops / language / library "
            "questions, where you answer with the depth the question "
            "warrants. Borderline (community process questions, "
            "career-shaped questions): default True and stay short."
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
    """The agent's full decision for one turn.

    Always carries `rankings` — one MessageScore per new `<message>` this
    turn. `response` is populated ONLY if at least one ranking scored
    >= 5; otherwise this is effectively a no-response turn.
    """

    rankings: list[MessageScore] = Field(
        description=(
            "One MessageScore per NEW `<message>` in this turn's input "
            "(activation_message on first turn, every new_message on "
            "follow-up turns). Rank each one before deciding whether to "
            "respond. The act of scoring is mandatory — the schema "
            "rejects responses without it."
        ),
    )
    response: ResponseBody | None = Field(
        default=None,
        description=(
            "The message you want to send, OR None to stay silent. "
            "REQUIRED to be None when every ranking scored < 5. "
            "REQUIRED to be populated when you want to send anything. "
            "When populated, `target_message_id` must match a ranking "
            "with `score >= 5`."
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
            "Per-person thread tracker. Format: 'alice: <thread + "
            "status>; bob: <thread + status>; carol: <thread + status>'. "
            "Accumulate as new threads emerge — don't replace. Drop a "
            "thread only when it has clearly concluded. This is durable "
            "memory across engagements; write it for your future self to "
            "remember WHO is asking about WHAT, not to summarise what "
            "was said (your conversation history covers that). Leave "
            "None to preserve the existing notes unchanged when nothing "
            "needs updating."
        ),
    )
    blog_topic_candidates: list[BlogTopicCandidate] = Field(
        default_factory=list,
        description=(
            "Blog-post ideas filed this turn. See the system prompt for "
            "the rules; the schema doesn't bias the count."
        ),
    )

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
        return self


AgentReturn = TurnDecision
