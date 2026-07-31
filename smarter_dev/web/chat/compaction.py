"""Provider-neutral durable web Chat compaction helpers.

The proven Discord cut/pairing kernel is reused directly, while model choice,
summary persistence, and pricing are supplied by the web runtime.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import RetryPromptPart
from pydantic_ai.messages import SystemPromptPart
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart

T = TypeVar("T")
KEEP_RECENT_CHARS = 20_000
TRANSCRIPT_TOOL_CLAMP = 1_500
CHARS_PER_TOKEN = 4
HARD_FOLD_TOKENS = 16_000
EXPECTED_FUTURE_CALLS = 5
MAX_SUMMARY_TOKENS = 625


def _safe_prompt_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        rendered = []
        for item in content:
            if isinstance(item, str):
                rendered.append(item)
            elif hasattr(item, "data"):
                rendered.append(
                    f"[binary attachment: {getattr(item, 'media_type', 'unknown')}, "
                    f"{len(getattr(item, 'data', b''))} bytes]"
                )
            else:
                rendered.append(str(item)[:TRANSCRIPT_TOOL_CLAMP])
        return "\n".join(rendered)
    return str(content)[:TRANSCRIPT_TOOL_CLAMP]


def _part_chars(part) -> int:
    if isinstance(part, UserPromptPart):
        return len(_safe_prompt_content(part.content))
    if isinstance(part, TextPart | ToolReturnPart):
        return (
            len(part.content)
            if isinstance(part.content, str)
            else len(str(part.content)[:TRANSCRIPT_TOOL_CLAMP])
        )
    if isinstance(part, ToolCallPart):
        return len(part.args) if isinstance(part.args, str) else len(str(part.args))
    return 0


def _render_transcript(messages: list[ModelMessage]) -> str:
    chunks: list[str] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, SystemPromptPart):
                continue
            if isinstance(part, UserPromptPart):
                chunks.append(f"[user_input]\n{_safe_prompt_content(part.content)}")
            elif isinstance(part, TextPart):
                chunks.append(f"[assistant]\n{part.content}")
            elif isinstance(part, ToolCallPart):
                chunks.append(
                    f"[tool_call {part.tool_name}] {str(part.args)[:TRANSCRIPT_TOOL_CLAMP]}"
                )
            elif isinstance(part, ToolReturnPart):
                chunks.append(
                    f"[tool_return {part.tool_name}]\n{str(part.content)[:TRANSCRIPT_TOOL_CLAMP]}"
                )
    return "\n\n".join(chunks)


def _is_user_turn_start(message: ModelMessage) -> bool:
    return isinstance(message, ModelRequest) and any(
        isinstance(part, UserPromptPart) for part in message.parts
    )


def _pick_cut_index(prior: list[ModelMessage]) -> int | None:
    starts = [
        index for index, message in enumerate(prior) if _is_user_turn_start(message)
    ]
    if not starts:
        return None
    suffix_chars = 0
    cut = None
    start_set = set(starts)
    for index in range(len(prior) - 1, -1, -1):
        suffix_chars += sum(_part_chars(part) for part in prior[index].parts)
        if index in start_set:
            if suffix_chars <= KEEP_RECENT_CHARS:
                cut = index
            else:
                break
    if cut is None:
        cut = starts[-1]
    return None if cut == 0 else cut


def _is_tool_result(part) -> bool:
    return isinstance(part, ToolReturnPart) or (
        isinstance(part, RetryPromptPart) and part.tool_name is not None
    )


def _split_cut_point(
    boundary: ModelRequest,
) -> tuple[ModelRequest | None, ModelRequest | None]:
    folded = [part for part in boundary.parts if _is_tool_result(part)]
    if not folded:
        return None, boundary
    kept = [part for part in boundary.parts if not _is_tool_result(part)]
    return ModelRequest(parts=folded), ModelRequest(
        parts=kept, instructions=boundary.instructions
    ) if kept else None


def _strip_orphan_leading_results(messages: list[ModelMessage]) -> list[ModelMessage]:
    head_end = len(messages)
    for index, message in enumerate(messages):
        if isinstance(message, ModelResponse) and any(
            isinstance(part, ToolCallPart) for part in message.parts
        ):
            head_end = index
            break
    head = messages[:head_end]
    if not any(_is_tool_result(part) for message in head for part in message.parts):
        return messages
    repaired = []
    for message in head:
        if not isinstance(message, ModelRequest):
            repaired.append(message)
            continue
        parts = [part for part in message.parts if not _is_tool_result(part)]
        if len(parts) == len(message.parts):
            repaired.append(message)
        elif parts:
            repaired.append(
                ModelRequest(parts=parts, instructions=message.instructions)
            )
    return [*repaired, *messages[head_end:]]


@dataclass(frozen=True, slots=True)
class DurableCompactionResult:
    messages: list
    summary: str | None
    folded_messages: list
    fingerprint: str
    changed: bool


def version_fingerprint(messages: list[dict]) -> str:
    """Invalidate summaries when an alternate assistant version is selected."""
    normalized = [
        {
            "id": m.get("id"),
            "version": m.get("version_number"),
            "active": m.get("is_active", True),
        }
        for m in messages
    ]
    return hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()


def should_compact_history(
    messages: list[ModelMessage],
    *,
    chat_input_rate,
    chat_cached_input_rate,
    compact_input_rate,
    compact_output_rate,
    cache_warm: bool,
) -> bool:
    """Mirror Discord's cache/price-aware fold decision for web Chat."""
    cut = _pick_cut_index(messages)
    if cut is None:
        return False
    foldable_tokens = max(
        sum(_part_chars(part) for message in messages[:cut] for part in message.parts)
        // CHARS_PER_TOKEN,
        0,
    )
    kept_tokens = max(
        sum(_part_chars(part) for message in messages[cut:] for part in message.parts)
        // CHARS_PER_TOKEN,
        0,
    )
    saved = foldable_tokens - MAX_SUMMARY_TOKENS
    if saved <= 0:
        return False
    if foldable_tokens >= HARD_FOLD_TOKENS:
        return True
    p_in = chat_input_rate
    p_cached = chat_cached_input_rate or chat_input_rate
    summarizer_cost = (
        foldable_tokens * compact_input_rate + MAX_SUMMARY_TOKENS * compact_output_rate
    )
    future_savings = EXPECTED_FUTURE_CALLS * p_cached * saved
    if cache_warm:
        return future_savings >= (p_in - p_cached) * kept_tokens + summarizer_cost
    return p_in * saved + future_savings >= summarizer_cost


async def compact_model_history(
    messages: list[ModelMessage],
    *,
    summarize: Callable[[str], Awaitable[str]],
) -> DurableCompactionResult:
    """Compact structured prior-turn history without breaking tool pairs."""
    original = list(messages)
    fingerprint = hashlib.sha256(
        ModelMessagesTypeAdapter.dump_json(original)
    ).hexdigest()
    try:
        cut = _pick_cut_index(original)
        if cut is None or cut <= 0:
            return DurableCompactionResult(original, None, [], fingerprint, False)
        folded = list(original[:cut])
        kept: list[ModelMessage] = list(original[cut:])
        if kept and isinstance(kept[0], ModelRequest):
            folded_boundary, kept_boundary = _split_cut_point(kept[0])
            if folded_boundary is not None:
                folded.append(folded_boundary)
            kept = ([kept_boundary] if kept_boundary is not None else []) + kept[1:]
        kept = _strip_orphan_leading_results(kept)
        summary = (await summarize(_render_transcript(folded))).strip()
        if not summary:
            return DurableCompactionResult(original, None, [], fingerprint, False)
        marker = ModelRequest(
            parts=[UserPromptPart(content=f"[compacted history]\n{summary}")]
        )
        compacted: list[ModelMessage] = [marker, *kept]
        # Require provider-message serialization to round-trip before replacing
        # canonical history.
        payload = ModelMessagesTypeAdapter.dump_json(compacted)
        compacted = list(ModelMessagesTypeAdapter.validate_json(payload))
        return DurableCompactionResult(compacted, summary, folded, fingerprint, True)
    except Exception:
        return DurableCompactionResult(original, None, [], fingerprint, False)


async def compact_safely(
    messages: list[T],
    *,
    summarize: Callable[[str], Awaitable[str]],
    render: Callable[[list[T]], str],
    pick_cut: Callable[[list[T]], int | None],
    current_turn_start: int,
) -> DurableCompactionResult:
    """Fold only prior turns and leave history unchanged on every failure.

    ``current_turn_start`` is an explicit durable sequence boundary. Tool
    call/result pair repair belongs in ``pick_cut`` for the concrete message
    representation (the Pydantic kernel exports above do exactly that).
    """
    original = list(messages)
    prior = original[:current_turn_start]
    current = original[current_turn_start:]
    fingerprint = hashlib.sha256(repr(original).encode()).hexdigest()
    try:
        cut = pick_cut(prior)
        if cut is None or cut <= 0:
            return DurableCompactionResult(original, None, [], fingerprint, False)
        folded, kept = prior[:cut], prior[cut:]
        summary = (await summarize(render(folded))).strip()
        if not summary:
            return DurableCompactionResult(original, None, [], fingerprint, False)
        # Runtime converts this durable marker into a provider-specific user
        # message; it is plain data here so stateless workers can replay it.
        compacted = [{"role": "user", "content": f"[compacted history]\n{summary}"}]
        return DurableCompactionResult(
            compacted + kept + current, summary, folded, fingerprint, True
        )
    except Exception:
        return DurableCompactionResult(original, None, [], fingerprint, False)


__all__ = [
    "DurableCompactionResult",
    "compact_model_history",
    "should_compact_history",
    "compact_safely",
    "version_fingerprint",
    "_pick_cut_index",
    "_render_transcript",
    "_split_cut_point",
    "_strip_orphan_leading_results",
]
