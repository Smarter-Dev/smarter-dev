"""Deterministic product-invariant tests for Smarter Dev web Chat."""

from __future__ import annotations

import inspect
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

import pytest
from litestar import Litestar
from pydantic_ai.messages import ModelRequest
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
from smarter_dev.web.chat.entitlements import has_chat
from smarter_dev.web.chat.entitlements import has_ultra_chat
from smarter_dev.web.chat.entitlements import resolve_spend_tier
from smarter_dev.web.chat.policy import compaction_model_key
from smarter_dev.web.chat.policy import policy_for
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


def test_unpriced_catalog_choice_does_not_block_other_admin_changes():
    filtered, skipped = filter_unpriced_catalog_selections(
        {
            "gpt-5-6-luna": (True, "low"),
            "gpt-5-6-sol": (True, "medium"),
        }
    )
    assert filtered["gpt-5-6-luna"] == (True, "low")
    assert filtered["gpt-5-6-sol"] == (False, "medium")
    assert skipped == ["gpt-5-6-sol"]


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


def test_children_cannot_recurse_and_prompt_omits_guidance():
    assert tool_names_for_child(["web_search", "run_subagent", "web_read"]) == [
        "web_search",
        "web_read",
    ]
    assert "run_subagent" in effective_system_prompt(child=False)
    assert "run_subagent" not in effective_system_prompt(child=True)
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
