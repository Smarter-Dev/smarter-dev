"""Administrator UI for model availability, defaults, and Chat spend tiers."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from litestar import Controller
from litestar import Request
from litestar import get
from litestar import post
from litestar.response import Redirect
from litestar.response import Template
from skrift.admin.helpers import get_admin_context
from skrift.admin.navigation import ADMIN_NAV_TAG
from skrift.auth.guards import Permission
from skrift.auth.guards import auth_guard
from skrift.auth.session_keys import SESSION_USER_ID
from skrift.flash import flash_error
from skrift.flash import flash_success
from skrift.flash import flash_warning
from skrift.flash import get_flash_messages
from skrift.forms.core import verify_csrf
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.model_catalog import ALL_REASONING_LEVELS
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.web.chat.entitlements import SPEND_TIERS
from smarter_dev.web.chat.policy import IntelligenceMode
from smarter_dev.web.chat.settings import SettingsInput
from smarter_dev.web.chat.settings import decimal_field
from smarter_dev.web.chat.settings import ensure_settings
from smarter_dev.web.chat.settings import integer_field
from smarter_dev.web.chat.settings import validate_settings_input
from smarter_dev.web.llm_pricing import price_rates_for_model
from smarter_dev.web.models import ChatCatalogModel
from smarter_dev.web.models import ChatSpendLimit


def filter_unpriced_catalog_selections(
    catalog: dict[str, tuple[bool, str]],
) -> tuple[dict[str, tuple[bool, str]], list[str]]:
    """Keep valid settings saveable while refusing unpriced model admission."""
    filtered = dict(catalog)
    skipped: list[str] = []
    for model in MODEL_CATALOG:
        selection = filtered.get(model.key)
        if selection is None:
            continue
        enabled, tier = selection
        if enabled and price_rates_for_model(model) is None:
            filtered[model.key] = (False, tier)
            skipped.append(model.key)
    return filtered, skipped


class ChatSettingsAdminController(Controller):
    path = "/admin"
    guards = [auth_guard]

    @get(
        "/chat-settings",
        tags=[ADMIN_NAV_TAG],
        guards=[auth_guard, Permission("administrator")],
        opt={"label": "Chat Settings", "icon": "message-square", "order": 36},
    )
    async def show(self, request: Request, db_session: AsyncSession) -> Template:
        settings = await ensure_settings(db_session)
        configured = {
            row.model_key: row
            for row in (
                await db_session.execute(
                    select(ChatCatalogModel).order_by(ChatCatalogModel.sort_order)
                )
            ).scalars()
        }
        limits = {
            row.tier: row
            for row in (await db_session.execute(select(ChatSpendLimit))).scalars()
        }
        return Template(
            "admin/chat_settings.html",
            context={
                "settings": settings,
                "models": [(model, configured[model.key]) for model in MODEL_CATALOG],
                "pricing_available": {
                    model.key: price_rates_for_model(model) is not None
                    for model in MODEL_CATALOG
                },
                "limits": limits,
                "spend_tiers": SPEND_TIERS,
                "reasoning_levels": ALL_REASONING_LEVELS,
                "intelligence_modes": IntelligenceMode,
                "flash_messages": get_flash_messages(request),
                **await get_admin_context(request, db_session),
            },
        )

    @post("/chat-settings", guards=[auth_guard, Permission("administrator")])
    async def update(self, request: Request, db_session: AsyncSession) -> Redirect:
        if not await verify_csrf(request):
            flash_error(request, "Your session expired. Please try again.")
            return Redirect(path="/admin/chat-settings")
        form = await request.form()
        try:
            catalog = {
                model.key: (
                    form.get(f"enabled_{model.key}") == "on",
                    str(form.get(f"cost_tier_{model.key}") or "medium"),
                )
                for model in MODEL_CATALOG
            }
            catalog, skipped_unpriced = filter_unpriced_catalog_selections(catalog)
            limits: dict[str, tuple[Decimal, Decimal, Decimal]] = {}
            for tier in SPEND_TIERS:
                limits[tier] = (
                    decimal_field(
                        form.get(f"limit_{tier}_four_hour"), f"{tier} 4-hour limit"
                    ),
                    decimal_field(
                        form.get(f"limit_{tier}_daily"), f"{tier} daily limit"
                    ),
                    decimal_field(
                        form.get(f"limit_{tier}_weekly"), f"{tier} weekly limit"
                    ),
                )
            parsed = validate_settings_input(
                SettingsInput(
                    default_model_key=str(form.get("default_model_key") or ""),
                    default_reasoning=str(form.get("default_reasoning") or "") or None,
                    default_intelligence_mode=str(
                        form.get("default_intelligence_mode") or ""
                    ),
                    summarizer_model_key=str(form.get("summarizer_model_key") or ""),
                    summarizer_fallback_model_key=str(
                        form.get("summarizer_fallback_model_key") or ""
                    )
                    or None,
                    compaction_model_key=str(form.get("compaction_model_key") or ""),
                    compaction_fallback_model_key=str(
                        form.get("compaction_fallback_model_key") or ""
                    )
                    or None,
                    thread_evaluator_model_key=str(
                        form.get("thread_evaluator_model_key") or ""
                    ),
                    thread_evaluator_fallback_model_key=str(
                        form.get("thread_evaluator_fallback_model_key") or ""
                    )
                    or None,
                    thread_idle_minutes=integer_field(
                        form.get("thread_idle_minutes"), "Thread idle threshold"
                    ),
                    catalog=catalog,
                    limits=limits,
                )
            )
        except (ValueError, TypeError) as exc:
            await db_session.rollback()
            flash_error(request, str(exc))
            return Redirect(path="/admin/chat-settings")

        settings = await ensure_settings(db_session)
        settings.default_model_key = parsed.default_model_key
        settings.default_reasoning = parsed.default_reasoning
        settings.default_intelligence_mode = parsed.default_intelligence_mode
        settings.summarizer_model_key = parsed.summarizer_model_key
        settings.summarizer_fallback_model_key = parsed.summarizer_fallback_model_key
        settings.compaction_model_key = parsed.compaction_model_key
        settings.compaction_fallback_model_key = parsed.compaction_fallback_model_key
        settings.thread_evaluator_model_key = parsed.thread_evaluator_model_key
        settings.thread_evaluator_fallback_model_key = (
            parsed.thread_evaluator_fallback_model_key
        )
        settings.thread_idle_minutes = parsed.thread_idle_minutes
        settings.revision += 1
        raw_actor = request.session.get(SESSION_USER_ID)
        settings.updated_by_user_id = UUID(str(raw_actor)) if raw_actor else None
        model_rows = {
            row.model_key: row
            for row in (await db_session.execute(select(ChatCatalogModel))).scalars()
        }
        for key, (enabled, cost_tier) in parsed.catalog.items():
            model_rows[key].enabled = enabled
            model_rows[key].cost_tier = cost_tier
        limit_rows = {
            row.tier: row
            for row in (await db_session.execute(select(ChatSpendLimit))).scalars()
        }
        for tier, values in parsed.limits.items():
            (
                limit_rows[tier].four_hour_usd,
                limit_rows[tier].daily_usd,
                limit_rows[tier].weekly_usd,
            ) = values
        await db_session.commit()
        flash_success(
            request, "Chat settings saved. New limits apply to the next operation."
        )
        if skipped_unpriced:
            labels = [
                model.label for model in MODEL_CATALOG if model.key in skipped_unpriced
            ]
            flash_warning(
                request,
                "Left unpriced model(s) disabled: " + ", ".join(labels) + ".",
            )
        return Redirect(path="/admin/chat-settings")
