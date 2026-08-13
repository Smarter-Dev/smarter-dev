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
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"
    OPENCODE_ZEN = "opencode_zen"


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
class OpenRouterRouting:
    """Constraints on which OpenRouter endpoints may serve a model.

    OpenRouter fronts one model id with many independent endpoints that differ
    in *precision*, not just price — the same wire id can be served bf16 by one
    provider and fp4 by another, and by default routing sorts by price, which
    means the cheapest (often most aggressively quantized) endpoint wins. These
    fields are passed through as the request's ``provider`` block.

    The house rule these encode: never accept an endpoint quantized below what
    the model's own authors publish, and never silently pay more than the rate
    :mod:`smarter_dev.web.llm_pricing` records for the model.

    Attributes:
        quantizations: Allow-list of precisions. Endpoints that declare
            something else — or declare nothing — are excluded, so this is only
            usable where the authors' own precision is declared. Empty means no
            precision filter (see ``order``/``ignore`` instead).
        order: Endpoint tags to try first, in order. Later entries are
            fallbacks, and routing continues past them unless
            ``allow_fallbacks`` is false.
        ignore: Endpoint tags never to use. Names a *specific* endpoint rather
            than a precision, for the case where an endpoint is distrusted on
            evidence rather than on its declared quantization.
        allow_fallbacks: Whether routing may leave ``order`` when those
            endpoints are unavailable. Kept true: a precision floor plus
            failover is strictly better than pinning one provider, which just
            trades quantization risk for downtime risk.
        max_price_input_mtok: Cost ceiling per million input tokens.
        max_price_output_mtok: Cost ceiling per million output tokens.
    """

    quantizations: tuple[str, ...] = ()
    order: tuple[str, ...] = ()
    ignore: tuple[str, ...] = ()
    allow_fallbacks: bool = True
    max_price_input_mtok: float | None = None
    max_price_output_mtok: float | None = None

    def as_provider_block(self) -> dict:
        """Render the OpenRouter ``provider`` request block.

        Only the constraints actually set are emitted, so a model with no
        opinion sends no block at all rather than a wall of defaults.
        """
        block: dict = {}
        if self.quantizations:
            block["quantizations"] = list(self.quantizations)
        if self.order:
            block["order"] = list(self.order)
        if self.ignore:
            block["ignore"] = list(self.ignore)
        if not self.allow_fallbacks:
            block["allow_fallbacks"] = False
        max_price: dict = {}
        if self.max_price_input_mtok is not None:
            max_price["prompt"] = self.max_price_input_mtok
        if self.max_price_output_mtok is not None:
            max_price["completion"] = self.max_price_output_mtok
        if max_price:
            block["max_price"] = max_price
        return block


@dataclass(frozen=True)
class CatalogModel:
    """One selectable model.

    Attributes:
        key: Stable slug persisted in the DB and embedded in Discord custom_ids
            (e.g. ``"kimi-k3"``). Never change once shipped.
        label: Human label shown in the Discord select (e.g. ``"Kimi K3"``).
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
    # Capability metadata is deliberately immutable catalog data. Availability
    # and the display-only cost tier are administrator-controlled DB settings.
    context_window: int = 128_000
    max_output_tokens: int = 16_384
    supports_vision: bool = False
    supports_tools: bool = True
    # Only meaningful for OPENROUTER models — every other provider serves one
    # endpoint, so there is nothing to choose between.
    openrouter_routing: OpenRouterRouting | None = None

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

    @property
    def vision_capable(self) -> bool:
        """Compatibility alias used by attachment and admin code."""
        return self.supports_vision

    @property
    def needs_prompted_output(self) -> bool:
        """Whether structured output has to be prompted rather than tool-called.

        The property belongs to the *model*, not the endpoint serving it: the
        open-weight families are uneven on tool-choice and json_schema output
        wherever they are hosted, and prompted JSON is the one mode all of them
        handle. Digital Ocean and Zen serve nothing else, so everything there
        qualifies; OpenRouter mixes open weights in with Grok and OpenAI's Luna,
        which do structured output natively and must not be downgraded.
        """
        if self.provider in (
            ModelProvider.DIGITALOCEAN,
            ModelProvider.OPENCODE_ZEN,
        ):
            return True
        if self.provider is ModelProvider.OPENROUTER:
            return self.family in OPEN_WEIGHT_FAMILIES
        return False


# Families whose weights are published, whatever endpoint happens to serve
# them. These are the models with uneven structured-output support, and the set
# a precision/quantization question can even be asked about — a proprietary
# model has exactly one build, served by its owner.
OPEN_WEIGHT_FAMILIES: frozenset[str] = frozenset(
    {"Kimi", "GLM", "DeepSeek", "Gemma", "Qwen", "MiniMax"}
)


# The model families the admin override exposes.
MODEL_FAMILIES: tuple[str, ...] = (
    "Kimi",
    "GLM",
    "DeepSeek",
    "Gemma",
    "Qwen",
    "MiniMax",
    "Gemini",
    "GPT",
    "Claude",
    "Grok",
)


# Who *made* each family, which is not who serves it: Grok is served through
# OpenRouter but the lab is xAI, and the open weights run on Digital Ocean /
# OpenCode Zen / OpenRouter regardless of origin. `provider` answers "which
# SDK"; this answers "whose model", which is what a reader wants next to the
# name.
MODEL_VENDORS: dict[str, str] = {
    "Kimi": "Moonshot",
    "GLM": "Zhipu",
    "DeepSeek": "DeepSeek",
    "Gemma": "Google",
    "Qwen": "Alibaba",
    "MiniMax": "MiniMax",
    "Gemini": "Google",
    "GPT": "OpenAI",
    "Claude": "Anthropic",
    "Grok": "xAI",
}


def model_vendor(model: CatalogModel) -> str:
    """The lab that made ``model``, for display beside its label."""
    return MODEL_VENDORS.get(model.family, model.family)


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
# Claude Sonnet 5 exposes the full effort ladder (thinking is adaptive by
# default and effort tunes its depth); Haiku 4.5 has no effort knob at all.
_CLAUDE_EFFORT = (
    ReasoningLevel.LOW,
    ReasoningLevel.MEDIUM,
    ReasoningLevel.HIGH,
    ReasoningLevel.XHIGH,
    ReasoningLevel.MAX,
)


# Curated catalog. Kept <= 24 entries so the whole set fits in one Discord
# string-select (25-option limit, leaving room for a "server default" sentinel).
# Gemini -> Google, GPT -> OpenAI, Claude -> Anthropic, Grok -> OpenRouter,
# and the open weights -> Digital Ocean / OpenCode Zen / OpenRouter, all
# OpenAI-compatible. Model ids reflect the latest releases as of mid-2026
# (verified against provider model listings); they are wire ids and can be
# re-verified without a migration.
MODEL_CATALOG: tuple[CatalogModel, ...] = (
    # --- Open weights via Digital Ocean serverless inference ---
    # DO uses flat model ids (verified against GET /v1/models on the live
    # account), not vendor-prefixed paths — an unknown id 403s.
    # Kimi K2.6 was retired here on 2026-08-02 — Kimi K3 on Zen supersedes it,
    # and the freed slot went to Grok 4.5. Its pricing and provider mapping are
    # deliberately retained (llm_pricing, usage_invoice) for historical rows.
    # Gemma left Digital Ocean on 2026-08-13. Google publishes Gemma as bf16
    # weights and serves no endpoint of its own, so bf16 IS the reference build
    # and OpenRouter carries four of them — the cheapest (open-inference, at
    # $0.08/$0.35) undercuts DO's undeclared $0.18/$0.50 outright. Precision
    # floor rather than a pin: open-inference sat at 92.7% 30-day uptime, and
    # the filter lets routing fail over to coreweave/venice/novita bf16 without
    # ever dropping to the fp4 endpoints that share this model id.
    CatalogModel(
        key="gemma-4-31b",
        label="Gemma 4 31B",
        family="Gemma",
        provider=ModelProvider.OPENROUTER,
        model_id="google/gemma-4-31b-it",
        openrouter_routing=OpenRouterRouting(
            quantizations=("bf16",),
            max_price_input_mtok=0.14,
            max_price_output_mtok=0.40,
        ),
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
    # --- Open weights via OpenCode Zen (OpenAI-compatible /chat/completions) ---
    # Zen wire ids verified against GET https://opencode.ai/zen/v1/models. Note
    # they do NOT all match the DO ids for the same model: DeepSeek is
    # "deepseek-v4-flash" here but "deepseek-4-flash" on DO, and the DO-era id
    # is still what historical usage rows carry (see llm_pricing).
    # Reasoning: Qwen keeps the low/medium/high knob its 3.5 sibling exposes.
    # Kimi K3 and MiniMax M3 declare NO knob — their reasoning support is
    # unverified, and declaring none sends no effort field at all, which every
    # OpenAI-compatible endpoint accepts. Add the ladder once it is confirmed.
    CatalogModel(
        key="kimi-k3",
        label="Kimi K3 (Moonshot)",
        family="Kimi",
        provider=ModelProvider.OPENCODE_ZEN,
        model_id="kimi-k3",
    ),
    CatalogModel(
        key="minimax-m3",
        label="MiniMax M3",
        family="MiniMax",
        provider=ModelProvider.OPENCODE_ZEN,
        model_id="minimax-m3",
    ),
    # Qwen3.6 Plus left Zen on 2026-08-13: Alibaba, the model's author, is the
    # ONLY endpoint OpenRouter carries for it, at $0.325/$1.95 against Zen's
    # $0.50/$3.00. Same build, same lab, 35% less. No routing constraints — a
    # single-endpoint model has nothing to choose between, and a precision
    # filter would only be able to make it fail.
    CatalogModel(
        key="qwen3-6-plus",
        label="Qwen3.6 Plus",
        family="Qwen",
        provider=ModelProvider.OPENROUTER,
        model_id="qwen/qwen3.6-plus",
        reasoning_levels=_OPEN_EFFORT,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    # GLM left Zen on 2026-08-13. Z.AI serves its own model at fp8, so fp8 is
    # the reference build, not a downgrade — and Zen charges exactly Z.AI's
    # $1.40/$4.40 while a dozen fp8 endpoints sell the same precision for half.
    # The floor admits fp8 and bf16 only, so routing can never quietly drop to
    # one of the fp4 endpoints sharing this id.
    CatalogModel(
        key="glm-5-2",
        label="GLM-5.2 (Zhipu)",
        family="GLM",
        provider=ModelProvider.OPENROUTER,
        model_id="z-ai/glm-5.2",
        reasoning_levels=_OPEN_EFFORT,
        default_reasoning=ReasoningLevel.MEDIUM,
        openrouter_routing=OpenRouterRouting(
            quantizations=("fp8", "bf16"),
            max_price_input_mtok=0.80,
            max_price_output_mtok=3.20,
        ),
    ),
    # DeepSeek V4 Pro, added 2026-08-13 into the slot 3.1 Flash Lite vacated.
    # Same routing shape as Flash below and for the same measured reason: the
    # authors' own endpoint never actually takes our traffic, so plan around
    # what does. Sampling put baidu/fp8 on 5 of 6 requests at $0.4225/$0.845 —
    # half what Digital Ocean charges and a QUARTER of Zen's $1.74/$3.48, which
    # is priced to shed demand while Zen is compute-starved on this model.
    # 1M context; text-only, so no vision.
    CatalogModel(
        key="deepseek-v4-pro",
        label="DeepSeek V4 Pro",
        family="DeepSeek",
        provider=ModelProvider.OPENROUTER,
        model_id="deepseek/deepseek-v4-pro",
        context_window=1_048_576,
        reasoning_levels=_OPEN_EFFORT,
        default_reasoning=ReasoningLevel.MEDIUM,
        openrouter_routing=OpenRouterRouting(
            order=("deepseek",),
            max_price_input_mtok=0.60,
            max_price_output_mtok=1.20,
        ),
    ),
    # DeepSeek left Zen on 2026-08-13 for the authors' own endpoint. This one is
    # a reliability move first and a cost move second: DeepSeek rate-limits
    # OpenCode's traffic on Flash, and this model is the default summarizer.
    # Same $0.14/$0.28 Zen charges, with cache reads at $0.0028 against Zen's
    # $0.028 — a tenfold cut on the axis a summarizer actually spends.
    #
    # No precision filter: DeepSeek declares no quantization on its own
    # endpoint, so any allow-list would exclude the reference build itself.
    # Order puts the authors first and the price ceiling keeps every fallback at
    # or below their rate. Digital Ocean is excluded by name on evidence — at
    # $0.068/$0.168 it undercuts every *declared fp4* endpoint of this model,
    # which is not a discount a full-precision build can fund.
    CatalogModel(
        key="deepseek-v4",
        label="DeepSeek V4 Flash",
        family="DeepSeek",
        provider=ModelProvider.OPENROUTER,
        model_id="deepseek/deepseek-v4-flash",
        reasoning_levels=_OPEN_EFFORT,
        default_reasoning=ReasoningLevel.MEDIUM,
        openrouter_routing=OpenRouterRouting(
            order=("deepseek",),
            ignore=("digitalocean",),
            max_price_input_mtok=0.14,
            max_price_output_mtok=0.28,
        ),
    ),
    # --- Gemini via Google ---
    # Gemini 3 Flash left the catalog on 2026-08-13 — the oldest Flash we
    # carried, and still on a *preview* wire id — to make room for 3.7 Flash.
    # Like 3.1 Flash Lite before it, the wire id stays in service outside the
    # catalog: the resources agent's reframer/gap-filler/author and the blogging
    # scout and research agents all pin ``gemini-3-flash-preview`` directly, so
    # its price patch and provider mapping remain live rather than historical.
    CatalogModel(
        key="gemini-3-7-flash",
        label="Gemini 3.7 Flash",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3.7-flash",
        supports_vision=True,
        # Verified against the Gemini models API, not assumed: 1M in, 64K out.
        context_window=1_048_576,
        max_output_tokens=65_536,
        reasoning_levels=_GEMINI_THINKING,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    # Gemini 3.1 Flash Lite left the catalog on 2026-08-13, superseded by 3.5
    # Flash Lite in the same class (3.6 Flash is a different class, not a
    # replacement), freeing the last slot for DeepSeek V4 Pro. The wire id is
    # still live: title generation, media reading, image prompt review and the
    # blogging agents all pin it directly rather than through the catalog, so
    # its price patch and provider mapping stay load-bearing, not historical.
    CatalogModel(
        key="gemini-3-1-pro",
        label="Gemini 3.1 Pro",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3.1-pro",
        supports_vision=True,
        reasoning_levels=_GEMINI_THINKING,
        default_reasoning=ReasoningLevel.HIGH,
    ),
    CatalogModel(
        key="gemini-3-5-flash-lite",
        label="Gemini 3.5 Flash Lite",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3.5-flash-lite",
        supports_vision=True,
        reasoning_levels=_GEMINI_THINKING,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    # Gemini 3.6 Flash replaced 3.5 Flash (2026-07-21); the old
    # ``gemini-3-5-flash`` key was remapped to this entry by migration.
    CatalogModel(
        key="gemini-3-6-flash",
        label="Gemini 3.6 Flash",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3.6-flash",
        supports_vision=True,
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
        supports_vision=True,
        reasoning_levels=_OPENAI_5X,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-4-mini",
        label="GPT-5.4 Mini",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.4-mini",
        supports_vision=True,
        reasoning_levels=_OPENAI_5X,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-4",
        label="GPT-5.4",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.4",
        supports_vision=True,
        reasoning_levels=_OPENAI_5X,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-5",
        label="GPT-5.5",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.5",
        supports_vision=True,
        reasoning_levels=_OPENAI_5X,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    # Luna is OpenAI's model but is served through OpenRouter (2026-08-06):
    # same OpenAI upstream at half the rate ($0.10/$0.60 against direct's
    # $0.20/$1.20). Probed live: OpenRouter passes reasoning_effort through to
    # the upstream — every level none→max returns 200 — so the full 5.6
    # ladder stays.
    CatalogModel(
        key="gpt-5-6-luna",
        label="GPT-5.6 Luna",
        family="GPT",
        provider=ModelProvider.OPENROUTER,
        model_id="openai/gpt-5.6-luna",
        supports_vision=True,
        reasoning_levels=_OPENAI_56,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-6-sol",
        label="GPT-5.6 Sol",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.6-sol",
        supports_vision=True,
        reasoning_levels=_OPENAI_56,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    CatalogModel(
        key="gpt-5-6-terra",
        label="GPT-5.6 Terra",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.6-terra",
        supports_vision=True,
        reasoning_levels=_OPENAI_56,
        default_reasoning=ReasoningLevel.MEDIUM,
    ),
    # --- Claude via Anthropic ---
    CatalogModel(
        key="claude-opus-5",
        label="Claude Opus 5",
        family="Claude",
        provider=ModelProvider.ANTHROPIC,
        model_id="claude-opus-5",
        supports_vision=True,
        reasoning_levels=_CLAUDE_EFFORT,
        default_reasoning=ReasoningLevel.HIGH,
    ),
    CatalogModel(
        key="claude-haiku-4-5",
        label="Claude Haiku 4.5",
        family="Claude",
        provider=ModelProvider.ANTHROPIC,
        model_id="claude-haiku-4-5",
        supports_vision=True,
    ),
    CatalogModel(
        key="claude-sonnet-5",
        label="Claude Sonnet 5",
        family="Claude",
        provider=ModelProvider.ANTHROPIC,
        model_id="claude-sonnet-5",
        supports_vision=True,
        reasoning_levels=_CLAUDE_EFFORT,
        default_reasoning=ReasoningLevel.HIGH,
    ),
    # --- Qwen3.8 via OpenRouter ---
    # The 2.4T A95B weights are NOT on our Digital Ocean account — GET
    # /v1/models lists qwen3.8-max but no A95B, and an unknown DO id 403s — so
    # OpenRouter is the only route that can serve it. No premium for that:
    # OpenRouter's cheapest endpoint for it *is* DigitalOcean-served, at the
    # same $2/$6 per M as every other route. Capabilities from GET
    # /api/v1/models/qwen/qwen3.8-2.4t-a95b/endpoints (2026-08): 262K context,
    # text-only input (no vision), tools, and a reasoning_effort knob.
    CatalogModel(
        key="qwen3-8-2-4t",
        label="Qwen3.8 2.4T A95B",
        family="Qwen",
        provider=ModelProvider.OPENROUTER,
        model_id="qwen/qwen3.8-2.4t-a95b",
        context_window=262_144,
        reasoning_levels=_OPEN_EFFORT,
        default_reasoning=ReasoningLevel.MEDIUM,
        # The weak spot in this catalog: Alibaba does not serve these weights on
        # OpenRouter, so there is no author build to match, and the only
        # endpoints that declare a precision declare fp4 and nvfp4. Every route
        # bills the same $2/$6, so price reveals nothing either. The best
        # available position is to exclude the two endpoints KNOWN to be
        # quantized and take the undeclared ones, which is a weaker guarantee
        # than every other model here gets. The ceiling admits together
        # ($2.50/$6.25) as the only fallback once digital ocean is out.
        openrouter_routing=OpenRouterRouting(
            ignore=("deepinfra", "modal"),
            max_price_input_mtok=2.50,
            max_price_output_mtok=6.25,
        ),
    ),
    # --- Grok via OpenRouter ---
    # xAI has no first-party key here, so Grok routes through OpenRouter's
    # OpenAI-compatible /chat/completions. Capabilities verified against GET
    # /api/v1/models/x-ai/grok-4.6/endpoints (2026-08): 500K context,
    # text+image input, tools, and a reasoning_effort knob. OpenRouter
    # normalizes low/medium/high effort for every route, so the standard
    # open-effort ladder applies. 4.6 replaced 4.5 on 2026-08-13 at the same
    # $2/$6 headline rate.
    CatalogModel(
        key="grok-4-6",
        label="Grok 4.6 (xAI)",
        family="Grok",
        provider=ModelProvider.OPENROUTER,
        model_id="x-ai/grok-4.6",
        supports_vision=True,
        context_window=500_000,
        reasoning_levels=_OPEN_EFFORT,
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
