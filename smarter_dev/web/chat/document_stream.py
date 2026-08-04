"""Fork the agent's own history so a document body streams as its next turn.

The document tool does not take the file body as an argument. It branches: the
model's tool call is answered — on a branch of its own — with "the file exists,
now write it", and the response to that is the file. The branch is thrown away
afterwards and the main line keeps only a short receipt, so a hundred-thousand
character document never has to live in the conversation's context.

Two things fall out of that shape. The reader watches the file appear as it is
written, because the branch is a stream. And the body is plain provider text
rather than a JSON tool argument, which is what a document is anyway.
"""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from uuid import UUID

from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models import ModelRequestParameters

from smarter_dev.shared.model_router import model_settings_for
from smarter_dev.web.chat.runtime import run_cancellable

# The fork's whole prompt. Terse on purpose: the model already knows what it
# asked for and why, because everything it has thought so far is on this branch.
FORK_INSTRUCTION = (
    "File created: {filename}\n\n"
    "Output the complete contents of this file as your next message, and "
    "nothing else. No preamble, no closing remarks, no surrounding code fence. "
    "Markdown only, starting at the first line of the file. Your message is "
    "written straight into the file as you produce it, and the reader is "
    "watching it appear."
)

# A sibling tool call in the same model response still has to be answered for
# the branch request to be well formed, but its real result belongs to the main
# line, not here.
WITHHELD_SIBLING_RESULT = (
    "Withheld on this branch. Write the file from what you already know."
)

# Documents are long, and thinking tokens draw on the same ceiling. High enough
# for a full-length file, low enough that the spend reservation this implies
# stays proportionate.
DOCUMENT_OUTPUT_TOKEN_CAP = 32_000

# Flush policy for the live preview. Bigger batches mean fewer writes and fewer
# notifications; smaller ones mean the text appears more smoothly.
FLUSH_CHARS = 512
FLUSH_SECONDS = 0.25


@dataclass(slots=True)
class DocumentStreamResult:
    markdown: str
    truncated: bool


def build_fork_messages(
    *,
    messages: list[ModelMessage],
    tool_call_id: str,
    filename: str,
) -> list[ModelMessage]:
    """Branch the run's history at the document tool call.

    ``messages`` is the live history of the run, which by the time a tool body
    executes already ends in the model response carrying the tool call. The
    branch answers that call with the write instruction — and answers any
    sibling call in the same response with a placeholder, because a provider
    rejects a request that leaves one of its own tool calls unanswered.
    """
    branch = list(messages)
    last_response = next(
        (
            message
            for message in reversed(branch)
            if isinstance(message, ModelResponse)
        ),
        None,
    )
    calls: list[ToolCallPart] = (
        [part for part in last_response.parts if isinstance(part, ToolCallPart)]
        if last_response is not None
        else []
    )
    returns: list[ToolReturnPart] = []
    for call in calls:
        if call.tool_call_id == tool_call_id:
            continue
        returns.append(
            ToolReturnPart(
                tool_name=call.tool_name,
                content=WITHHELD_SIBLING_RESULT,
                tool_call_id=call.tool_call_id,
            )
        )
    instruction = FORK_INSTRUCTION.format(filename=filename)
    returns.append(
        ToolReturnPart(
            tool_name="write_document",
            content=instruction,
            tool_call_id=tool_call_id,
        )
    )
    branch.append(ModelRequest(parts=returns, instructions=instruction))
    return branch


def _text_delta(event) -> str:
    from pydantic_ai import PartDeltaEvent
    from pydantic_ai import PartStartEvent
    from pydantic_ai.messages import TextPart
    from pydantic_ai.messages import TextPartDelta

    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    return ""


async def stream_document_body(
    *,
    metered,
    model,
    reasoning,
    messages: list[ModelMessage],
    max_chars: int,
    on_flush: Callable[[str, str], Awaitable[None]],
    turn_id: UUID,
    worker_lease_token: str | None,
) -> DocumentStreamResult:
    """Run the branch and stream its text out through ``on_flush``.

    ``on_flush`` receives ``(chunk, whole_body_so_far)``. It is called from the
    read loop, so it must be cheap: one bounded database write and one
    notification, not a re-render of the document.

    No agent and no tools — this is one provider request whose entire output is
    a file. Reasoning parts, if the model produces any, are ignored rather than
    written into the document.
    """
    settings = dict(model_settings_for(model, reasoning) or {})
    settings["max_tokens"] = min(model.max_output_tokens, DOCUMENT_OUTPUT_TOKEN_CAP)
    # There is no agent between us and the provider here, so do what one would:
    # merge the model's own settings, resolve the thinking knob, customize the
    # parameters, and normalize any native-tool parts left in the branch history.
    settings, parameters = metered.prepare_request(settings, ModelRequestParameters())
    messages = metered.prepare_messages(messages)

    async def consume() -> DocumentStreamResult:
        body = ""
        pending = ""
        truncated = False
        last_flush = monotonic()

        async def flush() -> None:
            nonlocal pending, last_flush
            if not pending:
                return
            chunk, pending = pending, ""
            last_flush = monotonic()
            await on_flush(chunk, body)

        async with metered.request_stream(messages, settings, parameters, None) as stream:
            async for event in stream:
                delta = _text_delta(event)
                if not delta:
                    continue
                room = max_chars - len(body)
                if len(delta) > room:
                    delta = delta[:room]
                    truncated = True
                body += delta
                pending += delta
                if truncated:
                    break
                if len(pending) >= FLUSH_CHARS or (
                    monotonic() - last_flush >= FLUSH_SECONDS
                ):
                    await flush()
            # A truncated body leaves the rest of the provider stream unread;
            # exiting the context manager closes it and settles what was billed.
            await flush()
        return DocumentStreamResult(markdown=body, truncated=truncated)

    return await run_cancellable(
        consume(),
        turn_id=turn_id,
        worker_lease_token=worker_lease_token,
    )
