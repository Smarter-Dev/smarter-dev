"""Tests for the monthly-invoice / per-channel usage aggregation module."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from smarter_dev.web.models import (
    ChatAgentCompactionEvent,
    ChatAgentEngagement,
    ChatAgentTurn,
    ResearchSession,
    ScanServiceUsage,
)
from smarter_dev.web.usage_invoice import (
    PROVIDER_LABELS,
    available_months,
    bare_model_name,
    channel_breakdown,
    month_bounds,
    monthly_invoice,
    provider_key_from_model_name,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_provider_key_maps_every_known_prefix():
    assert provider_key_from_model_name("google-gla:gemini-3.1") == "google"
    assert provider_key_from_model_name("google:gemini-3.1") == "google"
    assert provider_key_from_model_name("openai:gpt-5.4") == "openai"
    assert provider_key_from_model_name("openai-responses:gpt-5.4") == "openai"
    assert provider_key_from_model_name("anthropic:claude-sonnet-5") == "anthropic"
    assert provider_key_from_model_name("digitalocean:some-model") == "digitalocean"


def test_provider_key_resolves_flat_wire_ids_via_catalog():
    # Chat/voice/summarizer buckets store flat provider-less wire ids; the
    # model catalog says which provider serves each one.
    assert provider_key_from_model_name("gemini-3.1-flash-lite") == "google"
    assert provider_key_from_model_name("gpt-5.4") == "openai"
    assert provider_key_from_model_name("claude-sonnet-5") == "anthropic"
    assert provider_key_from_model_name("kimi-k2.6") == "digitalocean"


def test_provider_key_unknown_for_uncatalogued_names_and_unknown_prefix():
    assert provider_key_from_model_name("some-ad-hoc-model") == "unknown"
    assert provider_key_from_model_name("mystery:model") == "unknown"
    assert provider_key_from_model_name(None) == "unknown"


def test_provider_labels_cover_every_key():
    for key in ("google", "openai", "anthropic", "digitalocean", "unknown"):
        assert key in PROVIDER_LABELS
    assert PROVIDER_LABELS["google"] == "Google"
    assert PROVIDER_LABELS["openai"] == "OpenAI"
    assert PROVIDER_LABELS["anthropic"] == "Anthropic"
    assert PROVIDER_LABELS["digitalocean"] == "DigitalOcean"
    assert PROVIDER_LABELS["unknown"] == "Unknown"


def test_bare_model_name_strips_prefix_and_handles_none():
    assert bare_model_name("google-gla:gemini-3.1-flash-lite") == "gemini-3.1-flash-lite"
    assert bare_model_name("gemini-3.1-flash-lite") == "gemini-3.1-flash-lite"
    assert bare_model_name(None) == "unknown"


# ---------------------------------------------------------------------------
# month_bounds
# ---------------------------------------------------------------------------


def test_month_bounds_half_open_utc_range():
    start, end = month_bounds("2026-07")
    assert start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_month_bounds_wraps_year_in_december():
    start, end = month_bounds("2026-12")
    assert start == datetime(2026, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2027, 1, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("bad", ["2026", "2026-13", "2026-00", "not-a-month", "26-07", ""])
def test_month_bounds_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        month_bounds(bad)


# ---------------------------------------------------------------------------
# Fixtures / row builders
# ---------------------------------------------------------------------------


def _engagement(guild_id="G1", channel_id="C1", channel_name=None):
    return ChatAgentEngagement(
        guild_id=guild_id,
        channel_id=channel_id,
        channel_name=channel_name,
        activation_user_id="U1",
        activation_username="alice",
        activation_message_id="M1",
    )


def _turn(engagement, *, started_at, **kwargs):
    defaults = dict(
        request_id="req1",
        turn_kind="initial",
        output_kind="send_response",
        triggering_messages=[],
        agent_output={},
    )
    defaults.update(kwargs)
    return ChatAgentTurn(engagement=engagement, started_at=started_at, **defaults)


def _at(day=15):
    return datetime(2026, 7, day, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# monthly_invoice
# ---------------------------------------------------------------------------


async def test_empty_month_returns_no_lines(db_session):
    assert await monthly_invoice(db_session, "2026-07") == []


async def test_multi_source_aggregation_with_mixed_reasoning(db_session):
    engagement = _engagement()
    # Two chat turns on the same model but different reasoning levels ->
    # two distinct lines. Third turn shares (model, high) with the first.
    turn_high_a = _turn(
        engagement,
        started_at=_at(2),
        chat_model_name="gpt-5.4",
        chat_reasoning_level="high",
        chat_tokens_input=100,
        chat_tokens_output=40,
        chat_cost_usd=Decimal("0.010000"),
    )
    turn_high_b = _turn(
        engagement,
        started_at=_at(3),
        chat_model_name="gpt-5.4",
        chat_reasoning_level="high",
        chat_tokens_input=10,
        chat_tokens_output=5,
        chat_cost_usd=Decimal("0.002000"),
    )
    turn_low = _turn(
        engagement,
        started_at=_at(4),
        chat_model_name="gpt-5.4",
        chat_reasoning_level="low",
        chat_tokens_input=1,
        chat_tokens_output=1,
        chat_cost_usd=Decimal("0.000500"),
    )
    # A voice call rides on turn_high_a.
    turn_high_a.voice_model_name = "gemini-2.5-flash-preview-tts"
    turn_high_a.voice_tokens_input = 20
    turn_high_a.voice_tokens_output = 300
    turn_high_a.voice_cost_usd = Decimal("0.030000")

    compaction = ChatAgentCompactionEvent(
        turn=turn_high_a,
        event_kind="assistant_text",
        original_content="x",
        summary="y",
        original_chars=1,
        summary_chars=1,
        chars_saved=0,
        summarizer_model_name="gemini-3.1-flash-lite",
        summarizer_reasoning_level="low",
        summarizer_tokens_input=200,
        summarizer_tokens_output=50,
        summarizer_cost_usd=Decimal("0.001000"),
    )
    research = ResearchSession(
        query="q",
        status="complete",
        guild_id="G1",
        channel_id="C1",
        model_name="google-gla:gemini-3.1-flash-lite-preview",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=200,
        cache_write_tokens=10,
        cost_usd=Decimal("0.050000"),
        created_at=_at(6),
    )
    service = ScanServiceUsage(
        task_type="profile",
        model_name="gemini-3.1-flash-lite",
        input_tokens=300,
        output_tokens=100,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost_usd=Decimal("0.004000"),
        created_at=_at(7),
    )
    db_session.add_all(
        [engagement, turn_high_a, turn_high_b, turn_low, compaction, research, service]
    )
    await db_session.commit()

    lines = await monthly_invoice(db_session, "2026-07")

    by_key = {(l.source, l.model_name, l.reasoning_level): l for l in lines}

    chat_high = by_key[("chat", "gpt-5.4", "high")]
    assert chat_high.provider_key == "openai"  # flat wire name, resolved via catalog
    assert chat_high.input_tokens == 110
    assert chat_high.output_tokens == 45
    assert chat_high.cost_usd == Decimal("0.012000")

    chat_low = by_key[("chat", "gpt-5.4", "low")]
    assert chat_low.input_tokens == 1
    assert chat_low.cost_usd == Decimal("0.000500")

    voice = by_key[("voice", "gemini-2.5-flash-preview-tts", None)]
    assert voice.output_tokens == 300
    assert voice.cost_usd == Decimal("0.030000")

    compaction_line = by_key[("compaction", "gemini-3.1-flash-lite", "low")]
    assert compaction_line.input_tokens == 200
    assert compaction_line.cost_usd == Decimal("0.001000")

    scan = by_key[("scan", "gemini-3.1-flash-lite-preview", None)]
    assert scan.provider_key == "google"  # research names ARE prefixed
    assert scan.provider_label == "Google"
    assert scan.cache_read_tokens == 200
    assert scan.cache_write_tokens == 10
    assert scan.cost_usd == Decimal("0.050000")

    scan_service = by_key[("scan_service", "gemini-3.1-flash-lite", None)]
    assert scan_service.input_tokens == 300
    assert scan_service.cost_usd == Decimal("0.004000")

    # Sorted by cost descending.
    costs = [l.cost_usd for l in lines]
    assert costs == sorted(costs, reverse=True)


async def test_month_filter_excludes_out_of_range_rows(db_session):
    engagement = _engagement()
    inside = _turn(
        engagement,
        started_at=_at(15),
        chat_model_name="m",
        chat_tokens_input=100,
        chat_cost_usd=Decimal("0.001000"),
    )
    before = _turn(
        engagement,
        started_at=datetime(2026, 6, 30, 23, 59, tzinfo=timezone.utc),
        chat_model_name="m",
        chat_tokens_input=999,
        chat_cost_usd=Decimal("9.000000"),
    )
    after = _turn(
        engagement,
        started_at=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
        chat_model_name="m",
        chat_tokens_input=999,
        chat_cost_usd=Decimal("9.000000"),
    )
    db_session.add_all([engagement, inside, before, after])
    await db_session.commit()

    lines = await monthly_invoice(db_session, "2026-07")
    assert len(lines) == 1
    assert lines[0].input_tokens == 100
    assert lines[0].cost_usd == Decimal("0.001000")


async def test_null_cost_sums_coalesce_to_decimal_zero(db_session):
    research = ResearchSession(
        query="q",
        status="complete",
        model_name="google-gla:gemini-3.1",
        input_tokens=10,
        output_tokens=5,
        cost_usd=None,
        created_at=_at(9),
    )
    db_session.add(research)
    await db_session.commit()

    lines = await monthly_invoice(db_session, "2026-07")
    assert len(lines) == 1
    assert lines[0].cost_usd == Decimal("0")
    assert isinstance(lines[0].cost_usd, Decimal)


async def test_voice_lines_only_appear_when_a_voice_call_happened(db_session):
    engagement = _engagement()
    # A chat-only turn (no voice model) must NOT create a voice line.
    turn = _turn(
        engagement,
        started_at=_at(10),
        chat_model_name="m",
        chat_tokens_input=5,
        chat_cost_usd=Decimal("0.001000"),
    )
    db_session.add_all([engagement, turn])
    await db_session.commit()

    lines = await monthly_invoice(db_session, "2026-07")
    assert [l.source for l in lines] == ["chat"]


# ---------------------------------------------------------------------------
# channel_breakdown
# ---------------------------------------------------------------------------


async def test_channel_breakdown_joins_engagement_for_identity(db_session):
    busy = _engagement(channel_id="C-busy", channel_name="general")
    quiet = _engagement(channel_id="C-quiet")
    db_session.add_all(
        [
            busy,
            quiet,
            _turn(
                busy,
                started_at=_at(2),
                chat_model_name="m",
                chat_reasoning_level="high",
                chat_tokens_input=100,
                chat_tokens_output=50,
                chat_cost_usd=Decimal("0.100000"),
            ),
            _turn(
                quiet,
                started_at=_at(3),
                chat_model_name="m",
                chat_reasoning_level="high",
                chat_tokens_input=10,
                chat_tokens_output=5,
                chat_cost_usd=Decimal("0.010000"),
            ),
        ]
    )
    await db_session.commit()

    lines = await channel_breakdown(db_session, "2026-07")
    assert [(l.channel_id, l.cost_usd) for l in lines] == [
        ("C-busy", Decimal("0.100000")),
        ("C-quiet", Decimal("0.010000")),
    ]
    busy_line = next(l for l in lines if l.channel_id == "C-busy")
    assert busy_line.guild_id == "G1"
    assert busy_line.channel_name == "general"
    assert busy_line.model_name == "m"
    assert busy_line.reasoning_level == "high"
    assert busy_line.source == "chat"


async def test_channel_breakdown_aggregates_engagements_sharing_a_channel(db_session):
    first = _engagement(channel_id="C1", channel_name="general")
    second = _engagement(channel_id="C1", channel_name=None)
    db_session.add_all(
        [
            first,
            second,
            _turn(
                first,
                started_at=_at(2),
                chat_model_name="m",
                chat_tokens_input=100,
                chat_cost_usd=Decimal("0.010000"),
            ),
            _turn(
                second,
                started_at=_at(3),
                chat_model_name="m",
                chat_tokens_input=50,
                chat_cost_usd=Decimal("0.005000"),
            ),
        ]
    )
    await db_session.commit()

    lines = await channel_breakdown(db_session, "2026-07")
    assert len(lines) == 1
    line = lines[0]
    assert line.input_tokens == 150
    assert line.cost_usd == Decimal("0.015000")
    # Name fallback: the non-null snapshot wins over the NULL one.
    assert line.channel_name == "general"


async def test_channel_breakdown_includes_research_without_channel_name(db_session):
    research = ResearchSession(
        query="q",
        status="complete",
        guild_id="G9",
        channel_id="C9",
        model_name="google-gla:gemini-3.1",
        input_tokens=1000,
        output_tokens=200,
        cost_usd=Decimal("0.200000"),
        created_at=_at(5),
    )
    db_session.add(research)
    await db_session.commit()

    lines = await channel_breakdown(db_session, "2026-07")
    assert len(lines) == 1
    line = lines[0]
    assert line.source == "scan"
    assert line.guild_id == "G9"
    assert line.channel_id == "C9"
    assert line.channel_name is None
    assert line.provider_key == "google"
    assert line.cost_usd == Decimal("0.200000")


async def test_channel_breakdown_skips_scan_service(db_session):
    db_session.add(
        ScanServiceUsage(
            task_type="profile",
            model_name="gemini-3.1-flash-lite",
            input_tokens=100,
            output_tokens=50,
            cost_usd=Decimal("0.010000"),
            created_at=_at(5),
        )
    )
    await db_session.commit()

    lines = await channel_breakdown(db_session, "2026-07")
    assert lines == []


async def test_channel_breakdown_excludes_out_of_range_rows(db_session):
    engagement = _engagement(channel_id="C1")
    db_session.add_all(
        [
            engagement,
            _turn(
                engagement,
                started_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                chat_model_name="m",
                chat_tokens_input=999,
                chat_cost_usd=Decimal("9.000000"),
            ),
        ]
    )
    await db_session.commit()

    assert await channel_breakdown(db_session, "2026-07") == []


# ---------------------------------------------------------------------------
# available_months
# ---------------------------------------------------------------------------


async def test_available_months_span_from_earliest_record_to_now_descending(db_session):
    engagement = _engagement()
    db_session.add_all(
        [
            engagement,
            _turn(
                engagement,
                started_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
                chat_model_name="m",
                chat_tokens_input=1,
            ),
        ]
    )
    await db_session.commit()

    months = await available_months(db_session)
    now = datetime.now(timezone.utc)
    current = f"{now.year:04d}-{now.month:02d}"
    assert months[0] == current  # descending: newest first
    assert months[-1] == "2026-05"  # earliest record's month
    # Strictly descending, no duplicates.
    assert months == sorted(set(months), reverse=True)
    assert "2026-05" in months


async def test_available_months_defaults_to_current_month_when_empty(db_session):
    months = await available_months(db_session)
    now = datetime.now(timezone.utc)
    assert months == [f"{now.year:04d}-{now.month:02d}"]
