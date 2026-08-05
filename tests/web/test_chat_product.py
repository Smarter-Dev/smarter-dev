"""Deterministic product-invariant tests for Smarter Dev web Chat."""

from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from litestar import Litestar
from pydantic_ai.messages import BinaryContent
from pydantic_ai.messages import ModelRequest
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import TextPart
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart

from smarter_dev.shared.model_catalog import get_model
from smarter_dev.web.chat.api import ChatApiController
from smarter_dev.web.chat.attachments import AttachmentError
from smarter_dev.web.chat.attachments import extract_text
from smarter_dev.web.chat.attachments import require_attachment_count
from smarter_dev.web.chat.attachments import validate_attachment
from smarter_dev.web.chat.compaction import _split_cut_point
from smarter_dev.web.chat.compaction import compact_safely
from smarter_dev.web.chat.compaction import should_compact_history
from smarter_dev.web.chat.conversations import MAX_TITLE_CHARS
from smarter_dev.web.chat.conversations import ConversationTitleError
from smarter_dev.web.chat.conversations import derive_title
from smarter_dev.web.chat.conversations import normalize_title
from smarter_dev.web.chat.document_stream import WITHHELD_SIBLING_RESULT
from smarter_dev.web.chat.document_stream import build_fork_messages
from smarter_dev.web.chat.document_stream import stream_document_body
from smarter_dev.web.chat.documents import DOCUMENT_STATUSES
from smarter_dev.web.chat.documents import MAX_DOCUMENT_MARKDOWN_CHARS
from smarter_dev.web.chat.documents import READABLE_STATUSES
from smarter_dev.web.chat.documents import REPLACING_STATUSES
from smarter_dev.web.chat.documents import MarkdownDocumentError
from smarter_dev.web.chat.documents import apply_document_patches
from smarter_dev.web.chat.documents import attachment_kind
from smarter_dev.web.chat.documents import clean_document_body
from smarter_dev.web.chat.documents import validate_document_request
from smarter_dev.web.chat.entitlements import has_chat
from smarter_dev.web.chat.entitlements import has_ultra_chat
from smarter_dev.web.chat.entitlements import resolve_spend_tier
from smarter_dev.web.chat.jobs import _document_receipt
from smarter_dev.web.chat.jobs import _upload_manifest
from smarter_dev.web.chat.policy import compaction_model_key
from smarter_dev.web.chat.policy import policy_for
from smarter_dev.web.chat.runtime import BINARY_HISTORY_PLACEHOLDER
from smarter_dev.web.chat.runtime import decode_model_messages
from smarter_dev.web.chat.runtime import encode_model_messages
from smarter_dev.web.chat.runtime import strip_binary_content
from smarter_dev.web.chat.spend import append_wind_down
from smarter_dev.web.chat.spend import daily_bounds
from smarter_dev.web.chat.spend import evaluate_spend
from smarter_dev.web.chat.spend import four_hour_bounds
from smarter_dev.web.chat.spend import total_tokens
from smarter_dev.web.chat.spend import weekly_bounds
from smarter_dev.web.chat.subagents import effective_system_prompt
from smarter_dev.web.chat.subagents import run_subagent
from smarter_dev.web.chat.subagents import tool_names_for_child
from smarter_dev.web.chat.toolsets import ExecutionCounters
from smarter_dev.web.chat.toolsets import run_code
from smarter_dev.web.chat.toolsets import web_read_optional
from smarter_dev.web.chat.toolsets import web_read_required
from smarter_dev.web.chat_settings_admin import filter_unpriced_catalog_selections
from smarter_dev.web.llm_pricing import model_change_warning
from smarter_dev.web.llm_pricing import price_rates_for_model


def test_priced_catalog_choices_remain_enabled_during_admin_save():
    filtered, skipped = filter_unpriced_catalog_selections(
        {
            "gpt-5-6-luna": (True, "low"),
            "gpt-5-6-sol": (True, "medium"),
            "claude-opus-5": (True, "high"),
        }
    )
    assert filtered == {
        "gpt-5-6-luna": (True, "low"),
        "gpt-5-6-sol": (True, "medium"),
        "claude-opus-5": (True, "high"),
    }
    assert skipped == []


def test_chat_api_routes_register_with_litestar():
    app = Litestar(route_handlers=[ChatApiController])
    assert app.routes


@pytest.mark.parametrize(
    ("roles", "tier"),
    [
        ({"sudo-hacker"}, "hacker"),
        ({"sudo-r"}, "r"),
        ({"sudo-rw"}, "rw"),
        ({"sudo-rwx"}, "rwx"),
        ({"sudo-founder"}, "rw"),
        ({"sudo-founder", "sudo-rwx"}, "rwx"),
        ({"sudo-hacker", "sudo-r"}, "r"),
        (set(), None),
    ],
)
def test_spend_tier_precedence(roles, tier):
    assert resolve_spend_tier(roles) == tier


def test_entitlements_do_not_grant_admin_implicitly():
    assert not has_chat({"administrator"})
    assert not has_ultra_chat({"administrator"})
    assert has_chat({"sudo-hacker"})
    assert not has_ultra_chat({"sudo-hacker"})
    assert has_ultra_chat({"sudo-rw"})
    assert has_ultra_chat({"sudo-founder"})


@pytest.mark.parametrize(
    ("mode", "tools", "searches", "results", "subagents", "required"),
    [
        ("maximize_efficiency", 5, 2, 3, 0, True),
        ("efficient", 10, 2, 3, 0, True),
        ("intelligence", 10, 5, 5, 3, False),
        ("maximize_intelligence", None, None, 10, 10, False),
        ("ultra_intelligence", None, None, None, None, False),
    ],
)
def test_exact_policy_matrix(mode, tools, searches, results, subagents, required):
    policy = policy_for(mode)
    assert (policy.max_tool_calls, policy.max_searches, policy.max_search_results) == (
        tools,
        searches,
        results,
    )
    assert policy.max_subagents == subagents
    assert policy.web_summary_required is required


def test_compaction_model_selection():
    assert (
        compaction_model_key(
            "intelligence", selected_model_key="chat", configured_model_key="compact"
        )
        == "compact"
    )
    assert (
        compaction_model_key(
            "maximize_intelligence",
            selected_model_key="chat",
            configured_model_key="compact",
        )
        == "chat"
    )
    assert (
        compaction_model_key(
            "ultra_intelligence",
            selected_model_key="chat",
            configured_model_key="compact",
        )
        == "chat"
    )


def test_mode_specific_web_read_signatures_are_structural():
    required = inspect.signature(web_read_required).parameters[
        "summarization_instruction"
    ]
    optional = inspect.signature(web_read_optional).parameters[
        "summarization_instruction"
    ]
    assert required.default is inspect.Parameter.empty
    assert optional.default is None


def test_tool_and_search_caps_count_failures_before_body():
    policy = policy_for("maximize_efficiency")
    counters = ExecutionCounters()
    assert all(counters.accept_tool(policy, str(i)) for i in range(5))
    assert not counters.accept_tool(policy, "overflow")
    assert counters.tool_calls == 5
    # Redelivery of an accepted call remains idempotent.
    assert counters.accept_tool(policy, "0")
    assert counters.tool_calls == 5


def test_search_cap_is_independent_and_also_counts_tools():
    policy = policy_for("efficient")
    counters = ExecutionCounters()
    assert counters.accept_search(policy, "a")
    assert counters.accept_search(policy, "b")
    assert not counters.accept_search(policy, "c")
    assert counters.searches == 2 and counters.tool_calls == 2


def test_subagent_attempt_cap_counts_accepted_attempts():
    policy = policy_for("intelligence")
    counters = ExecutionCounters()
    assert [counters.accept_subagent(policy, str(i)) for i in range(4)] == [
        True,
        True,
        True,
        False,
    ]
    assert counters.subagent_attempts == 3


def test_title_guidance_only_reaches_a_root_agent_that_still_needs_a_name():
    unnamed = effective_system_prompt(child=False, needs_title=True)
    named = effective_system_prompt(child=False, needs_title=False)
    assert "set_chat_title" in unnamed
    assert "set_chat_title" not in named
    assert "set_chat_title" not in effective_system_prompt(child=True, needs_title=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Durable Queue Design  ", "Durable Queue Design"),
        ("Two\nlines\tapart", "Two lines apart"),
        ("collapse    the    gaps", "collapse the gaps"),
    ],
)
def test_titles_are_folded_to_one_clean_line(raw, expected):
    assert normalize_title(raw) == expected


def test_unusable_titles_are_refused_and_long_ones_are_clipped():
    for empty in ("", "   ", "\n\t", None):
        with pytest.raises(ConversationTitleError):
            normalize_title(empty)
    clipped = normalize_title("z" * (MAX_TITLE_CHARS + 40))
    assert len(clipped) == MAX_TITLE_CHARS + 1 and clipped.endswith("…")


def test_derived_title_is_the_opening_of_the_question():
    assert derive_title("Explain durable queues") == "Explain durable queues"
    long = derive_title("word " * 60)
    assert long.endswith("…") and len(long) == 80


def test_children_cannot_recurse_and_prompt_omits_guidance():
    assert tool_names_for_child(["web_search", "run_subagent", "web_read"]) == [
        "web_search",
        "web_read",
    ]
    assert "run_subagent" in effective_system_prompt(child=False)
    assert "run_subagent" not in effective_system_prompt(child=True)
    for tool in ("write_document", "read_document", "list_documents"):
        assert tool in effective_system_prompt(child=False)
        assert tool not in effective_system_prompt(child=True)
    assert list(inspect.signature(run_subagent).parameters) == [
        "name",
        "task",
        "reasoning_level",
    ]
    assert (
        inspect.signature(run_subagent).parameters["reasoning_level"].default
        == "inherit"
    )


def test_fixed_four_hour_window_exact_boundary():
    start = datetime(2026, 7, 1, 12, 34, 56, 789, tzinfo=UTC)
    assert four_hour_bounds(start) == (start, start + timedelta(hours=4))
    assert (
        start + timedelta(hours=3, minutes=59, seconds=59, microseconds=999999)
        < four_hour_bounds(start)[1]
    )
    assert start + timedelta(hours=4) == four_hour_bounds(start)[1]


def test_daily_and_sunday_weekly_boundaries():
    saturday = datetime(2026, 8, 1, 23, 59, 59, tzinfo=UTC)
    sunday = datetime(2026, 8, 2, 0, 0, 0, tzinfo=UTC)
    assert daily_bounds(saturday)[1] == sunday
    assert weekly_bounds(saturday)[1] == sunday
    assert weekly_bounds(sunday)[0] == sunday
    assert weekly_bounds(sunday)[1] == sunday + timedelta(days=7)


def test_overage_is_shared_not_stacked_and_equality_enters_overage():
    decision = evaluate_spend(
        four_hour_spend=Decimal("10"),
        daily_spend=Decimal("21"),
        weekly_spend=Decimal("50"),
        four_hour_limit=Decimal("10"),
        daily_limit=Decimal("20"),
        weekly_limit=Decimal("50"),
    )
    assert decision.allowed and decision.in_overage
    assert decision.overage_used == Decimal("1")
    assert decision.overage_allowance == Decimal("1.50")


def test_normal_and_ultra_hard_cutoffs():
    base = {
        "four_hour_spend": Decimal("11.5"),
        "daily_spend": Decimal("11.5"),
        "weekly_spend": Decimal("11.5"),
        "four_hour_limit": Decimal("10"),
        "daily_limit": Decimal("10"),
        "weekly_limit": Decimal("10"),
    }
    normal = evaluate_spend(**base)
    ultra = evaluate_spend(**base, ultra=True)
    assert normal.hard_cutoff and not normal.allowed
    assert ultra.allowed and ultra.in_overage
    assert ultra.overage_allowance == Decimal("2.50")


def test_zero_limit_fails_closed_and_warning_appends():
    decision = evaluate_spend(
        four_hour_spend=Decimal("0"),
        daily_spend=Decimal("0"),
        weekly_spend=Decimal("0"),
        four_hour_limit=Decimal("0"),
        daily_limit=Decimal("0"),
        weekly_limit=Decimal("0"),
    )
    assert decision.hard_cutoff
    assert "Wind down" in append_wind_down("result", in_overage=True)
    assert append_wind_down("result", in_overage=False) == "result"


def test_cache_tokens_are_not_double_counted():
    assert total_tokens(100, 20, cache_read_tokens=80, cache_write_tokens=10) == 120


@pytest.mark.asyncio
async def test_chat_code_tool_is_sandboxed_and_bounded():
    result = await run_code("verify arithmetic", "print(2 + 2)\n2 + 2")
    assert "4" in result
    denied = await run_code(
        "attempt filesystem access", "open('/tmp/not-allowed', 'w')"
    )
    assert "error:" in denied.lower()


def test_attachment_text_and_json_validation():
    item = validate_attachment("config.json", "application/json", b'{"ok": true}')
    assert item.kind == "text" and len(item.sha256) == 64
    assert extract_text(b'{"ok": true}', "application/json") == '{"ok": true}'


@pytest.mark.parametrize(
    ("name", "mime", "data"),
    [
        ("payload.exe", "application/octet-stream", b"MZ..."),
        ("archive.zip", "application/zip", b"PK"),
        ("fake.png", "image/png", b"not png"),
        ("binary.txt", "text/plain", b"a\x00b"),
        ("../secret.txt", "text/plain", b"hello"),
    ],
)
def test_attachment_rejections(name, mime, data):
    with pytest.raises((AttachmentError, ValueError)):
        validate_attachment(name, mime, data)


def test_attachment_count_and_size_boundaries():
    require_attachment_count([1, 2, 3, 4, 5])
    with pytest.raises(AttachmentError):
        require_attachment_count([1, 2, 3, 4, 5, 6])
    with pytest.raises(AttachmentError):
        validate_attachment("x.txt", "text/plain", b"x" * (10 * 1024 * 1024 + 1))


def test_document_request_validation_normalizes_filename():
    requested = validate_document_request(
        title=" Queue design ", filename="durable-queues"
    )
    assert requested.title == "Queue design"
    assert requested.filename == "durable-queues.md"


@pytest.mark.parametrize(
    ("title", "filename"),
    [
        ("", "guide.md"),
        ("Guide", "../guide.md"),
        ("Guide", "guide\n.md"),
        ("Guide", ""),
        ("Guide", "g" * 300),
    ],
)
def test_document_request_validation_rejects_unsafe_or_unbounded_values(
    title, filename
):
    with pytest.raises(MarkdownDocumentError):
        validate_document_request(title=title, filename=filename)


@pytest.mark.parametrize(
    ("streamed", "expected"),
    [
        ("```markdown\n# Plan\n\nBody\n```", "# Plan\n\nBody"),
        ("```\n# Plan\n```", "# Plan"),
        ("  # Plan\n\nBody  ", "# Plan\n\nBody"),
        # A fenced code block that is the whole file stays fenced.
        ("```python\nx = 1\n```", "```python\nx = 1\n```"),
    ],
)
def test_streamed_document_body_is_unwrapped_but_code_is_left_alone(
    streamed, expected
):
    assert clean_document_body(streamed) == expected


def test_document_fork_answers_every_pending_tool_call():
    """A branch leaving a sibling tool call unanswered is a malformed request.

    The fork is a real provider request built from the live run history, so every
    tool call in the response it branches from has to be accounted for: the
    document's own call carries the write instruction, and any sibling gets a
    placeholder.
    """
    history = [
        ModelRequest(parts=[UserPromptPart(content="write the plan")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name="write_document",
                    args={"reason": "r", "filename": "plan.md", "title": "Plan"},
                    tool_call_id="call-doc",
                ),
                ToolCallPart(
                    tool_name="web_search",
                    args={"query": "queues"},
                    tool_call_id="call-search",
                ),
            ]
        ),
    ]
    fork = build_fork_messages(
        messages=history, tool_call_id="call-doc", filename="plan.md"
    )
    assert fork[:2] == history
    returns = {part.tool_call_id: part for part in fork[-1].parts}
    assert set(returns) == {"call-doc", "call-search"}
    assert returns["call-search"].content == WITHHELD_SIBLING_RESULT
    assert "plan.md" in returns["call-doc"].content
    # The instruction also rides as request instructions, which providers append
    # to the system prompt already in the history rather than replacing it.
    assert fork[-1].instructions == returns["call-doc"].content
    # The branch is a copy: the live history the agent is still running on must
    # not gain a turn from being forked.
    assert len(history) == 2


@pytest.mark.asyncio
async def test_compaction_never_touches_current_turn_and_failure_is_safe():
    messages = ["old-1", "old-2", "recent", "current"]
    result = await compact_safely(
        messages,
        summarize=lambda _: _raise(),
        render=lambda rows: "\n".join(rows),
        pick_cut=lambda _: 2,
        current_turn_start=3,
    )
    assert result.messages == messages and not result.changed


async def _raise():
    raise RuntimeError("scripted compactor failure")


def test_tool_result_boundary_stays_with_folded_side():
    boundary = ModelRequest(
        parts=[
            ToolReturnPart(tool_name="web_read", content="done", tool_call_id="1"),
            UserPromptPart(content="next turn"),
        ]
    )
    folded, kept = _split_cut_point(boundary)
    assert isinstance(folded.parts[0], ToolReturnPart)
    assert isinstance(kept.parts[0], UserPromptPart)


def test_web_compaction_uses_cache_and_price_economics():
    messages = []
    for index in range(4):
        messages.extend(
            [
                ModelRequest(
                    parts=[UserPromptPart(content=f"question {index} " * 500)]
                ),
                ModelRequest(
                    parts=[UserPromptPart(content=f"followup {index} " * 500)]
                ),
            ]
        )
    # A cold expensive chat prompt can repay a cheap compactor immediately.
    assert should_compact_history(
        messages,
        chat_input_rate=Decimal("10"),
        chat_cached_input_rate=Decimal("1"),
        compact_input_rate=Decimal("0.1"),
        compact_output_rate=Decimal("0.2"),
        cache_warm=False,
    )
    # With a warm, almost-free cache and expensive summarizer, defer the fold.
    assert not should_compact_history(
        messages,
        chat_input_rate=Decimal("1"),
        chat_cached_input_rate=Decimal("0.01"),
        compact_input_rate=Decimal("100"),
        compact_output_rate=Decimal("100"),
        cache_warm=True,
    )


def test_model_warning_uses_actual_rates_and_no_universal_discount_claim():
    old = get_model("gemini-3-1-flash-lite")
    new = get_model("gpt-5-4-mini")
    assert price_rates_for_model(old) is not None
    warning = model_change_warning(old, new)
    assert (
        "uncached input" in warning
        and "cached input" in warning
        and "output" in warning
    )
    assert "1/10" not in warning and "90%" not in warning


class _FakeStream:
    """The provider side of one fork request, as a stream of text deltas."""

    def __init__(self, deltas: list[str]):
        self.deltas = deltas

    async def __aiter__(self):
        from pydantic_ai import PartDeltaEvent
        from pydantic_ai.messages import TextPartDelta

        for index, delta in enumerate(self.deltas):
            yield PartDeltaEvent(index=index, delta=TextPartDelta(content_delta=delta))


class _FakeMeteredModel:
    def __init__(self, deltas: list[str]):
        self.deltas = deltas
        self.settings = None
        self.messages = None

    def prepare_request(self, settings, parameters):
        return settings, parameters

    def prepare_messages(self, messages):
        return messages

    @asynccontextmanager
    async def request_stream(self, messages, settings, parameters, run_context=None):
        self.messages = messages
        self.settings = settings
        yield _FakeStream(self.deltas)


@pytest.mark.asyncio
async def test_document_stream_flushes_as_it_writes_and_returns_the_whole_body():
    """The reader sees the file appear, and the caller gets the finished text.

    Flushes are what the preview is built from, so each one carries the new chunk
    and the body as it stands — the client appends the chunk and never has to
    reassemble the file from scratch.
    """
    model = get_model("gemini-3-1-flash-lite")
    metered = _FakeMeteredModel(["a" * 600, "b" * 600, "tail"])
    flushes: list[tuple[str, str]] = []

    async def on_flush(chunk: str, body: str) -> None:
        flushes.append((chunk, body))

    result = await stream_document_body(
        metered=metered,
        model=model,
        reasoning=None,
        messages=[ModelRequest(parts=[UserPromptPart(content="write it")])],
        max_chars=10_000,
        on_flush=on_flush,
        turn_id=uuid4(),
        worker_lease_token="lease",
    )

    assert result.markdown == "a" * 600 + "b" * 600 + "tail"
    assert result.truncated is False
    # Chunks concatenate to the body, in order, with nothing repeated.
    assert "".join(chunk for chunk, _ in flushes) == result.markdown
    assert len(flushes) >= 2
    assert flushes[-1][1] == result.markdown
    # A document draws on the same output ceiling as any response, so the cap has
    # to be asked for explicitly rather than inherited.
    assert 0 < metered.settings["max_tokens"] <= model.max_output_tokens


@pytest.mark.asyncio
async def test_document_stream_stops_at_the_ceiling_and_says_it_was_truncated():
    """Silently storing a file that stops mid-sentence is the bad failure."""
    model = get_model("gemini-3-1-flash-lite")
    metered = _FakeMeteredModel(["x" * 40, "y" * 40])
    flushed: list[str] = []

    async def on_flush(chunk: str, _body: str) -> None:
        flushed.append(chunk)

    result = await stream_document_body(
        metered=metered,
        model=model,
        reasoning=None,
        messages=[ModelRequest(parts=[UserPromptPart(content="write it")])],
        max_chars=50,
        on_flush=on_flush,
        turn_id=uuid4(),
        worker_lease_token="lease",
    )

    assert len(result.markdown) == 50
    assert result.truncated is True
    assert "".join(flushed) == result.markdown


def test_document_receipt_omits_the_body_and_points_at_the_reread():
    """The receipt is the whole reason the fork saves context.

    It has to leave the model believing it wrote the file — otherwise the next
    turn re-derives the contents into the reply — while carrying none of it, and
    naming the way back to the exact text if it turns out to be needed.
    """
    body = "# Plan\n\n" + "Ship the queue. " * 200
    receipt = _document_receipt(
        title="Plan", filename="plan.md", markdown=body, truncated=False
    )
    assert "Ship the queue." not in receipt
    assert 'read_document("plan.md")' in receipt
    assert len(receipt) < len(body)
    truncated = _document_receipt(
        title="Plan", filename="plan.md", markdown=body, truncated=True
    )
    assert "TRUNCATED" in truncated


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("image/png", "image"),
        ("application/pdf", "pdf"),
        ("text/markdown", "text"),
        ("text/x-python", "text"),
        ("", "text"),
    ],
)
def test_upload_kind_classification(media_type, expected):
    assert attachment_kind(media_type) == expected


def test_upload_manifest_announces_files_without_their_contents():
    """The manifest is the whole point of making uploads lazy.

    It has to be complete enough that the model knows what it has and can name a
    file to read, and empty enough that a 90-page PDF costs nothing until asked
    for.
    """
    assert _upload_manifest([]) == ""
    manifest = _upload_manifest(
        [
            SimpleNamespace(
                original_name="spec.pdf",
                media_type="application/pdf",
                size_bytes=2_500_000,
                extracted_text="CONFIDENTIAL launch plan",
                summarization_instruction=None,
            ),
            SimpleNamespace(
                original_name="mock.png",
                media_type="image/png",
                size_bytes=4096,
                extracted_text=None,
                summarization_instruction="Describe the layout",
            ),
            SimpleNamespace(
                original_name="scan.pdf",
                media_type="application/pdf",
                size_bytes=1024,
                extracted_text=None,
                summarization_instruction=None,
            ),
        ]
    )
    assert "CONFIDENTIAL" not in manifest
    assert "spec.pdf (PDF, 2.4 MB, text extracted)" in manifest
    assert "mock.png (image, 4.0 KB, has a summarization instruction)" in manifest
    assert "scan.pdf (PDF, 1.0 KB, no extractable text)" in manifest
    assert "listed, not loaded" in manifest
    assert "list_documents()" in manifest


def test_binary_tool_returns_leave_a_pointer_in_durable_history():
    """Bytes a tool read must not become permanent history.

    A BinaryContent does not survive the JSON round-trip as binary — it comes
    back as a mapping the provider would resend as base64 text on every later
    turn, unreadable and megabytes at a time. So the image lives for the turn
    that read it and leaves a note behind. Text in the same tool return, and the
    parts around it, are untouched.
    """
    image = BinaryContent(
        data=b"\x89PNG\r\n\x1a\nnot-a-real-png", media_type="image/png"
    )
    # The shape pydantic-ai actually produces for ToolReturn.content: the bytes
    # arrive as a follow-on user part beside the tool return, not inside it.
    history = [
        ModelRequest(
            parts=[
                UserPromptPart(content="what does the chart say?"),
                ToolReturnPart(
                    tool_name="read_document",
                    content=["chart.png — uploaded by the user", image],
                    tool_call_id="call-read",
                ),
                UserPromptPart(content=[image]),
            ]
        ),
        ModelResponse(parts=[TextPart(content="Revenue climbs.")]),
    ]
    stripped = strip_binary_content(history)
    kept = stripped[0].parts[1].content
    assert kept == ["chart.png — uploaded by the user", BINARY_HISTORY_PLACEHOLDER]
    assert stripped[0].parts[2].content == [BINARY_HISTORY_PLACEHOLDER]
    assert stripped[0].parts[0].content == "what does the chart say?"
    assert stripped[1] == history[1]
    # The live run may still be using these messages.
    assert history[0].parts[1].content[1] is image
    # And what is stored now round-trips to exactly what was stored.
    assert (
        decode_model_messages(encode_model_messages(stripped))[0].parts[1].content
        == kept
    )


def test_patches_apply_in_order_and_each_sees_the_last_ones_result():
    body = "# Report\n\nThe queue is slow.\n\nEnd.\n"
    edited = apply_document_patches(
        body,
        [
            ("The queue is slow.", "The queue is slow under burst load."),
            ("under burst load.", "under burst load, by about 40%."),
        ],
    )
    assert edited == (
        "# Report\n\nThe queue is slow under burst load, by about 40%.\n\nEnd.\n"
    )


def test_a_patch_may_delete_by_replacing_with_nothing():
    assert apply_document_patches("keep\ndrop\n", [("drop\n", "")]) == "keep\n"


@pytest.mark.parametrize(
    ("body", "patches", "expected"),
    [
        ("one two\n", [("three", "four")], "not in the file"),
        ("dup\ndup\n", [("dup", "x")], "appears 2 times"),
        ("text\n", [("text", "text")], "identical to the original"),
        ("text\n", [("", "x")], "text to replace is required"),
        ("text\n", [], "At least one patch"),
        ("only\n", [("only\n", "   ")], "would empty the file"),
    ],
)
def test_unusable_patches_are_refused_with_the_reason(body, patches, expected):
    with pytest.raises(MarkdownDocumentError) as exc:
        apply_document_patches(body, patches)
    assert expected in str(exc.value)


def test_a_failed_patch_abandons_every_patch_in_the_call():
    body = "alpha\nbeta\n"
    with pytest.raises(MarkdownDocumentError):
        apply_document_patches(body, [("alpha", "ALPHA"), ("missing", "x")])
    # The function is pure, so "nothing was applied" is literally true: the
    # caller still holds the original and never reached the durable write.
    assert body == "alpha\nbeta\n"


def test_patch_count_and_result_size_are_bounded():
    with pytest.raises(MarkdownDocumentError) as too_many:
        apply_document_patches(
            "x" * 40, [(f"{index}", f"{index}!") for index in range(21)]
        )
    assert "Too many patches" in str(too_many.value)

    body = "a" + "b" * 40
    with pytest.raises(MarkdownDocumentError) as too_big:
        apply_document_patches(body, [("a", "c" * (MAX_DOCUMENT_MARKDOWN_CHARS + 1))])
    assert "characters" in str(too_big.value)


def test_overwrite_only_displaces_a_file_once_the_new_one_is_real():
    # The statuses a replacement must reach before the old file is retired.
    # "stopped" and "failed" are deliberately absent: half a file must never be
    # what displaces a whole one.
    assert REPLACING_STATUSES == ("complete", "truncated")
    assert "superseded" in DOCUMENT_STATUSES
    assert "superseded" not in READABLE_STATUSES


def test_document_guidance_teaches_editing_and_the_overwrite_rule():
    prompt = effective_system_prompt(child=False)
    assert "edit_document(filename, patches)" in prompt
    assert "overwrite=True" in prompt
    # Children have no document tools at all, so none of this reaches them.
    assert "edit_document" not in effective_system_prompt(child=True)
