"""Runtime-enforced tool counters and mode-specific web tool signatures."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field

import httpx
import pydantic_monty as monty

from smarter_dev.web.chat.policy import IntelligencePolicy
from smarter_dev.web.chat.safety import validate_public_url
from smarter_dev.web.research_tools import brave_search
from smarter_dev.web.research_tools import jina_read

RAW_READ_CHAR_LIMIT = 100_000
MAX_CODE_OUTPUT_CHARS = 10_000
MONTY_LIMITS = {
    "max_memory": 256 * 1024 * 1024,
    "max_recursion_depth": 500,
    "max_duration_secs": 10.0,
}


@dataclass(slots=True)
class ExecutionCounters:
    """Counters for one agent execution/root turn.

    Callers persist these values after each accepted operation. Increment occurs
    before validation/body execution so failed and retried attempts count.
    """

    tool_calls: int = 0
    searches: int = 0
    subagent_attempts: int = 0
    seen_call_ids: set[str] = field(default_factory=set)

    def accept_tool(self, policy: IntelligencePolicy, call_id: str) -> bool:
        if call_id in self.seen_call_ids:
            return True  # worker redelivery is idempotent
        if (
            policy.max_tool_calls is not None
            and self.tool_calls >= policy.max_tool_calls
        ):
            return False
        self.seen_call_ids.add(call_id)
        self.tool_calls += 1
        return True

    def accept_search(self, policy: IntelligencePolicy, call_id: str) -> bool:
        if call_id in self.seen_call_ids:
            return True
        if policy.max_searches is not None and self.searches >= policy.max_searches:
            return False
        if (
            policy.max_tool_calls is not None
            and self.tool_calls >= policy.max_tool_calls
        ):
            return False
        self.seen_call_ids.add(call_id)
        self.tool_calls += 1
        self.searches += 1
        return True

    def accept_subagent(self, policy: IntelligencePolicy, call_id: str) -> bool:
        if call_id in self.seen_call_ids:
            return True
        # Every accepted creation attempt counts before dispatch/validation.
        if (
            policy.max_subagents is not None
            and self.subagent_attempts >= policy.max_subagents
        ):
            return False
        if (
            policy.max_tool_calls is not None
            and self.tool_calls >= policy.max_tool_calls
        ):
            return False
        self.seen_call_ids.add(call_id)
        self.tool_calls += 1
        self.subagent_attempts += 1
        return True


async def run_code(reason: str, code: str) -> str:
    """Execute Python without network/filesystem access in the Monty sandbox."""
    if not (reason or "").strip() or not (code or "").strip():
        return "error: reason and code are required"
    collector = monty.CollectStreams()
    try:
        compiled = monty.Monty(code)
        value = await compiled.run_async(
            limits=MONTY_LIMITS, print_callback=collector
        )
    except monty.MontyError as exc:
        stdout = "".join(text for stream, text in collector.output if stream == "stdout")
        suffix = f"\nstdout before error:\n{stdout}" if stdout else ""
        return f"error: {type(exc).__name__}: {exc}{suffix}"[:MAX_CODE_OUTPUT_CHARS]
    except Exception as exc:
        return f"error: {type(exc).__name__}: {exc}"[:MAX_CODE_OUTPUT_CHARS]
    stdout = "".join(text for stream, text in collector.output if stream == "stdout")
    result = f"stdout:\n{stdout}\n" if stdout else ""
    result += f"return value: {value!r}"
    return result[:MAX_CODE_OUTPUT_CHARS]


async def web_search(
    query: str, *, policy: IntelligencePolicy, client: httpx.AsyncClient | None = None
) -> list[dict]:
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        # Ultra has no product result cap; request the provider maximum and do
        # not slice it. Other modes retain their exact structural cap.
        requested = policy.max_search_results
        provider_count = requested if requested is not None else 20
        results = await brave_search(client, query, num_results=provider_count)
        return results if requested is None else results[:requested]
    finally:
        if own_client:
            await client.aclose()


async def _read_raw(url: str, *, client: httpx.AsyncClient | None = None) -> str:
    await validate_public_url(url)
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0, follow_redirects=False)
    try:
        result = await jina_read(client, url)
        if result.get("error"):
            return f"error: {result['error']}"
        return str(result.get("content") or "")[:RAW_READ_CHAR_LIMIT]
    finally:
        if own_client:
            await client.aclose()


async def web_read_required(
    url: str,
    summarization_instruction: str,
    *,
    summarize: Callable[[str, str], Awaitable[str]] | None = None,
) -> str:
    """Lower-mode signature: instruction is structurally required."""
    instruction = (summarization_instruction or "").strip()
    if not instruction:
        return "error: summarization_instruction is required in this intelligence mode"
    raw = await _read_raw(url)
    if raw.startswith("error:"):
        return raw
    return await summarize(raw, instruction) if summarize else raw


async def web_read_optional(
    url: str,
    summarization_instruction: str | None = None,
    *,
    summarize: Callable[[str, str], Awaitable[str]] | None = None,
) -> str:
    """Higher-mode signature: raw Jina content is permitted and bounded."""
    raw = await _read_raw(url)
    if raw.startswith("error:") or not summarization_instruction:
        return raw
    return await summarize(raw, summarization_instruction)
