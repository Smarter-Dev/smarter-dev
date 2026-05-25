"""Length-gated history compaction for the chat agent.

Registered on the ``ChatAgent`` as a ``history_processor``. Pydantic AI
invokes this before every model call. The processor walks every message
part in *prior turns* (anything before the just-added current user
request); any part whose textual content exceeds
``COMPACT_THRESHOLD_CHARS`` is replaced with a short summary produced by
a small Gemini agent.

Self-stabilising design: summaries are short (capped under the threshold),
so on subsequent turns they're already-small and the walker skips them.
Combined with the engine persisting ``result.all_messages()`` after every
turn, compactions survive across turns without a separate cache.

The current turn (the final ``ModelRequest``) is never touched — the
agent must see its real input.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from contextvars import ContextVar
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
from pydantic_ai.providers.google import GoogleProvider

logger = logging.getLogger(__name__)

COMPACT_THRESHOLD_CHARS = 10_000
MAX_SUMMARY_CHARS = 2_000

DEFAULT_COMPACT_MODEL = "gemini-3.1-flash-lite"
COMPACT_MODEL_ENV_VAR = "CHAT_AGENT_COMPACT_MODEL"


@dataclasses.dataclass
class CompactionEvent:
    """One record of the compactor replacing a long part with a summary.

    Collected per agent run via the ``_collected`` ContextVar so the engine
    can persist them alongside the turn for the operator dashboard.
    """

    event_kind: str  # user_prompt / assistant_text / tool_call_args / tool_return
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
You are a conversation-history compactor. You receive a single chunk of
text pulled from a prior turn of a Discord chat agent's conversation
history. Produce a faithful, compact summary that preserves WHAT MATTERS
for the downstream agent to keep reasoning correctly:

- For chunks marked as USER_PROMPT (the chat agent's input JSON for a
  prior turn): 4-6 sentences covering the topics discussed, which users
  engaged with each topic, salient points or questions, and any web
  results or links cited.
- For chunks marked as ASSISTANT_TEXT (the agent's prior reply): 2-3
  sentences capturing the substance of what the agent said.
- For chunks marked as TOOL_CALL (the agent's invocation of a tool):
  one line: tool name + a normalised description of the arguments.
- For chunks marked as TOOL_RETURN (the result a tool produced): 2-3
  sentences capturing what was returned that mattered — search hits,
  page contents, errors.

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


async def _summarise(label: str, text: str) -> _SummariseResult:
    """Run the summarizer and clamp the output to ``MAX_SUMMARY_CHARS``."""
    prompt = f"[{label}]\n\n{text}"
    tokens_input = 0
    tokens_output = 0
    model_name = os.getenv(COMPACT_MODEL_ENV_VAR, DEFAULT_COMPACT_MODEL)
    try:
        result = await get_summarizer_agent().run(user_prompt=prompt)
        summary = (result.output or "").strip()
        try:
            usage = result.usage()
            if usage is not None:
                tokens_input = int(usage.input_tokens or 0)
                tokens_output = int(usage.output_tokens or 0)
        except Exception:
            pass
    except Exception:
        logger.exception("Summariser failed; falling back to a truncation")
        summary = text[:MAX_SUMMARY_CHARS]
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS]
    if not summary:
        summary = text[:MAX_SUMMARY_CHARS]
    return _SummariseResult(
        text=f"[compacted] {summary}",
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        model_name=model_name,
    )


def _content_text(value: Any) -> str:
    """Return a stringified view of part.content for length checking."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


async def _compact_request_parts(parts: list) -> list:
    new_parts = []
    for part in parts:
        if isinstance(part, UserPromptPart):
            text = _content_text(part.content)
            if len(text) > COMPACT_THRESHOLD_CHARS:
                summary = await _summarise("USER_PROMPT", text)
                _record_event(
                    CompactionEvent(
                        event_kind="user_prompt",
                        tool_name=None,
                        original_content=text,
                        summary=summary.text,
                        original_chars=len(text),
                        summary_chars=len(summary.text),
                        summarizer_tokens_input=summary.tokens_input,
                        summarizer_tokens_output=summary.tokens_output,
                        summarizer_model_name=summary.model_name,
                    )
                )
                new_parts.append(dataclasses.replace(part, content=summary.text))
                continue
        elif isinstance(part, ToolReturnPart):
            text = _content_text(part.content)
            if len(text) > COMPACT_THRESHOLD_CHARS:
                summary = await _summarise("TOOL_RETURN", text)
                _record_event(
                    CompactionEvent(
                        event_kind="tool_return",
                        tool_name=getattr(part, "tool_name", None),
                        original_content=text,
                        summary=summary.text,
                        original_chars=len(text),
                        summary_chars=len(summary.text),
                        summarizer_tokens_input=summary.tokens_input,
                        summarizer_tokens_output=summary.tokens_output,
                        summarizer_model_name=summary.model_name,
                    )
                )
                new_parts.append(dataclasses.replace(part, content=summary.text))
                continue
        elif isinstance(part, SystemPromptPart):
            # Never compact system prompts — they're owned by the framework.
            pass
        new_parts.append(part)
    return new_parts


async def _compact_response_parts(parts: list) -> list:
    new_parts = []
    for part in parts:
        if isinstance(part, TextPart):
            text = _content_text(part.content)
            if len(text) > COMPACT_THRESHOLD_CHARS:
                summary = await _summarise("ASSISTANT_TEXT", text)
                _record_event(
                    CompactionEvent(
                        event_kind="assistant_text",
                        tool_name=None,
                        original_content=text,
                        summary=summary.text,
                        original_chars=len(text),
                        summary_chars=len(summary.text),
                        summarizer_tokens_input=summary.tokens_input,
                        summarizer_tokens_output=summary.tokens_output,
                        summarizer_model_name=summary.model_name,
                    )
                )
                new_parts.append(dataclasses.replace(part, content=summary.text))
                continue
        elif isinstance(part, ToolCallPart):
            args_text = _content_text(part.args)
            if len(args_text) > COMPACT_THRESHOLD_CHARS:
                summary = await _summarise(
                    f"TOOL_CALL {part.tool_name}", args_text
                )
                _record_event(
                    CompactionEvent(
                        event_kind="tool_call_args",
                        tool_name=part.tool_name,
                        original_content=args_text,
                        summary=summary.text,
                        original_chars=len(args_text),
                        summary_chars=len(summary.text),
                        summarizer_tokens_input=summary.tokens_input,
                        summarizer_tokens_output=summary.tokens_output,
                        summarizer_model_name=summary.model_name,
                    )
                )
                new_parts.append(dataclasses.replace(part, args=summary.text))
                continue
        new_parts.append(part)
    return new_parts


def _index_of_last_request(messages: list[ModelMessage]) -> int:
    for idx in range(len(messages) - 1, -1, -1):
        if isinstance(messages[idx], ModelRequest):
            return idx
    return len(messages)


async def compact_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """Length-gated compaction over prior turns; current turn left intact."""
    if len(messages) <= 1:
        return messages

    current_start = _index_of_last_request(messages)
    prior = messages[:current_start]
    current = messages[current_start:]

    compacted: list[ModelMessage] = []
    for msg in prior:
        if isinstance(msg, ModelRequest):
            new_parts = await _compact_request_parts(list(msg.parts))
            compacted.append(dataclasses.replace(msg, parts=new_parts))
        elif isinstance(msg, ModelResponse):
            new_parts = await _compact_response_parts(list(msg.parts))
            compacted.append(dataclasses.replace(msg, parts=new_parts))
        else:
            compacted.append(msg)

    return compacted + current
