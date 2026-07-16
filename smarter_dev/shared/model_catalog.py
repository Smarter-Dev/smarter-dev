"""Single source of truth for the selectable LLM models.

The admin per-channel model-override command (and its web-API validation and
enforcement) all resolve models through this catalog. Each :class:`CatalogModel`
pairs a stable ``key`` (persisted in the DB and embedded in Discord custom_ids)
with the ``model_id`` wire string handed to the provider SDK. Keys are stable;
``model_id`` values are the wire ids and may be re-verified/updated without a
migration.

Each model also declares which :class:`ReasoningLevel` values it supports and a
sensible default. Providers disagree on the ladder they expose (OpenAI runs
``none``..``max``; Gemini's ``thinking_level`` tops out at ``high``; several open
models offer only ``low``/``medium``/``high``; a few have no reasoning knob at
all), so the admin modal offers one superset select and
:func:`resolve_reasoning_level` maps whatever is chosen onto the selected model,
clamping to the nearest supported level rather than failing.

Provider routing lives in :mod:`smarter_dev.bot.agents.model_router`; this module
is pure data + lookup helpers.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ModelProvider(enum.Enum):
    """Where a catalog model is served from."""

    GOOGLE = "google"
    OPENAI = "openai"
    DIGITALOCEAN = "digitalocean"


class ReasoningLevel(enum.Enum):
    """A reasoning/thinking effort level, ordered least → most from top to bottom.

    Definition order *is* the ladder: members earlier in the class reason less.
    :func:`resolve_reasoning_level` relies on this ordering to clamp an
    unsupported choice to the nearest level a given model actually offers. The
    ``value`` strings are the wire tokens providers expect (OpenAI
    ``reasoning_effort``, Gemini ``thinking_level``).
    """

    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"

    @property
    def label(self) -> str:
        """Human label shown in the Discord reasoning select."""
        return _REASONING_LABELS[self]


_REASONING_LABELS: dict[ReasoningLevel, str] = {
    ReasoningLevel.NONE: "None (no reasoning)",
    ReasoningLevel.MINIMAL: "Minimal",
    ReasoningLevel.LOW: "Low",
    ReasoningLevel.MEDIUM: "Medium",
    ReasoningLevel.HIGH: "High",
    ReasoningLevel.XHIGH: "Extra high",
    ReasoningLevel.MAX: "Max",
}

# Full ladder in ascending effort order — the superset the admin select offers.
ALL_REASONING_LEVELS: tuple[ReasoningLevel, ...] = tuple(ReasoningLevel)


@dataclass(frozen=True)
class CatalogModel:
    """One selectable model.

    Attributes:
        key: Stable slug persisted in the DB and embedded in Discord custom_ids
            (e.g. ``"kimi-k2-6"``). Never change once shipped.
        label: Human label shown in the Discord select (e.g. ``"Kimi K2.6"``).
        family: One of the seven supported families.
        provider: Which provider SDK serves this model.
        model_id: Exact id passed to the provider SDK / sent as the ``model``
            field on the wire.
        reasoning_levels: The reasoning levels this model supports, ascending.
            Empty means the model has no reasoning knob (e.g. Gemma, Kimi K2).
        default_reasoning: The level applied when the channel override does not
            pin one; ``None`` for models with no reasoning knob.
    """

    key: str
    label: str
    family: str
    provider: ModelProvider
    model_id: str
    reasoning_levels: tuple[ReasoningLevel, ...] = ()
    default_reasoning: ReasoningLevel | None = None

    def __post_init__(self) -> None:
        if self.default_reasoning is not None and (
            self.default_reasoning not in self.reasoning_levels
        ):
            raise ValueError(
                f"{self.key}: default_reasoning {self.default_reasoning} is not "
                f"one of reasoning_levels {self.reasoning_levels}"
            )
        if not self.reasoning_levels and self.default_reasoning is not None:
            raise ValueError(
                f"{self.key}: default_reasoning set but no reasoning_levels"
            )

    @property
    def supports_reasoning(self) -> bool:
        """Whether this model exposes a reasoning knob at all."""
        return bool(self.reasoning_levels)


# The seven model families the admin override exposes.
MODEL_FAMILIES: tuple[str, ...] = (
    "Kimi",
    "GLM",
    "DeepSeek",
    "Gemma",
    "Qwen",
    "Gemini",
    "GPT",
)


# Common reasoning ladders, named once so the catalog stays scannable.
# OpenAI GPT-5.4/5.5: none → xhigh. GPT-5.6 adds "max". Gemini's thinking_level
# caps at "high". Open reasoning models (GLM/DeepSeek/Qwen served via DO) expose
# a low/medium/high effort knob through the OpenAI-compatible API.
_OPENAI_5X = (
    ReasoningLevel.NONE,
    ReasoningLevel.LOW,
    ReasoningLevel.MEDIUM,
    ReasoningLevel.HIGH,
    ReasoningLevel.XHIGH,
)
_OPENAI_56 = _OPENAI_5X + (ReasoningLevel.MAX,)
_GEMINI_THINKING = (
    ReasoningLevel.MINIMAL,
    ReasoningLevel.LOW,
    ReasoningLevel.MEDIUM,
    ReasoningLevel.HIGH,
)
_OPEN_EFFORT = (ReasoningLevel.LOW, ReasoningLevel.MEDIUM, ReasoningLevel.HIGH)


# Curated catalog. Kept <= 24 entries so the whole set fits in one Discord
# string-select (25-option limit, leaving room for a "server default" sentinel).
# Gemini -> Google, GPT -> OpenAI, everything else -> Digital Ocean's
# OpenAI-compatible serverless inference. Model ids reflect the latest releases
# as of mid-2026 (verified against provider/DO model listings); they are wire
# ids and can be re-verified without a migration.
MODEL_CATALOG: tuple[CatalogModel, ...] = (
    # --- Open weights via Digital Ocean serverless inference ---
    # DO uses flat model ids (verified against GET /v1/models on the live
    # account), not vendor-prefixed paths — an unknown id 403s.
    CatalogModel(
        key="kimi-k2-6",
        label="Kimi K2.6 (Moonshot)",
        family="Kimi",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="kimi-k2.6",
    ),
    CatalogModel(
        key="glm-5-2",
        label="GLM-5.2 (Zhipu)",
        family="GLM",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="glm-5.2",
        reasoning_levels=_OPEN_EFFORT,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="deepseek-v4",
        label="DeepSeek V4 Flash",
        family="DeepSeek",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="deepseek-4-flash",
        reasoning_levels=_OPEN_EFFORT,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gemma-4-31b",
        label="Gemma 4 31B",
        family="Gemma",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="gemma-4-31B-it",
    ),
    CatalogModel(
        key="qwen3-5-397b",
        label="Qwen3.5 397B",
        family="Qwen",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="qwen3.5-397b-a17b",
        reasoning_levels=_OPEN_EFFORT,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    # --- Gemini via Google ---
    CatalogModel(
        key="gemini-3-flash",
        label="Gemini 3 Flash",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3-flash-preview",
        reasoning_levels=_GEMINI_THINKING,
        default_reasoning=ReasoningLevel.HIGH,
    ),
    CatalogModel(
        key="gemini-3-1-flash-lite",
        label="Gemini 3.1 Flash Lite",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3.1-flash-lite",
        reasoning_levels=_GEMINI_THINKING,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gemini-3-1-pro",
        label="Gemini 3.1 Pro",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3.1-pro",
        reasoning_levels=_GEMINI_THINKING,
        default_reasoning=ReasoningLevel.HIGH,
    ),
    CatalogModel(
        key="gemini-3-5-flash",
        label="Gemini 3.5 Flash",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3.5-flash",
        reasoning_levels=_GEMINI_THINKING,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    # --- GPT via OpenAI ---
    CatalogModel(
        key="gpt-5-4-nano",
        label="GPT-5.4 Nano",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.4-nano",
        reasoning_levels=_OPENAI_5X,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-4-mini",
        label="GPT-5.4 Mini",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.4-mini",
        reasoning_levels=_OPENAI_5X,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-4",
        label="GPT-5.4",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.4",
        reasoning_levels=_OPENAI_5X,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-5",
        label="GPT-5.5",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.5",
        reasoning_levels=_OPENAI_5X,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-6-luna",
        label="GPT-5.6 Luna",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.6-luna",
        reasoning_levels=_OPENAI_56,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-6-terra",
        label="GPT-5.6 Terra",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.6-terra",
        reasoning_levels=_OPENAI_56,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
)


# Built once at import so the hot-path lookups below never rebuild it. The
# catalog is immutable, so a single shared mapping is safe to reuse.
_MODEL_BY_KEY: dict[str, CatalogModel] = {
    model.key: model for model in MODEL_CATALOG
}


def catalog_by_key() -> dict[str, CatalogModel]:
    """Return the shared ``key -> CatalogModel`` mapping."""
    return _MODEL_BY_KEY


def get_model(key: str) -> CatalogModel | None:
    """Return the catalog model for ``key``, or ``None`` if unknown."""
    return _MODEL_BY_KEY.get(key)


def is_valid_model_key(key: str) -> bool:
    """Return whether ``key`` names a catalog model."""
    return key in _MODEL_BY_KEY


def models_by_family() -> dict[str, list[CatalogModel]]:
    """Group the catalog by family, preserving catalog order within each family.

    Only families that have at least one model appear. Family insertion order
    follows first appearance in :data:`MODEL_CATALOG`.
    """
    grouped: dict[str, list[CatalogModel]] = {}
    for model in MODEL_CATALOG:
        grouped.setdefault(model.family, []).append(model)
    return grouped


def parse_reasoning_level(value: str | None) -> ReasoningLevel | None:
    """Parse a stored/select reasoning string into a :class:`ReasoningLevel`.

    ``None``/empty means "no explicit choice" -> ``None`` (use the model default).
    An unrecognised string also degrades to ``None`` rather than raising, so a
    stale persisted value never breaks a chat turn.
    """
    if not value:
        return None
    try:
        return ReasoningLevel(value)
    except ValueError:
        return None


def resolve_reasoning_level(
    model: CatalogModel, requested: ReasoningLevel | None
) -> ReasoningLevel | None:
    """Map a requested reasoning level onto what ``model`` actually supports.

    - A model with no reasoning knob always resolves to ``None`` (ignored).
    - ``requested is None`` falls back to the model's ``default_reasoning``.
    - A supported ``requested`` is returned unchanged.
    - Otherwise the choice is clamped to the nearest supported level (ties break
      toward the lower/cheaper level), so an invalid pick degrades gracefully
      instead of erroring.
    """
    if not model.reasoning_levels:
        return None
    if requested is None:
        return model.default_reasoning
    if requested in model.reasoning_levels:
        return requested
    ladder = list(ReasoningLevel)
    requested_rank = ladder.index(requested)
    return min(
        model.reasoning_levels,
        key=lambda level: (abs(ladder.index(level) - requested_rank), ladder.index(level)),
    )
