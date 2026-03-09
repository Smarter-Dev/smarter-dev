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

from smarter_dev.web.models import ResearchSession

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
    ) -> TemplateResponse:
        """Usage dashboard with individual runs, per-user aggregation, and cost chart."""
        ctx = await get_admin_context(request, db_session)
        filters = _build_filters(days, pipeline_mode)

        if granularity not in _BUCKET_EXPR:
            granularity = "day"

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
                **ctx,
            },
        )
