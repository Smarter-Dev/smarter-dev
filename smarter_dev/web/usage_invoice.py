"""Aggregation queries backing the admin LLM-usage "invoice" dashboard.

Pure data layer: turns the four cost-bearing tables (chat turns, chat
compaction events, research sessions, and internal scan-service runs) into
flat, provider-labelled invoice lines for a given calendar month, plus a
per-Discord-channel breakdown.

Every query uses plain ``>= start`` / ``< end`` range predicates and
``func.sum`` / ``func.coalesce`` grouping so it runs unchanged on SQLite
(no ``date_trunc`` / ``to_char``). Provider identity is derived in Python
from the stored model-name string.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from decimal import Decimal

from sqlalchemy import String
from sqlalchemy import cast
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.web.models import ChatAgentCompactionEvent
from smarter_dev.web.models import ChatAgentEngagement
from smarter_dev.web.models import ChatAgentTurn
from smarter_dev.web.models import ResearchSession
from smarter_dev.web.models import ScanServiceUsage
from smarter_dev.web.models import UsageCostRow

# ---------------------------------------------------------------------------
# Provider identity
# ---------------------------------------------------------------------------

# Wire/pydantic-ai model-name prefix -> internal provider key.
_PROVIDER_KEYS: dict[str, str] = {
    "google-gla": "google",
    "google": "google",
    "openai": "openai",
    "openai-responses": "openai",
    "anthropic": "anthropic",
    "digitalocean": "digitalocean",
}

# The chat/voice/summarizer buckets store flat provider-less wire ids
# (e.g. "gpt-5.4"); the catalog knows which provider serves each of those.
_PROVIDER_BY_FLAT_MODEL_ID: dict[str, str] = {
    catalog_model.model_id: catalog_model.provider.value
    for catalog_model in MODEL_CATALOG
}
# Wire ids of retired catalog models — historical usage rows still carry them.
_PROVIDER_BY_FLAT_MODEL_ID.setdefault("gemini-3.5-flash", "google")
# Retired when GLM/DeepSeek moved to OpenCode Zen and Laguna XS left the catalog:
# the models live on under new ids (or not at all), but rows written before the
# move still carry the old wire id and would otherwise fall to "unknown" and
# silently drop out of the per-provider invoice breakdown.
_PROVIDER_BY_FLAT_MODEL_ID.setdefault("deepseek-4-flash", "digitalocean")
_PROVIDER_BY_FLAT_MODEL_ID.setdefault("poolside/laguna-xs-2.1", "openrouter")
# Laguna S briefly ran on Zen's free tier before its rate limiting sent it
# back to OpenRouter; rows from that window carry the free id.
_PROVIDER_BY_FLAT_MODEL_ID.setdefault("laguna-s-2.1-free", "opencode_zen")

# Provider key -> human display label.
PROVIDER_LABELS: dict[str, str] = {
    "google": "Google",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "digitalocean": "DigitalOcean",
    # This table is indexed directly, so a missing key is a KeyError mid-invoice
    # rather than a fallback — openrouter was absent even while Laguna shipped
    # on it, so any Laguna line would have raised.
    "openrouter": "OpenRouter",
    "opencode_zen": "OpenCode Zen",
    "unknown": "Unknown",
}


def provider_key_from_model_name(model_name: str | None) -> str:
    """Derive the provider key from a stored model-name string.

    A ``"provider:model"`` name (research sessions) maps via its prefix.
    A flat provider-less wire id (chat/voice/compaction/scan-service
    buckets) maps via the model catalog. Anything unrecognised is
    ``"unknown"``.
    """
    if not model_name:
        return "unknown"
    if ":" in model_name:
        prefix = model_name.split(":", 1)[0]
        return _PROVIDER_KEYS.get(prefix, "unknown")
    return _PROVIDER_BY_FLAT_MODEL_ID.get(model_name, "unknown")


def bare_model_name(model_name: str | None) -> str:
    """Strip the ``"provider:"`` prefix from a model name, if present."""
    if not model_name:
        return "unknown"
    if ":" in model_name:
        return model_name.split(":", 1)[1]
    return model_name


# ---------------------------------------------------------------------------
# Month handling
# ---------------------------------------------------------------------------


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """Parse ``"YYYY-MM"`` into a tz-aware UTC half-open ``[start, end)`` range.

    ``end`` is the first instant of the following month. Raises ``ValueError``
    on any malformed input (fail fast).
    """
    parts = month.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"month must be 'YYYY-MM', got {month!r}")
    year = int(parts[0])  # int() raises ValueError on non-numeric parts
    month_number = int(parts[1])
    if not 1 <= month_number <= 12:
        raise ValueError(f"month out of range: {month!r}")
    start = datetime(year, month_number, 1, tzinfo=UTC)
    if month_number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month_number + 1, 1, tzinfo=UTC)
    return start, end


def _month_label(moment: datetime) -> str:
    return f"{moment.year:04d}-{moment.month:02d}"


async def available_months(db_session: AsyncSession) -> list[str]:
    """Distinct ``"YYYY-MM"`` labels, newest first.

    Spans from the earliest record across the four cost tables up to the
    current UTC month (inclusive). Computed portably: min timestamps come
    from the database, the month list is generated in Python so it works on
    SQLite. When no records exist, returns just the current month.
    """
    earliest_columns = (
        ChatAgentTurn.started_at,
        ChatAgentCompactionEvent.created_at,
        ResearchSession.created_at,
        ScanServiceUsage.created_at,
        UsageCostRow.metered_at,
    )
    earliest: datetime | None = None
    for column in earliest_columns:
        # Each table queried separately: a single cross-table SELECT would
        # null every min() as soon as one table is empty.
        table_min = await db_session.scalar(select(func.min(column)))
        if table_min is None:
            continue
        if table_min.tzinfo is None:
            table_min = table_min.replace(tzinfo=UTC)
        if earliest is None or table_min < earliest:
            earliest = table_min

    now = datetime.now(UTC)
    if earliest is None:
        return [_month_label(now)]

    months: list[str] = []
    year, month = now.year, now.month
    while (year, month) >= (earliest.year, earliest.month):
        months.append(f"{year:04d}-{month:02d}")
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
    return months


# ---------------------------------------------------------------------------
# Invoice lines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InvoiceLine:
    """One aggregated (model, reasoning, source) row for a month."""

    provider_key: str
    provider_label: str
    model_name: str
    reasoning_level: str | None
    source: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class ChannelUsageLine:
    """One aggregated per-channel (model, reasoning, source) row for a month."""

    guild_id: str | None
    channel_id: str | None
    channel_name: str | None
    provider_key: str
    provider_label: str
    model_name: str
    reasoning_level: str | None
    source: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


def _invoice_line(
    *,
    raw_model_name: str | None,
    reasoning_level: str | None,
    source: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    cost_usd: Decimal,
) -> InvoiceLine:
    provider_key = provider_key_from_model_name(raw_model_name)
    return InvoiceLine(
        provider_key=provider_key,
        provider_label=PROVIDER_LABELS[provider_key],
        model_name=bare_model_name(raw_model_name),
        reasoning_level=reasoning_level,
        source=source,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cache_read_tokens=int(cache_read_tokens),
        cache_write_tokens=int(cache_write_tokens),
        cost_usd=cost_usd,
    )


def _channel_line(
    *,
    guild_id: str | None,
    channel_id: str | None,
    channel_name: str | None,
    raw_model_name: str | None,
    reasoning_level: str | None,
    source: str,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
) -> ChannelUsageLine:
    provider_key = provider_key_from_model_name(raw_model_name)
    return ChannelUsageLine(
        guild_id=guild_id,
        channel_id=channel_id,
        channel_name=channel_name,
        provider_key=provider_key,
        provider_label=PROVIDER_LABELS[provider_key],
        model_name=bare_model_name(raw_model_name),
        reasoning_level=reasoning_level,
        source=source,
        input_tokens=int(input_tokens),
        output_tokens=int(output_tokens),
        cost_usd=cost_usd,
    )


def _tokens(column):
    return func.coalesce(func.sum(column), 0)


def _cost(column):
    return func.coalesce(func.sum(column), Decimal("0"))


async def monthly_invoice(db_session: AsyncSession, month: str) -> list[InvoiceLine]:
    """Aggregate every cost source for *month* into flat invoice lines.

    Each source is grouped by ``(model_name, reasoning_level)`` (voice and
    the scan sources have no reasoning knob, so their reasoning level is
    always ``None``). Lines are returned sorted by ``cost_usd`` descending.
    """
    start, end = month_bounds(month)
    lines: list[InvoiceLine] = []

    ledger_rows = (
        await db_session.execute(
            select(
                UsageCostRow.product_mode,
                UsageCostRow.operation_type,
                UsageCostRow.provider_key,
                UsageCostRow.model_id,
                UsageCostRow.reasoning_level,
                _tokens(UsageCostRow.input_tokens).label("input_tokens"),
                _tokens(UsageCostRow.output_tokens).label("output_tokens"),
                _tokens(UsageCostRow.cache_read_tokens).label("cache_read_tokens"),
                _tokens(UsageCostRow.cache_write_tokens).label("cache_write_tokens"),
                _cost(UsageCostRow.cost_usd).label("cost_usd"),
            )
            .where(UsageCostRow.metered_at >= start, UsageCostRow.metered_at < end)
            .group_by(
                UsageCostRow.product_mode,
                UsageCostRow.operation_type,
                UsageCostRow.provider_key,
                UsageCostRow.model_id,
                UsageCostRow.reasoning_level,
            )
        )
    ).all()
    for row in ledger_rows:
        provider_key = (
            row.provider_key if row.provider_key in PROVIDER_LABELS else "unknown"
        )
        lines.append(
            InvoiceLine(
                provider_key=provider_key,
                provider_label=PROVIDER_LABELS[provider_key],
                model_name=row.model_id,
                reasoning_level=row.reasoning_level,
                source=f"{row.product_mode}:{row.operation_type}",
                input_tokens=int(row.input_tokens or 0),
                output_tokens=int(row.output_tokens or 0),
                cache_read_tokens=int(row.cache_read_tokens or 0),
                cache_write_tokens=int(row.cache_write_tokens or 0),
                cost_usd=Decimal(row.cost_usd or 0),
            )
        )

    # Legacy Discord rows remain a fallback for pre-ledger databases/tests.
    # -- chat bucket -------------------------------------------------------
    chat_stmt = (
        select(
            ChatAgentTurn.chat_model_name,
            ChatAgentTurn.chat_reasoning_level,
            _tokens(ChatAgentTurn.chat_tokens_input).label("input_tokens"),
            _tokens(ChatAgentTurn.chat_tokens_output).label("output_tokens"),
            _tokens(ChatAgentTurn.chat_cache_read_tokens).label("cache_read_tokens"),
            _tokens(ChatAgentTurn.chat_cache_write_tokens).label("cache_write_tokens"),
            _cost(ChatAgentTurn.chat_cost_usd).label("cost_usd"),
        )
        .where(ChatAgentTurn.started_at >= start)
        .where(ChatAgentTurn.started_at < end)
        .where(
            ~select(UsageCostRow.id)
            .where(
                UsageCostRow.product_mode == "discord",
                UsageCostRow.operation_type == "primary",
                UsageCostRow.root_turn_id == ChatAgentTurn.id,
            )
            .exists()
        )
        .group_by(ChatAgentTurn.chat_model_name, ChatAgentTurn.chat_reasoning_level)
    )
    for row in (await db_session.execute(chat_stmt)).all():
        lines.append(
            _invoice_line(
                raw_model_name=row.chat_model_name,
                reasoning_level=row.chat_reasoning_level,
                source="chat",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_read_tokens=row.cache_read_tokens,
                cache_write_tokens=row.cache_write_tokens,
                cost_usd=row.cost_usd,
            )
        )

    # -- voice bucket ------------------------------------------------------
    # A turn only belongs to the voice source when a voice call actually
    # happened, which is exactly when voice_model_name was recorded.
    voice_stmt = (
        select(
            ChatAgentTurn.voice_model_name,
            _tokens(ChatAgentTurn.voice_tokens_input).label("input_tokens"),
            _tokens(ChatAgentTurn.voice_tokens_output).label("output_tokens"),
            _cost(ChatAgentTurn.voice_cost_usd).label("cost_usd"),
        )
        .where(ChatAgentTurn.started_at >= start)
        .where(ChatAgentTurn.started_at < end)
        .where(ChatAgentTurn.voice_model_name.is_not(None))
        .where(
            ~select(UsageCostRow.id)
            .where(
                UsageCostRow.product_mode == "discord",
                UsageCostRow.operation_type == "voice",
                UsageCostRow.root_turn_id == ChatAgentTurn.id,
            )
            .exists()
        )
        .group_by(ChatAgentTurn.voice_model_name)
    )
    for row in (await db_session.execute(voice_stmt)).all():
        lines.append(
            _invoice_line(
                raw_model_name=row.voice_model_name,
                reasoning_level=None,
                source="voice",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_read_tokens=0,
                cache_write_tokens=0,
                cost_usd=row.cost_usd,
            )
        )

    # -- compaction bucket -------------------------------------------------
    compaction_stmt = (
        select(
            ChatAgentCompactionEvent.summarizer_model_name,
            ChatAgentCompactionEvent.summarizer_reasoning_level,
            _tokens(ChatAgentCompactionEvent.summarizer_tokens_input).label(
                "input_tokens"
            ),
            _tokens(ChatAgentCompactionEvent.summarizer_tokens_output).label(
                "output_tokens"
            ),
            _tokens(ChatAgentCompactionEvent.summarizer_cache_read_tokens).label(
                "cache_read_tokens"
            ),
            _tokens(ChatAgentCompactionEvent.summarizer_cache_write_tokens).label(
                "cache_write_tokens"
            ),
            _cost(ChatAgentCompactionEvent.summarizer_cost_usd).label("cost_usd"),
        )
        .where(ChatAgentCompactionEvent.created_at >= start)
        .where(ChatAgentCompactionEvent.created_at < end)
        .where(
            ~select(UsageCostRow.id)
            .where(
                UsageCostRow.product_mode == "discord",
                UsageCostRow.operation_type == "compaction",
                UsageCostRow.operation_key.like(
                    "%:compaction:" + cast(ChatAgentCompactionEvent.id, String)
                ),
            )
            .exists()
        )
        .group_by(
            ChatAgentCompactionEvent.summarizer_model_name,
            ChatAgentCompactionEvent.summarizer_reasoning_level,
        )
    )
    for row in (await db_session.execute(compaction_stmt)).all():
        lines.append(
            _invoice_line(
                raw_model_name=row.summarizer_model_name,
                reasoning_level=row.summarizer_reasoning_level,
                source="compaction",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_read_tokens=row.cache_read_tokens,
                cache_write_tokens=row.cache_write_tokens,
                cost_usd=row.cost_usd,
            )
        )

    # -- scan research bucket ---------------------------------------------
    research_stmt = (
        select(
            ResearchSession.model_name,
            _tokens(ResearchSession.input_tokens).label("input_tokens"),
            _tokens(ResearchSession.output_tokens).label("output_tokens"),
            _tokens(ResearchSession.cache_read_tokens).label("cache_read_tokens"),
            _tokens(ResearchSession.cache_write_tokens).label("cache_write_tokens"),
            _cost(ResearchSession.cost_usd).label("cost_usd"),
        )
        .where(ResearchSession.created_at >= start)
        .where(ResearchSession.created_at < end)
        .group_by(ResearchSession.model_name)
    )
    for row in (await db_session.execute(research_stmt)).all():
        lines.append(
            _invoice_line(
                raw_model_name=row.model_name,
                reasoning_level=None,
                source="scan",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_read_tokens=row.cache_read_tokens,
                cache_write_tokens=row.cache_write_tokens,
                cost_usd=row.cost_usd,
            )
        )

    # -- scan internal-service bucket -------------------------------------
    service_stmt = (
        select(
            ScanServiceUsage.model_name,
            _tokens(ScanServiceUsage.input_tokens).label("input_tokens"),
            _tokens(ScanServiceUsage.output_tokens).label("output_tokens"),
            _tokens(ScanServiceUsage.cache_read_tokens).label("cache_read_tokens"),
            _tokens(ScanServiceUsage.cache_write_tokens).label("cache_write_tokens"),
            _cost(ScanServiceUsage.cost_usd).label("cost_usd"),
        )
        .where(ScanServiceUsage.created_at >= start)
        .where(ScanServiceUsage.created_at < end)
        .group_by(ScanServiceUsage.model_name)
    )
    for row in (await db_session.execute(service_stmt)).all():
        lines.append(
            _invoice_line(
                raw_model_name=row.model_name,
                reasoning_level=None,
                source="scan_service",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cache_read_tokens=row.cache_read_tokens,
                cache_write_tokens=row.cache_write_tokens,
                cost_usd=row.cost_usd,
            )
        )

    return sorted(lines, key=lambda line: line.cost_usd, reverse=True)


async def channel_breakdown(
    db_session: AsyncSession, month: str
) -> list[ChannelUsageLine]:
    """Per-Discord-channel usage lines for *month*, sorted by cost descending.

    Chat, voice, and compaction rows are joined through the engagement for
    channel identity (the channel name is the most-recent non-null snapshot).
    Research sessions carry their own ``guild_id`` / ``channel_id`` but have
    no denormalised channel name. Scan-service rows have no channel and are
    intentionally excluded.
    """
    start, end = month_bounds(month)
    lines: list[ChannelUsageLine] = []

    channel_name = func.max(ChatAgentEngagement.channel_name).label("channel_name")

    # -- chat bucket -------------------------------------------------------
    chat_stmt = (
        select(
            ChatAgentEngagement.guild_id,
            ChatAgentEngagement.channel_id,
            channel_name,
            ChatAgentTurn.chat_model_name,
            ChatAgentTurn.chat_reasoning_level,
            _tokens(ChatAgentTurn.chat_tokens_input).label("input_tokens"),
            _tokens(ChatAgentTurn.chat_tokens_output).label("output_tokens"),
            _cost(ChatAgentTurn.chat_cost_usd).label("cost_usd"),
        )
        .select_from(ChatAgentTurn)
        .join(
            ChatAgentEngagement,
            ChatAgentTurn.engagement_id == ChatAgentEngagement.id,
        )
        .where(ChatAgentTurn.started_at >= start)
        .where(ChatAgentTurn.started_at < end)
        .group_by(
            ChatAgentEngagement.guild_id,
            ChatAgentEngagement.channel_id,
            ChatAgentTurn.chat_model_name,
            ChatAgentTurn.chat_reasoning_level,
        )
    )
    for row in (await db_session.execute(chat_stmt)).all():
        lines.append(
            _channel_line(
                guild_id=row.guild_id,
                channel_id=row.channel_id,
                channel_name=row.channel_name,
                raw_model_name=row.chat_model_name,
                reasoning_level=row.chat_reasoning_level,
                source="chat",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cost_usd=row.cost_usd,
            )
        )

    # -- voice bucket ------------------------------------------------------
    voice_stmt = (
        select(
            ChatAgentEngagement.guild_id,
            ChatAgentEngagement.channel_id,
            channel_name,
            ChatAgentTurn.voice_model_name,
            _tokens(ChatAgentTurn.voice_tokens_input).label("input_tokens"),
            _tokens(ChatAgentTurn.voice_tokens_output).label("output_tokens"),
            _cost(ChatAgentTurn.voice_cost_usd).label("cost_usd"),
        )
        .select_from(ChatAgentTurn)
        .join(
            ChatAgentEngagement,
            ChatAgentTurn.engagement_id == ChatAgentEngagement.id,
        )
        .where(ChatAgentTurn.started_at >= start)
        .where(ChatAgentTurn.started_at < end)
        .where(ChatAgentTurn.voice_model_name.is_not(None))
        .group_by(
            ChatAgentEngagement.guild_id,
            ChatAgentEngagement.channel_id,
            ChatAgentTurn.voice_model_name,
        )
    )
    for row in (await db_session.execute(voice_stmt)).all():
        lines.append(
            _channel_line(
                guild_id=row.guild_id,
                channel_id=row.channel_id,
                channel_name=row.channel_name,
                raw_model_name=row.voice_model_name,
                reasoning_level=None,
                source="voice",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cost_usd=row.cost_usd,
            )
        )

    # -- compaction bucket -------------------------------------------------
    compaction_stmt = (
        select(
            ChatAgentEngagement.guild_id,
            ChatAgentEngagement.channel_id,
            channel_name,
            ChatAgentCompactionEvent.summarizer_model_name,
            ChatAgentCompactionEvent.summarizer_reasoning_level,
            _tokens(ChatAgentCompactionEvent.summarizer_tokens_input).label(
                "input_tokens"
            ),
            _tokens(ChatAgentCompactionEvent.summarizer_tokens_output).label(
                "output_tokens"
            ),
            _cost(ChatAgentCompactionEvent.summarizer_cost_usd).label("cost_usd"),
        )
        .select_from(ChatAgentCompactionEvent)
        .join(
            ChatAgentTurn,
            ChatAgentCompactionEvent.turn_id == ChatAgentTurn.id,
        )
        .join(
            ChatAgentEngagement,
            ChatAgentTurn.engagement_id == ChatAgentEngagement.id,
        )
        .where(ChatAgentCompactionEvent.created_at >= start)
        .where(ChatAgentCompactionEvent.created_at < end)
        .group_by(
            ChatAgentEngagement.guild_id,
            ChatAgentEngagement.channel_id,
            ChatAgentCompactionEvent.summarizer_model_name,
            ChatAgentCompactionEvent.summarizer_reasoning_level,
        )
    )
    for row in (await db_session.execute(compaction_stmt)).all():
        lines.append(
            _channel_line(
                guild_id=row.guild_id,
                channel_id=row.channel_id,
                channel_name=row.channel_name,
                raw_model_name=row.summarizer_model_name,
                reasoning_level=row.summarizer_reasoning_level,
                source="compaction",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cost_usd=row.cost_usd,
            )
        )

    # -- scan research bucket ---------------------------------------------
    research_stmt = (
        select(
            ResearchSession.guild_id,
            ResearchSession.channel_id,
            ResearchSession.model_name,
            _tokens(ResearchSession.input_tokens).label("input_tokens"),
            _tokens(ResearchSession.output_tokens).label("output_tokens"),
            _cost(ResearchSession.cost_usd).label("cost_usd"),
        )
        .where(ResearchSession.created_at >= start)
        .where(ResearchSession.created_at < end)
        .group_by(
            ResearchSession.guild_id,
            ResearchSession.channel_id,
            ResearchSession.model_name,
        )
    )
    for row in (await db_session.execute(research_stmt)).all():
        lines.append(
            _channel_line(
                guild_id=row.guild_id,
                channel_id=row.channel_id,
                channel_name=None,
                raw_model_name=row.model_name,
                reasoning_level=None,
                source="scan",
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cost_usd=row.cost_usd,
            )
        )

    return sorted(lines, key=lambda line: line.cost_usd, reverse=True)
