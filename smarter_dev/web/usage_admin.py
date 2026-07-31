"""Token usage tracking controller for the Skrift admin panel."""

from __future__ import annotations

import json
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

from litestar import Controller
from litestar import Request
from litestar import get
from litestar.response import Template as TemplateResponse
from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.db.models.oauth_account import OAuthAccount
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.web.models import ChatSpendReservation
from smarter_dev.web.models import ResearchSession
from smarter_dev.web.models import ScanServiceUsage
from smarter_dev.web.models import UsageCostRow
from smarter_dev.web.models import WebChatConversation
from smarter_dev.web.models import WebChatSubagent
from smarter_dev.web.models import WebChatTurn
from smarter_dev.web.usage_invoice import ChannelUsageLine
from smarter_dev.web.usage_invoice import InvoiceLine
from smarter_dev.web.usage_invoice import available_months
from smarter_dev.web.usage_invoice import channel_breakdown
from smarter_dev.web.usage_invoice import monthly_invoice

# Bucket expressions keyed by granularity name.
# Each returns a string label suitable for Chart.js x-axis.
_BUCKET_EXPR = {
    "hour": func.to_char(
        func.date_trunc("hour", ResearchSession.created_at), "YYYY-MM-DD HH24:00"
    ),
    "day": func.to_char(
        func.date_trunc("day", ResearchSession.created_at), "YYYY-MM-DD"
    ),
    "week": func.to_char(
        func.date_trunc("week", ResearchSession.created_at), "YYYY-MM-DD"
    ),
    "month": func.to_char(
        func.date_trunc("month", ResearchSession.created_at), "YYYY-MM"
    ),
}


def _build_filters(days: int | None, pipeline_mode: str | None) -> list:
    filters = []
    if days:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        filters.append(ResearchSession.created_at >= cutoff)
    if pipeline_mode:
        filters.append(ResearchSession.pipeline_mode == pipeline_mode)
    return filters


_SOURCE_LABELS = {
    "chat": "Chat",
    "voice": "Voice",
    "compaction": "Compaction",
    "scan": "Scan research",
    "scan_service": "Scan services",
}

_VALID_TABS = {"runs", "users", "service", "invoice", "channels", "products"}


def _reasoning_display(reasoning_level: str | None) -> str:
    if reasoning_level is None:
        return "—"
    try:
        return ReasoningLevel(reasoning_level).label
    except ValueError:
        return reasoning_level


def _zero_token_totals() -> dict:
    return {
        "cost": Decimal("0"),
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


def build_invoice_tree(lines: list[InvoiceLine]) -> tuple[list[dict], dict]:
    """Group flat invoice lines into provider → model → reasoning rows.

    Returns (providers, grand_totals) where each provider carries its own
    subtotals and a cost-descending list of models, each model carrying its
    subtotals and cost-descending reasoning/source rows.
    """
    providers_by_key: dict[str, dict] = {}
    grand_totals = _zero_token_totals()

    for line in lines:
        provider = providers_by_key.setdefault(
            line.provider_key,
            {
                "key": line.provider_key,
                "label": line.provider_label,
                **_zero_token_totals(),
                "models": {},
            },
        )
        model = provider["models"].setdefault(
            line.model_name,
            {
                "name": line.model_name,
                **_zero_token_totals(),
                "rows": [],
            },
        )
        model["rows"].append(
            {
                "reasoning": _reasoning_display(line.reasoning_level),
                "source": _SOURCE_LABELS.get(line.source, line.source),
                "input_tokens": line.input_tokens,
                "output_tokens": line.output_tokens,
                "cache_read_tokens": line.cache_read_tokens,
                "cache_write_tokens": line.cache_write_tokens,
                "cost": line.cost_usd,
            }
        )
        for bucket in (model, provider, grand_totals):
            bucket["cost"] += line.cost_usd
            bucket["input_tokens"] += line.input_tokens
            bucket["output_tokens"] += line.output_tokens
            bucket["cache_read_tokens"] += line.cache_read_tokens
            bucket["cache_write_tokens"] += line.cache_write_tokens

    providers = sorted(providers_by_key.values(), key=lambda p: p["cost"], reverse=True)
    for provider in providers:
        provider["models"] = sorted(
            provider["models"].values(), key=lambda m: m["cost"], reverse=True
        )
        for model in provider["models"]:
            model["rows"].sort(key=lambda r: r["cost"], reverse=True)
    return providers, grand_totals


def build_channel_tree(lines: list[ChannelUsageLine]) -> list[dict]:
    """Group flat channel usage lines into per-channel groups with subtotals."""
    channels_by_key: dict[tuple, dict] = {}

    for line in lines:
        channel = channels_by_key.setdefault(
            (line.guild_id, line.channel_id),
            {
                "guild_id": line.guild_id,
                "channel_id": line.channel_id,
                "display_name": None,
                "cost": Decimal("0"),
                "input_tokens": 0,
                "output_tokens": 0,
                "rows": [],
            },
        )
        if line.channel_name and not channel["display_name"]:
            channel["display_name"] = f"#{line.channel_name}"
        channel["rows"].append(
            {
                "provider_label": line.provider_label,
                "model_name": line.model_name,
                "reasoning": _reasoning_display(line.reasoning_level),
                "source": _SOURCE_LABELS.get(line.source, line.source),
                "input_tokens": line.input_tokens,
                "output_tokens": line.output_tokens,
                "cost": line.cost_usd,
            }
        )
        channel["cost"] += line.cost_usd
        channel["input_tokens"] += line.input_tokens
        channel["output_tokens"] += line.output_tokens

    for channel in channels_by_key.values():
        if not channel["display_name"]:
            channel["display_name"] = channel["channel_id"] or "unknown channel"
        channel["rows"].sort(key=lambda r: r["cost"], reverse=True)
    return sorted(channels_by_key.values(), key=lambda c: c["cost"], reverse=True)


class _DecimalEncoder(json.JSONEncoder):
    def default(self, o: object) -> object:
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


class UsageAdminController(Controller):
    """Token usage tracking in the Skrift admin panel."""

    path = "/admin"
    guards = [auth_guard]

    @get(
        "/usage",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Usage", "icon": "bar-chart-2", "order": 35},
    )
    async def usage_overview(
        self,
        request: Request,
        db_session: AsyncSession,
        days: int | None = 30,
        pipeline_mode: str | None = None,
        granularity: str | None = "day",
        month: str | None = None,
        tab: str | None = None,
    ) -> TemplateResponse:
        """Usage dashboard with individual runs, per-user aggregation, and cost chart."""
        ctx = await get_admin_context(request, db_session)
        filters = _build_filters(days, pipeline_mode)

        if granularity not in _BUCKET_EXPR:
            granularity = "day"
        active_tab = tab if tab in _VALID_TABS else "runs"

        # ------------------------------------------------------------------
        # Monthly invoice + per-channel breakdown
        # ------------------------------------------------------------------
        months = await available_months(db_session)
        selected_month = month if month in months else (months[0] if months else None)
        if selected_month:
            invoice_lines = await monthly_invoice(db_session, selected_month)
            channel_lines = await channel_breakdown(db_session, selected_month)
        else:
            invoice_lines = []
            channel_lines = []
        invoice_providers, invoice_totals = build_invoice_tree(invoice_lines)
        channels = build_channel_tree(channel_lines)

        # ------------------------------------------------------------------
        # Tab 1: Individual runs (most recent 200)
        # ------------------------------------------------------------------
        runs_stmt = (
            select(
                ResearchSession.id,
                ResearchSession.user_id,
                ResearchSession.query,
                ResearchSession.pipeline_mode,
                ResearchSession.status,
                ResearchSession.input_tokens,
                ResearchSession.output_tokens,
                ResearchSession.cache_read_tokens,
                ResearchSession.cache_write_tokens,
                ResearchSession.model_name,
                ResearchSession.cost_usd,
                ResearchSession.created_at,
            )
            .where(*filters)
            .order_by(ResearchSession.created_at.desc())
            .limit(200)
        )
        runs = (await db_session.execute(runs_stmt)).all()

        # ------------------------------------------------------------------
        # Tab 2: Per-user aggregation
        # ------------------------------------------------------------------
        agg_stmt = (
            select(
                ResearchSession.user_id,
                ResearchSession.pipeline_mode,
                func.count().label("session_count"),
                func.sum(ResearchSession.input_tokens).label("total_input"),
                func.sum(ResearchSession.output_tokens).label("total_output"),
                func.sum(ResearchSession.cost_usd).label("total_cost"),
            )
            .where(*filters)
            .group_by(ResearchSession.user_id, ResearchSession.pipeline_mode)
            .order_by(func.sum(ResearchSession.cost_usd).desc().nulls_last())
        )
        agg_rows = (await db_session.execute(agg_stmt)).all()

        # ------------------------------------------------------------------
        # Chart: cost over time by product
        # ------------------------------------------------------------------
        bucket = _BUCKET_EXPR[granularity]
        chart_stmt = (
            select(
                bucket.label("bucket"),
                ResearchSession.pipeline_mode,
                func.coalesce(func.sum(ResearchSession.cost_usd), 0).label("cost"),
            )
            .where(*filters)
            .group_by(bucket, ResearchSession.pipeline_mode)
            .order_by(bucket)
        )
        chart_rows = (await db_session.execute(chart_stmt)).all()

        # Pivot into {label: [cost_lite, cost_premium, ...]} for Chart.js
        labels_ordered: list[str] = []
        products: dict[str, dict[str, float]] = {}
        for row in chart_rows:
            lbl = row.bucket
            if lbl not in labels_ordered:
                labels_ordered.append(lbl)
            products.setdefault(row.pipeline_mode, {})[lbl] = float(row.cost or 0)

        chart_datasets: list[dict] = []
        palette = {"lite": "#3b82f6", "premium": "#f59e0b"}
        nice_name = {"lite": "Lite Research", "premium": "Advanced Research"}
        for product, costs_by_label in sorted(products.items()):
            chart_datasets.append(
                {
                    "label": nice_name.get(product, product),
                    "data": [costs_by_label.get(lbl, 0) for lbl in labels_ordered],
                    "borderColor": palette.get(product, "#8b5cf6"),
                    "backgroundColor": palette.get(product, "#8b5cf6") + "33",
                    "fill": True,
                    "tension": 0.3,
                }
            )

        # ------------------------------------------------------------------
        # Canonical cross-product usage ledger (web Chat, Resources, Discord)
        # ------------------------------------------------------------------
        ledger_filters = []
        if days:
            ledger_filters.append(
                UsageCostRow.metered_at >= datetime.now(UTC) - timedelta(days=days)
            )
        product_rows = (
            await db_session.execute(
                select(
                    UsageCostRow.product_mode,
                    UsageCostRow.operation_type,
                    UsageCostRow.user_id,
                    UsageCostRow.discord_user_id,
                    UsageCostRow.intelligence_mode,
                    UsageCostRow.provider_key,
                    UsageCostRow.model_id,
                    UsageCostRow.reasoning_level,
                    UsageCostRow.conversation_id,
                    UsageCostRow.root_turn_id,
                    UsageCostRow.subagent_id,
                    func.count().label("operations"),
                    func.sum(UsageCostRow.input_tokens).label("input_tokens"),
                    func.sum(UsageCostRow.output_tokens).label("output_tokens"),
                    func.sum(UsageCostRow.cost_usd).label("cost_usd"),
                    func.sum(UsageCostRow.overage_cost_usd).label("overage_cost_usd"),
                )
                .where(*ledger_filters)
                .group_by(
                    UsageCostRow.product_mode,
                    UsageCostRow.operation_type,
                    UsageCostRow.user_id,
                    UsageCostRow.discord_user_id,
                    UsageCostRow.intelligence_mode,
                    UsageCostRow.provider_key,
                    UsageCostRow.model_id,
                    UsageCostRow.reasoning_level,
                    UsageCostRow.conversation_id,
                    UsageCostRow.root_turn_id,
                    UsageCostRow.subagent_id,
                )
                .order_by(func.sum(UsageCostRow.cost_usd).desc())
            )
        ).all()
        subagent_rows = (
            await db_session.execute(
                select(
                    WebChatSubagent.id,
                    WebChatSubagent.name,
                    WebChatSubagent.lineage,
                    WebChatSubagent.spawn_ordinal,
                    WebChatSubagent.status,
                    WebChatSubagent.reasoning_level,
                    WebChatSubagent.input_tokens,
                    WebChatSubagent.output_tokens,
                    WebChatSubagent.cost_usd,
                    WebChatTurn.id.label("root_turn_id"),
                    WebChatConversation.id.label("conversation_id"),
                    WebChatConversation.owner_user_id,
                    WebChatConversation.intelligence_mode,
                )
                .join(WebChatTurn, WebChatTurn.id == WebChatSubagent.root_turn_id)
                .join(
                    WebChatConversation,
                    WebChatConversation.id == WebChatTurn.conversation_id,
                )
                .order_by(WebChatSubagent.queued_at.desc())
                .limit(500)
            )
        ).all()
        pending_chat_reservations = (
            await db_session.execute(
                select(
                    func.count(ChatSpendReservation.id),
                    func.coalesce(func.sum(ChatSpendReservation.reserved_usd), 0),
                ).where(
                    ChatSpendReservation.status == "active",
                    ChatSpendReservation.expires_at > datetime.now(UTC),
                )
            )
        ).one()
        ledger_totals = (
            await db_session.execute(
                select(
                    func.count().label("operations"),
                    func.coalesce(func.sum(UsageCostRow.input_tokens), 0),
                    func.coalesce(func.sum(UsageCostRow.output_tokens), 0),
                    func.coalesce(func.sum(UsageCostRow.cost_usd), 0),
                ).where(*ledger_filters)
            )
        ).one()
        ledger_bucket = func.to_char(
            func.date_trunc(granularity, UsageCostRow.metered_at),
            "YYYY-MM-DD HH24:00"
            if granularity == "hour"
            else ("YYYY-MM" if granularity == "month" else "YYYY-MM-DD"),
        )
        ledger_chart_rows = (
            await db_session.execute(
                select(
                    ledger_bucket.label("bucket"),
                    UsageCostRow.product_mode,
                    func.coalesce(func.sum(UsageCostRow.cost_usd), 0).label("cost"),
                )
                .where(*ledger_filters)
                .group_by(ledger_bucket, UsageCostRow.product_mode)
                .order_by(ledger_bucket)
            )
        ).all()
        if ledger_chart_rows:
            labels_ordered = []
            products = {}
            for row in ledger_chart_rows:
                if row.bucket not in labels_ordered:
                    labels_ordered.append(row.bucket)
                products.setdefault(row.product_mode, {})[row.bucket] = float(
                    row.cost or 0
                )
            product_palette = {
                "resources": "#3b82f6",
                "chat": "#8b5cf6",
                "discord": "#22c55e",
            }
            chart_datasets = [
                {
                    "label": product.title(),
                    "data": [values.get(label, 0) for label in labels_ordered],
                    "borderColor": product_palette.get(product, "#8b5cf6"),
                    "backgroundColor": product_palette.get(product, "#8b5cf6") + "33",
                    "fill": True,
                    "tension": 0.3,
                }
                for product, values in sorted(products.items())
            ]

        discord_ids = {
            row.discord_user_id for row in product_rows if row.discord_user_id
        }
        discord_links = {}
        if discord_ids:
            discord_links = dict(
                (
                    await db_session.execute(
                        select(
                            OAuthAccount.provider_account_id,
                            OAuthAccount.user_id,
                        ).where(
                            OAuthAccount.provider == "discord",
                            OAuthAccount.provider_account_id.in_(discord_ids),
                        )
                    )
                ).all()
            )

        # ------------------------------------------------------------------
        # Grand totals
        # ------------------------------------------------------------------
        total_sessions = int(ledger_totals[0] or 0)
        total_input = int(ledger_totals[1] or 0)
        total_output = int(ledger_totals[2] or 0)
        total_cost = Decimal(ledger_totals[3] or 0)

        # ------------------------------------------------------------------
        # Service usage (profiler, etc.) — internal costs
        # ------------------------------------------------------------------
        svc_filters: list = []
        if days:
            cutoff = datetime.now(UTC) - timedelta(days=days)
            svc_filters.append(ScanServiceUsage.created_at >= cutoff)

        svc_runs_stmt = (
            select(ScanServiceUsage)
            .where(*svc_filters)
            .order_by(ScanServiceUsage.created_at.desc())
            .limit(200)
        )
        svc_runs = list((await db_session.execute(svc_runs_stmt)).scalars().all())

        svc_agg_stmt = (
            select(
                ScanServiceUsage.task_type,
                func.count().label("invocations"),
                func.sum(ScanServiceUsage.input_tokens).label("total_input"),
                func.sum(ScanServiceUsage.output_tokens).label("total_output"),
                func.sum(ScanServiceUsage.cost_usd).label("total_cost"),
            )
            .where(*svc_filters)
            .group_by(ScanServiceUsage.task_type)
            .order_by(func.sum(ScanServiceUsage.cost_usd).desc().nulls_last())
        )
        svc_agg_rows = (await db_session.execute(svc_agg_stmt)).all()

        svc_total_cost = sum(r.total_cost or 0 for r in svc_agg_rows)
        svc_total_invocations = sum(r.invocations for r in svc_agg_rows)

        return TemplateResponse(
            "admin/usage.html",
            context={
                "runs": runs,
                "agg_rows": agg_rows,
                "total_sessions": total_sessions,
                "total_input": total_input,
                "total_output": total_output,
                "total_cost": total_cost,
                "days": days,
                "pipeline_mode": pipeline_mode or "",
                "granularity": granularity,
                "chart_labels": json.dumps(labels_ordered),
                "chart_datasets": json.dumps(chart_datasets, cls=_DecimalEncoder),
                "svc_runs": svc_runs,
                "svc_agg_rows": svc_agg_rows,
                "svc_total_cost": svc_total_cost,
                "svc_total_invocations": svc_total_invocations,
                "active_tab": active_tab,
                "months": months,
                "selected_month": selected_month,
                "invoice_providers": invoice_providers,
                "invoice_totals": invoice_totals,
                "channels": channels,
                "product_rows": product_rows,
                "subagent_rows": subagent_rows,
                "pending_chat_reservation_count": int(
                    pending_chat_reservations[0] or 0
                ),
                "pending_chat_reservation_cost": Decimal(
                    pending_chat_reservations[1] or 0
                ),
                "discord_links": discord_links,
                **ctx,
            },
        )
