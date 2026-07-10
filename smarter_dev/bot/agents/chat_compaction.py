"""Conversation-level history compaction for the chat agent.

Registered on the ``ChatAgent`` as a ``history_processor``. Pydantic AI
invokes this before every model call. When the *prior* conversation
(everything before the current model request) grows past
``COMPACT_TRIGGER_CHARS``, the oldest turns are rendered to a transcript
and summarised into a single running-summary message by a small Gemini
agent; the most recent ~``KEEP_RECENT_CHARS`` of turns stay verbatim.

The summary REQUIRES per-user attribution (username + user-id) so the
agent's multi-user discipline survives compaction — a summary that says
"someone asked about webhooks" is worse than no summary at all.

Self-stabilising design: the summary is injected as a normal user-turn
request prefixed ``[compacted history]``. When the conversation grows
past the trigger again, that summary is at the head of the "old" slice
and merges into the next running summary.

Cut points are always the start of a user turn (a ``ModelRequest``
containing a ``UserPromptPart``), so a tool call and its return are never
split across the summary boundary. The current turn (the final
``ModelRequest`` onward) is never touched — the agent must see its real
input.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from contextvars import ContextVar

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider

logger = logging.getLogger(__name__)

# Compact once the prior conversation exceeds this many characters
# (~10k tokens). Keeps steady-state input well below the unbounded growth
# we had before, without touching short conversations at all.
COMPACT_TRIGGER_CHARS = 40_000
# After compaction, keep roughly this many characters of the most recent
# turns verbatim. The gap between trigger and keep is hysteresis — it
# stops the compactor from re-firing on every single turn.
KEEP_RECENT_CHARS = 20_000
MAX_SUMMARY_CHARS = 2_500
# Individual tool returns/calls are clamped to this many chars in the
# transcript handed to the summariser — web fetches are already summaries,
# they don't deserve more of the summariser's attention than user talk.
TRANSCRIPT_TOOL_CLAMP = 1_500

COMPACTED_PREFIX = "[compacted history]"

DEFAULT_COMPACT_MODEL = "gemini-3.1-flash-lite"
COMPACT_MODEL_ENV_VAR = "CHAT_AGENT_COMPACT_MODEL"


@dataclasses.dataclass
class CompactionEvent:
    """One record of the compactor folding old turns into a summary.

    Collected per agent run via the ``_collected`` ContextVar so the engine
    can persist them alongside the turn for the operator dashboard.
    """

    event_kind: str  # conversation
    tool_name: str | None
    original_content: str
    summary: str
    original_chars: int
    summary_chars: int
    summarizer_tokens_input: int
    summarizer_tokens_output: int
    summarizer_model_name: str


_collected: ContextVar[list[CompactionEvent] | None] = ContextVar(
    "chat_compaction_events", default=None
)


def start_collection() -> list[CompactionEvent]:
    """Install a fresh per-run collector and return it.

    The engine calls this immediately before ``agent.run(...)``; the
    processor (running inside the same context) appends to it; the engine
    drains via ``drain_collection`` after the run completes.
    """
    bucket: list[CompactionEvent] = []
    _collected.set(bucket)
    return bucket


def drain_collection() -> list[CompactionEvent]:
    """Return the current collector and reset it to None."""
    bucket = _collected.get()
    _collected.set(None)
    return list(bucket) if bucket is not None else []


def _record_event(event: CompactionEvent) -> None:
    bucket = _collected.get()
    if bucket is not None:
        bucket.append(event)


_SUMMARIZER_PROMPT = """\
You compact the older portion of a Discord chat agent's conversation
history into a running summary the agent reads in place of those turns.
The transcript below interleaves user input (XML `<message>` blocks with
`user-id` and `username` attributes), the agent's replies, and tool
calls/returns. It may open with an earlier `[compacted history]` summary —
that is the running summary of even older turns; merge its still-relevant
facts into your output so nothing attributed is lost.

Non-negotiable rules:

1. ATTRIBUTION. Every question, claim, request, decision, or position you
   keep must name the user it came from as `username (id <user-id>)`.
   Never merge two users' statements into one, never say "someone" or
   "users discussed" when the transcript names them. If you cannot tell
   who said something, drop it rather than guess.
2. Structure the summary as:
   - `Participants:` one line listing each user as username (id ...).
   - Topic bullets: per topic, who said/asked what and how it resolved.
   - `Agent state:` what the agent itself said, promised, or produced
     (tool calls made and what they returned that still matters).
3. Prefer recent and unresolved things over old and settled ones when
   trimming to fit.

Hard limits: never exceed {max_chars} characters. No preamble, no
quoting, no apologies. Return just the summary text.
"""


_summarizer_agent: Agent[None, str] | None = None


def _build_summarizer_model() -> GoogleModel:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
    model_id = os.getenv(COMPACT_MODEL_ENV_VAR, DEFAULT_COMPACT_MODEL)
    return GoogleModel(model_id, provider=GoogleProvider(api_key=api_key))


def get_summarizer_agent() -> Agent[None, str]:
    """Return the singleton compaction summarizer agent."""
    global _summarizer_agent
    if _summarizer_agent is None:
        _summarizer_agent = Agent(
            _build_summarizer_model(),
            output_type=str,
            system_prompt=_SUMMARIZER_PROMPT.format(max_chars=MAX_SUMMARY_CHARS),
            model_settings=GoogleModelSettings(
                google_thinking_config={"thinking_level": "LOW"},
            ),
        )
    return _summarizer_agent


@dataclasses.dataclass
class _SummariseResult:
    text: str
    tokens_input: int
    tokens_output: int
    model_name: str


async def _summarise_conversation(transcript: str) -> _SummariseResult | None:
    """Run the summarizer over a transcript of old turns.

    Returns None on failure — the caller then leaves history untouched
    (a long prompt beats a lossy or missing one).
    """
    tokens_input = 0
    tokens_output = 0
    model_name = os.getenv(COMPACT_MODEL_ENV_VAR, DEFAULT_COMPACT_MODEL)
    try:
        result = await get_summarizer_agent().run(user_prompt=transcript)
    except Exception:
        logger.exception("Conversation summariser failed; skipping compaction")
        return None
    summary = (result.output or "").strip()
    if not summary:
        logger.warning("Conversation summariser returned empty output; skipping")
        return None
    try:
        usage = result.usage()
        if usage is not None:
            tokens_input = int(usage.input_tokens or 0)
            tokens_output = int(usage.output_tokens or 0)
    except Exception:
        pass
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS]
    return _SummariseResult(
        text=f"{COMPACTED_PREFIX} {summary}",
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        model_name=model_name,
    )


def _part_chars(part) -> int:
    if isinstance(part, (UserPromptPart, TextPart, ToolReturnPart)):
        content = part.content
        return len(content) if isinstance(content, str) else len(str(content))
    if isinstance(part, ToolCallPart):
        args = part.args
        return len(args) if isinstance(args, str) else len(str(args))
    return 0


def _message_chars(msg: ModelMessage) -> int:
    return sum(_part_chars(p) for p in msg.parts)


def _clamp(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + " …[truncated]"


def _render_transcript(messages: list[ModelMessage]) -> str:
    """Flatten old turns into labelled text for the summariser."""
    chunks: list[str] = []
    for msg in messages:
        for part in msg.parts:
            if isinstance(part, SystemPromptPart):
                continue
            if isinstance(part, UserPromptPart):
                content = part.content if isinstance(part.content, str) else str(part.content)
                chunks.append(f"[user_input]\n{content}")
            elif isinstance(part, TextPart):
                chunks.append(f"[assistant]\n{part.content}")
            elif isinstance(part, ToolCallPart):
                args = part.args if isinstance(part.args, str) else str(part.args)
                chunks.append(
                    f"[tool_call {part.tool_name}] "
                    f"{_clamp(args, TRANSCRIPT_TOOL_CLAMP)}"
                )
            elif isinstance(part, ToolReturnPart):
                content = part.content if isinstance(part.content, str) else str(part.content)
                chunks.append(
                    f"[tool_return {part.tool_name}]\n"
                    f"{_clamp(content, TRANSCRIPT_TOOL_CLAMP)}"
                )
    return "\n\n".join(chunks)


def _is_user_turn_start(msg: ModelMessage) -> bool:
    """A ModelRequest that begins a user turn (not a tool-return request)."""
    return isinstance(msg, ModelRequest) and any(
        isinstance(p, UserPromptPart) for p in msg.parts
    )


def _index_of_last_request(messages: list[ModelMessage]) -> int:
    for idx in range(len(messages) - 1, -1, -1):
        if isinstance(messages[idx], ModelRequest):
            return idx
    return len(messages)


def _pick_cut_index(prior: list[ModelMessage]) -> int | None:
    """Choose where old ends and kept-verbatim begins.

    Returns the smallest user-turn-start index whose suffix fits inside
    ``KEEP_RECENT_CHARS`` (keeping as much verbatim as the budget allows),
    or the latest user-turn start if even a single turn exceeds the
    budget. Returns None if there's no valid cut that leaves anything to
    summarise.
    """
    turn_starts = [i for i, m in enumerate(prior) if _is_user_turn_start(m)]
    if not turn_starts:
        return None

    suffix_chars = 0
    cut: int | None = None
    starts = set(turn_starts)
    for i in range(len(prior) - 1, -1, -1):
        suffix_chars += _message_chars(prior[i])
        if i in starts:
            if suffix_chars <= KEEP_RECENT_CHARS:
                cut = i
            else:
                break
    if cut is None:
        # Even the most recent prior turn alone busts the keep budget —
        # keep just that turn and summarise everything before it.
        cut = turn_starts[-1]
    if cut == 0:
        return None
    return cut


def _collect_system_parts(messages: list[ModelMessage]) -> list[SystemPromptPart]:
    parts: list[SystemPromptPart] = []
    for msg in messages:
        if isinstance(msg, ModelRequest):
            parts.extend(p for p in msg.parts if isinstance(p, SystemPromptPart))
    return parts


async def compact_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Fold old turns into a running summary once history gets long.

    The current turn (last ``ModelRequest`` onward) is always left intact.
    """
    if len(messages) <= 1:
        return messages

    current_start = _index_of_last_request(messages)
    prior = messages[:current_start]
    current = messages[current_start:]

    total_chars = sum(_message_chars(m) for m in prior)
    if total_chars <= COMPACT_TRIGGER_CHARS:
        return messages

    cut = _pick_cut_index(prior)
    if cut is None:
        return messages
    old = prior[:cut]
    kept = prior[cut:]

    transcript = _render_transcript(old)
    summary = await _summarise_conversation(transcript)
    if summary is None:
        return messages

    _record_event(
        CompactionEvent(
            event_kind="conversation",
            tool_name=None,
            original_content=transcript,
            summary=summary.text,
            original_chars=len(transcript),
            summary_chars=len(summary.text),
            summarizer_tokens_input=summary.tokens_input,
            summarizer_tokens_output=summary.tokens_output,
            summarizer_model_name=summary.model_name,
        )
    )

    # System prompt lives in the first ModelRequest of the history; it must
    # survive the fold or the agent loses its instructions entirely.
    summary_request = ModelRequest(
        parts=[
            *_collect_system_parts(old),
            UserPromptPart(content=summary.text),
        ]
    )
    return [summary_request, *kept, *current]
