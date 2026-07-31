"""Database-backed Chat settings with atomic whole-form validation."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal
from decimal import InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_catalog import parse_reasoning_level
from smarter_dev.shared.model_catalog import resolve_reasoning_level
from smarter_dev.web.chat.entitlements import SPEND_TIERS
from smarter_dev.web.chat.policy import IntelligenceMode
from smarter_dev.web.chat.policy import parse_intelligence_mode
from smarter_dev.web.llm_pricing import price_rates_for_model
from smarter_dev.web.models import ChatCatalogModel
from smarter_dev.web.models import ChatSettings
from smarter_dev.web.models import ChatSpendLimit

DEFAULT_MODEL = "gemini-3-1-flash-lite"
DEFAULT_SUMMARIZER = "poolside-laguna-s-2-1"
COST_TIERS = frozenset({"low", "medium", "high", "ultra"})


@dataclass(frozen=True, slots=True)
class SettingsInput:
    default_model_key: str
    default_reasoning: str | None
    default_intelligence_mode: str
    summarizer_model_key: str
    summarizer_fallback_model_key: str | None
    compaction_model_key: str
    compaction_fallback_model_key: str | None
    catalog: dict[str, tuple[bool, str]]
    limits: dict[str, tuple[Decimal, Decimal, Decimal]]


def validate_settings_input(data: SettingsInput) -> SettingsInput:
    keys = {
        data.default_model_key,
        data.summarizer_model_key,
        data.compaction_model_key,
        data.summarizer_fallback_model_key,
        data.compaction_fallback_model_key,
    } - {None, ""}
    if not data.summarizer_model_key or not data.compaction_model_key:
        raise ValueError("Summarizer and compaction models are required")
    unknown = sorted(key for key in keys if get_model(str(key)) is None)
    if unknown:
        raise ValueError(f"Unknown model key(s): {', '.join(unknown)}")
    parse_intelligence_mode(data.default_intelligence_mode)
    if (
        data.default_model_key not in data.catalog
        or not data.catalog[data.default_model_key][0]
    ):
        raise ValueError("The default Chat model must be enabled")
    auxiliary_keys = [
        data.summarizer_model_key,
        data.summarizer_fallback_model_key,
        data.compaction_model_key,
        data.compaction_fallback_model_key,
    ]
    for key in (value for value in auxiliary_keys if value):
        model = get_model(key)
        if model is None or price_rates_for_model(model) is None:
            raise ValueError(f"Auxiliary model {key} must have configured pricing")
    media_models = [
        get_model(key)
        for key in (
            data.summarizer_model_key,
            data.summarizer_fallback_model_key,
        )
        if key
    ]
    if not any(model and model.supports_vision for model in media_models):
        raise ValueError(
            "The summarizer or its fallback must support vision for image instructions"
        )
    for key, (enabled, tier) in data.catalog.items():
        catalog_model = get_model(key)
        if catalog_model is None:
            raise ValueError(f"Unknown catalog model: {key}")
        if tier not in COST_TIERS:
            raise ValueError(f"Invalid cost tier for {key}: {tier}")
        if enabled and price_rates_for_model(catalog_model) is None:
            raise ValueError(f"Cannot enable {key}: provider pricing is unavailable")
    if set(data.limits) != set(SPEND_TIERS):
        raise ValueError("Spend limits are required for every tier")
    max_limit = Decimal("999999.999999")
    for tier, values in data.limits.items():
        if any(Decimal(value) < 0 for value in values):
            raise ValueError(f"Spend limits for {tier} cannot be negative")
        if any(Decimal(value) > max_limit for value in values):
            raise ValueError(f"Spend limits for {tier} exceed storage precision")
    model = get_model(data.default_model_key)
    requested = parse_reasoning_level(data.default_reasoning)
    effective = resolve_reasoning_level(model, requested) if model else None
    return replace(data, default_reasoning=effective.value if effective else None)


async def ensure_settings(session: AsyncSession) -> ChatSettings:
    settings = await session.get(ChatSettings, 1)
    if settings is None:
        settings = ChatSettings(
            id=1,
            default_model_key=DEFAULT_MODEL,
            default_reasoning="medium",
            default_intelligence_mode=IntelligenceMode.EFFICIENT.value,
            summarizer_model_key=DEFAULT_SUMMARIZER,
            summarizer_fallback_model_key=DEFAULT_MODEL,
            compaction_model_key=DEFAULT_MODEL,
            compaction_fallback_model_key=None,
        )
        session.add(settings)
    existing = set(
        (await session.execute(select(ChatCatalogModel.model_key))).scalars()
    )
    for order, model in enumerate(MODEL_CATALOG):
        if model.key not in existing:
            session.add(
                ChatCatalogModel(
                    model_key=model.key,
                    enabled=model.key == DEFAULT_MODEL,
                    cost_tier="low"
                    if model.key in {DEFAULT_MODEL, DEFAULT_SUMMARIZER}
                    else "medium",
                    sort_order=order,
                )
            )
    defaults = {
        "hacker": ("1", "2", "8"),
        "r": ("2", "4", "16"),
        "rw": ("4", "8", "32"),
        "rwx": ("8", "16", "64"),
    }
    present = set((await session.execute(select(ChatSpendLimit.tier))).scalars())
    for tier, values in defaults.items():
        if tier not in present:
            session.add(
                ChatSpendLimit(
                    tier=tier,
                    four_hour_usd=Decimal(values[0]),
                    daily_usd=Decimal(values[1]),
                    weekly_usd=Decimal(values[2]),
                )
            )
    await session.commit()
    await session.refresh(settings)
    return settings


def decimal_field(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result
