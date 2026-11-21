"""Build a Pydantic AI ``Model`` for any catalog model.

Routing by :class:`~smarter_dev.shared.model_catalog.ModelProvider`:

- ``GOOGLE``       -> ``GoogleModel`` + ``GoogleProvider`` (Gemini keys).
- ``OPENAI``       -> ``OpenAIResponsesModel`` + ``OpenAIProvider`` (OpenAI key).
- ``ANTHROPIC``    -> ``AnthropicModel`` + ``AnthropicProvider`` (Anthropic key,
  ``get_credential("anthropic")``), with 1-hour prompt caching on
  the tool definitions, the system prompt and the newest message.
- ``OPENROUTER``   -> ``OpenAIChatModel`` + ``OpenRouterProvider``.

API keys are read from the environment (matching the existing convention in
``chat_agent.py``), not from the settings class.
"""

from __future__ import annotations


from pydantic_ai import AgentRetries
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.models.google import GoogleModel
from pydantic_ai.models.google import GoogleModelSettings
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openai import OpenAIChatModelSettings
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from pydantic_ai.profiles import ModelProfile
from pydantic_ai.profiles import merge_profile
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings

from smarter_dev.shared.config import get_settings
from smarter_dev.shared.credentials import require_credential
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import resolve_reasoning_level
from smarter_dev.shared.skrift_models import ANTHROPIC_CACHE_TTL

# Output retries for a model offered its output tool on "auto" (see
# ``CatalogModel.supports_forced_tool_choice``). Such a model can answer in
# plain text; pydantic-ai spends one output retry re-prompting it for the tool
# call. The default budget is one, so a second slip would fail the turn.
UNFORCED_OUTPUT_RETRIES = 3

# Prompt caching for every direct Claude request: three of Anthropic's four
# breakpoints, all on the 1-hour TTL. The tool definitions and the system
# prompt are the stable prefix. The third breakpoint sits on the newest message,
# so the next request in a tool loop, and the next turn of an append-only
# conversation, reads the whole history back at the cache-read rate. Chat
# replies and proactive wakes routinely come more than five minutes apart, so
# a 5-minute message entry would usually expire before it is read and the
# history would be written again. One TTL everywhere also means every cache
# write is a 1-hour write, which is the rate llm_pricing charges direct Claude
# writes at; add a 5-minute breakpoint here and that pricing has to split.
# ANTHROPIC_CACHE_TTL is defined in skrift_models, which the Skrift agents'
# settings share without importing pydantic-ai.
ANTHROPIC_CACHE_SETTINGS = AnthropicModelSettings(
    anthropic_cache_tool_definitions=ANTHROPIC_CACHE_TTL,
    anthropic_cache_instructions=ANTHROPIC_CACHE_TTL,
    anthropic_cache_messages=ANTHROPIC_CACHE_TTL,
)
# A single-request call (a title, an image read, a summary, a compaction) never
# reads its own messages back, so a message breakpoint would only bill its whole
# prompt at the 1-hour write rate, twice the input rate. Such agents pass
# ``cache_messages=False`` to model_settings_for; the tool and system-prompt
# breakpoints stay, since that prefix is shared by every call of the agent.
ANTHROPIC_NO_MESSAGE_CACHE = AnthropicModelSettings(anthropic_cache_messages=False)


def anthropic_api_key() -> str:
    """The direct Claude API key (opaque name first, then legacy names)."""
    return require_credential("anthropic")


def build_model_for(model: CatalogModel) -> Model:
    """Return a configured Pydantic AI ``Model`` for ``model``."""
    if model.provider is ModelProvider.GOOGLE:
        api_key = require_credential("gemini")
        return GoogleModel(model.model_id, provider=GoogleProvider(api_key=api_key))
    if model.provider is ModelProvider.OPENAI:
        return OpenAIResponsesModel(
            model.model_id,
            provider=OpenAIProvider(api_key=require_credential("openai")),
        )
    if model.provider is ModelProvider.ANTHROPIC:
        provider = AnthropicProvider(api_key=anthropic_api_key())
        profile = provider.model_profile(model.model_id)
        if not model.supports_forced_tool_choice:
            # The 5.5 generation answers tool_choice "any"/"tool" with a 400.
            # pydantic-ai's own profile knows this for Opus and Sonnet 5.5;
            # the catalog flag makes it hold for every model that sets it.
            profile = merge_profile(
                profile, ModelProfile(supports_forced_tool_choice=False)
            )
        return AnthropicModel(
            model.model_id,
            provider=provider,
            profile=profile,
            settings=ANTHROPIC_CACHE_SETTINGS,
        )
    if model.provider is ModelProvider.OPENROUTER:
        provider = OpenRouterProvider(api_key=require_credential("openrouter"))
        if not model.supports_forced_tool_choice:
            # e.g. Claude Sonnet 5.5: its endpoints list tool_choice "required"
            # as unsupported, so offer the output tool on "auto". Only this
            # flag changes; the rest of OpenRouter's profile for the id stays.
            return OpenAIChatModel(
                model.model_id,
                provider=provider,
                profile=merge_profile(
                    provider.model_profile(model.model_id),
                    OpenAIModelProfile(openai_supports_tool_choice_required=False),
                ),
            )
        return OpenAIChatModel(model.model_id, provider=provider)
    raise ValueError(f"Unhandled provider: {model.provider!r}")


def agent_retries_for(model: CatalogModel | None) -> AgentRetries | None:
    """Retry budget for a structured-output agent on ``model``.

    ``None`` keeps pydantic-ai's defaults. Only a model whose output tool
    cannot be forced gets more output retries, because only it can end a turn
    with plain text; when those run out the run raises and the caller's usual
    error reply applies, so the turn is never dropped silently.
    """
    if model is not None and not model.supports_forced_tool_choice:
        return AgentRetries(output=UNFORCED_OUTPUT_RETRIES)
    return None


def model_settings_for(
    model: CatalogModel,
    reasoning_level: ReasoningLevel | None = None,
    *,
    cache_messages: bool = True,
) -> ModelSettings | None:
    """Return per-provider model settings for ``model`` at ``reasoning_level``.

    ``reasoning_level`` is the admin's per-channel choice (or ``None`` to use the
    model's default). It is mapped onto the model's supported levels by
    :func:`resolve_reasoning_level`, so an invalid pick is clamped rather than
    passed through verbatim. A model with no reasoning knob (or one that resolves
    to no level) gets ``None`` — the provider's own default applies.

    OpenRouter models are the exception to that last part: their endpoint
    constraints (see :class:`OpenRouterRouting`) have to ride on every request
    whether or not a reasoning level applies, because dropping them means
    OpenRouter silently picks by price and price tracks quantization.

    ``cache_messages=False`` is for single-request agents: on a direct Claude
    model it drops the message cache breakpoint (ANTHROPIC_NO_MESSAGE_CACHE).
    Other providers ignore it.
    """
    effective = resolve_reasoning_level(model, reasoning_level)
    if model.provider is ModelProvider.OPENROUTER:
        return _openrouter_settings(model, effective)
    if effective is None:
        if model.provider is ModelProvider.ANTHROPIC and not cache_messages:
            return AnthropicModelSettings(**ANTHROPIC_NO_MESSAGE_CACHE)
        return None
    if model.provider is ModelProvider.GOOGLE:
        # Gemini's thinking_level enum is upper-cased on the wire.
        return GoogleModelSettings(
            google_thinking_config={"thinking_level": effective.value.upper()},
        )
    if model.provider is ModelProvider.OPENAI:
        return OpenAIResponsesModelSettings(openai_reasoning_effort=effective.value)
    if model.provider is ModelProvider.ANTHROPIC:
        # Claude 4.6+ thinking is adaptive; effort tunes its depth. Never send
        # {"type": "disabled"}: the 5.5 generation rejects it (Opus 5.5 at any
        # effort), and no catalog Claude ladder offers NONE to map onto it.
        # The cache breakpoints ride on the model itself (build_model_for), so
        # these settings merge over them rather than replacing them.
        settings = AnthropicModelSettings(
            anthropic_thinking={"type": "adaptive"},
            anthropic_effort=effective.value,
        )
        if not cache_messages:
            settings.update(ANTHROPIC_NO_MESSAGE_CACHE)
        return settings
    return OpenAIChatModelSettings(openai_reasoning_effort=effective.value)


def _openrouter_settings(
    model: CatalogModel, effective: ReasoningLevel | None
) -> ModelSettings | None:
    """Chat-model settings for an OpenRouter model: effort plus endpoint policy.

    The ``provider`` block goes through ``extra_body`` because it is
    OpenRouter's own extension to the OpenAI chat schema, not something the
    OpenAI SDK models. Returns ``None`` when the model asks for neither, so an
    unconstrained model still sends a bare request.
    """
    settings: dict = {}
    if effective is not None:
        settings["openai_reasoning_effort"] = effective.value
    routing = model.openrouter_routing
    if routing is not None:
        provider_block = routing.as_provider_block()
        if provider_block:
            settings["extra_body"] = {"provider": provider_block}
    if not settings:
        return None
    return OpenAIChatModelSettings(**settings)
