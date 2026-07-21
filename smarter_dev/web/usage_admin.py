"""Token usage tracking controller for the Skrift admin panel."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from litestar import Controller, Request, get
from litestar.response import Template as TemplateResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission, auth_guard

from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.web.models import ResearchSession, ScanServiceUsage
from smarter_dev.web.usage_invoice import (
    ChannelUsageLine,
    InvoiceLine,
    available_months,
    channel_breakdown,
    monthly_invoice,
)

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


def _build_filters(
    days: int | None, pipeline_mode: str | None
) -> list:
    filters = []
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
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

_VALID_TABS = {"runs", "users", "service", "invoice", "channels"}


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
        provider = providers_by_key.setdefault(line.provider_key, {
            "key": line.provider_key,
            "label": line.provider_label,
            **_zero_token_totals(),
            "models": {},
        })
        model = provider["models"].setdefault(line.model_name, {
            "name": line.model_name,
            **_zero_token_totals(),
            "rows": [],
        })
        model["rows"].append({
            "reasoning": _reasoning_display(line.reasoning_level),
            "source": _SOURCE_LABELS.get(line.source, line.source),
            "input_tokens": line.input_tokens,
            "output_tokens": line.output_tokens,
            "cache_read_tokens": line.cache_read_tokens,
            "cache_write_tokens": line.cache_write_tokens,
            "cost": line.cost_usd,
        })
        for bucket in (model, provider, grand_totals):
            bucket["cost"] += line.cost_usd
            bucket["input_tokens"] += line.input_tokens
            bucket["output_tokens"] += line.output_tokens
            bucket["cache_read_tokens"] += line.cache_read_tokens
            bucket["cache_write_tokens"] += line.cache_write_tokens

    providers = sorted(
        providers_by_key.values(), key=lambda p: p["cost"], reverse=True
    )
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
        channel = channels_by_key.setdefault((line.guild_id, line.channel_id), {
            "guild_id": line.guild_id,
            "channel_id": line.channel_id,
            "display_name": None,
            "cost": Decimal("0"),
            "input_tokens": 0,
            "output_tokens": 0,
            "rows": [],
        })
        if line.channel_name and not channel["display_name"]:
            channel["display_name"] = f"#{line.channel_name}"
        channel["rows"].append({
            "provider_label": line.provider_label,
            "model_name": line.model_name,
            "reasoning": _reasoning_display(line.reasoning_level),
            "source": _SOURCE_LABELS.get(line.source, line.source),
            "input_tokens": line.input_tokens,
            "output_tokens": line.output_tokens,
            "cost": line.cost_usd,
        })
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
        days: Optional[int] = 30,
        pipeline_mode: Optional[str] = None,
        granularity: Optional[str] = "day",
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
            chart_datasets.append({
                "label": nice_name.get(product, product),
                "data": [costs_by_label.get(lbl, 0) for lbl in labels_ordered],
                "borderColor": palette.get(product, "#8b5cf6"),
                "backgroundColor": palette.get(product, "#8b5cf6") + "33",
                "fill": True,
                "tension": 0.3,
            })

        # ------------------------------------------------------------------
        # Grand totals
        # ------------------------------------------------------------------
        total_sessions = sum(r.session_count for r in agg_rows)
        total_input = sum(r.total_input or 0 for r in agg_rows)
        total_output = sum(r.total_output or 0 for r in agg_rows)
        total_cost = sum(r.total_cost or 0 for r in agg_rows)

        # ------------------------------------------------------------------
        # Service usage (profiler, etc.) — internal costs
        # ------------------------------------------------------------------
        svc_filters: list = []
        if days:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
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
                **ctx,
            },
        )
