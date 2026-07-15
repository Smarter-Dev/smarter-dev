"""Single source of truth for the selectable LLM models.

The admin per-channel model-override command (and its web-API validation and
enforcement) all resolve models through this catalog. Each :class:`CatalogModel`
pairs a stable ``key`` (persisted in the DB and embedded in Discord custom_ids)
with the ``model_id`` wire string handed to the provider SDK. Keys are stable;
``model_id`` values are the wire ids and may be re-verified/updated without a
migration.

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


@dataclass(frozen=True)
class CatalogModel:
    """One selectable model.

    Attributes:
        key: Stable slug persisted in the DB and embedded in Discord custom_ids
            (e.g. ``"kimi-k2"``). Never change once shipped.
        label: Human label shown in the Discord select (e.g. ``"Kimi K2"``).
        family: One of the seven supported families.
        provider: Which provider SDK serves this model.
        model_id: Exact id passed to the provider SDK / sent as the ``model``
            field on the wire.
    """

    key: str
    label: str
    family: str
    provider: ModelProvider
    model_id: str


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


# Curated catalog. Kept <= 24 entries so the whole set fits in one Discord
# string-select (25-option limit, leaving room for a "server default" sentinel
# added in a later stage). Gemini -> Google, GPT -> OpenAI, everything else ->
# Digital Ocean's OpenAI-compatible serverless inference.
MODEL_CATALOG: tuple[CatalogModel, ...] = (
    CatalogModel(
        key="kimi-k2",
        label="Kimi K2 (Moonshot)",
        family="Kimi",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="moonshotai/kimi-k2-instruct",
    ),
    CatalogModel(
        key="glm-4-6",
        label="GLM-4.6 (Zhipu)",
        family="GLM",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="zai/glm-4.6",
    ),
    CatalogModel(
        key="deepseek-v3-1",
        label="DeepSeek V3.1",
        family="DeepSeek",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="deepseek-ai/deepseek-v3.1",
    ),
    CatalogModel(
        key="deepseek-r1",
        label="DeepSeek R1",
        family="DeepSeek",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="deepseek-ai/deepseek-r1",
    ),
    CatalogModel(
        key="gemma-3-27b",
        label="Gemma 3 27B",
        family="Gemma",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="google/gemma-3-27b-it",
    ),
    CatalogModel(
        key="qwen3-32b",
        label="Qwen3 32B",
        family="Qwen",
        provider=ModelProvider.DIGITALOCEAN,
        model_id="qwen/qwen3-32b",
    ),
    CatalogModel(
        key="gemini-3-1-flash-lite",
        label="Gemini 3.1 Flash Lite",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3.1-flash-lite",
    ),
    CatalogModel(
        key="gemini-3-flash",
        label="Gemini 3 Flash",
        family="Gemini",
        provider=ModelProvider.GOOGLE,
        model_id="gemini-3-flash-preview",
    ),
    CatalogModel(
        key="gpt-5-4",
        label="GPT-5.4",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.4",
    ),
    CatalogModel(
        key="gpt-5-4-nano",
        label="GPT-5.4 nano",
        family="GPT",
        provider=ModelProvider.OPENAI,
        model_id="gpt-5.4-nano",
    ),
)


def catalog_by_key() -> dict[str, CatalogModel]:
    """Return a fresh ``key -> CatalogModel`` mapping."""
    return {model.key: model for model in MODEL_CATALOG}


def get_model(key: str) -> CatalogModel | None:
    """Return the catalog model for ``key``, or ``None`` if unknown."""
    return catalog_by_key().get(key)


def is_valid_model_key(key: str) -> bool:
    """Return whether ``key`` names a catalog model."""
    return key in catalog_by_key()


def models_by_family() -> dict[str, list[CatalogModel]]:
    """Group the catalog by family, preserving catalog order within each family.

    Only families that have at least one model appear. Family insertion order
    follows first appearance in :data:`MODEL_CATALOG`.
    """
    grouped: dict[str, list[CatalogModel]] = {}
    for model in MODEL_CATALOG:
        grouped.setdefault(model.family, []).append(model)
    return grouped
