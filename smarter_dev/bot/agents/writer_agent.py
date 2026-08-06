"""Discord chat WRITER agent — the tool-less answering stage of two-stage mode.

In two-stage chat mode the cheap DRAFTER (worker) runs the agentic turn (tools,
rankings, silence/continue decisions) and, instead of writing the reply, emits a
:class:`~smarter_dev.bot.agents.chat_models.WriterBrief`. This module builds the
WRITER: a single generation call on the channel's primary model with NO tools
whose system prompt makes it BE the bot. It is handed the brief's relevant
information and writes one friendly Discord message in response.

Usage:
    from smarter_dev.bot.agents.writer_agent import build_writer_prompt
    from smarter_dev.bot.agents.writer_agent import get_writer_agent

    agent = get_writer_agent(primary_model_id, reasoning_level)
    result = await agent.run(build_writer_prompt(brief))
    output = result.output  # WriterOutput (message, voice_summary)
"""

from __future__ import annotations

from pathlib import Path

from pydantic_ai import Agent
from pydantic_ai import PromptedOutput
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from smarter_dev.bot.agents.chat_agent import build_agent_model
from smarter_dev.bot.agents.chat_models import WriterBrief
from smarter_dev.bot.agents.chat_models import WriterOutput
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.shared.model_catalog import MODEL_CATALOG
from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import parse_reasoning_level

WRITER_SYSTEM_PROMPT = (
    Path(__file__).parent / "prompts" / "writer_agent.md"
).read_text(encoding="utf-8")


# Providers whose models need PromptedOutput instead of tool/json_schema output.
# Both serve the same open-weights families over an OpenAI-compatible endpoint
# that is uneven on structured output; prompted JSON is the one mode every one
# of them handles.
_PROMPTED_OUTPUT_PROVIDERS = (
    ModelProvider.DIGITALOCEAN,
    ModelProvider.OPENCODE_ZEN,
)


def _catalog_model_for_id(model_id: str) -> CatalogModel | None:
    """Return the catalog model whose wire ``model_id`` matches, if any."""
    for model in MODEL_CATALOG:
        if model.model_id == model_id:
            return model
    return None


def _output_type_for(model_id: str) -> type[WriterOutput] | PromptedOutput:
    """Structured-output mode for ``model_id``.

    Mirrors :func:`chat_agent._output_type_for`: DigitalOcean-hosted models get
    ``PromptedOutput`` (schema in the prompt, JSON text back) because DO's
    OpenAI-compatible endpoint is uneven on tool-call / json_schema output;
    every other provider returns ``WriterOutput`` via the default tool output.
    """
    catalog_model = _catalog_model_for_id(model_id)
    if catalog_model is not None and catalog_model.provider in _PROMPTED_OUTPUT_PROVIDERS:
        return PromptedOutput(WriterOutput)
    return WriterOutput


def _writer_settings_for(
    model_id: str, reasoning_level: str | None = None
) -> ModelSettings | None:
    """Per-provider model settings for the writer at ``reasoning_level``.

    Mirrors :func:`chat_agent._model_settings_for` / ``get_worker_agent``: a
    catalog model routes through the shared model router, clamping the requested
    ``reasoning_level`` onto what it supports (or its ``default_reasoning`` when
    ``None``); a non-catalog id gets no explicit settings (the provider default
    applies).
    """
    catalog_model = _catalog_model_for_id(model_id)
    if catalog_model is None:
        return None
    return model_settings_for(catalog_model, parse_reasoning_level(reasoning_level))


def _identity_directive(model_id: str) -> str:
    """A system-prompt addendum telling the writer its own model identity.

    The tool-less writer answers from its system prompt + the drafter's brief; it
    never sees the ``<your-model>`` metadata the drafter does, and the small
    drafter can't be relied on to relay the model name through the brief. So the
    writer carries its own identity: the catalog label for ``model_id`` (its wire
    id as a fallback for ad-hoc ids). Phrased to surface only when asked, mirroring
    the worker/drafter prompt's "never volunteer it" rule.
    """
    catalog_model = _catalog_model_for_id(model_id)
    name = catalog_model.label if catalog_model is not None else model_id
    return (
        f"\n\nYour underlying model is {name}. If someone directly asks what model "
        f"or AI you are, tell them {name}. Never bring it up otherwise."
    )


def build_writer_prompt(brief: WriterBrief, *, long_term: str | None = None) -> str:
    """Render ``brief`` into the writer's user message (pure function).

    Deterministically lays out the attributed message summaries, the search
    findings, and the verbatim question(s), and states the language to reply in.
    When ``brief.send_voice`` is True it appends an explicit instruction to also
    produce a short spoken-form ``voice_summary``; when False, no voice text is
    added. Empty sections are omitted. Does not mutate ``brief``.

    Memory comes in by two different routes on purpose. ``long_term`` is the
    guild's memory blob, injected verbatim by the engine — it is the writer's
    identity in this server, and routing it through the cheap drafter would let a
    small model paraphrase the persona afresh every single turn. ``brief.remembered``
    is situational: the lines from today's notes and this hour's actions that the
    drafter judged the reply would be worse without. Both are laid out before the
    conversation so the ordering runs far to near, and both are framed as the
    writer's own memory rather than as findings to cite.
    """
    sections: list[str] = [
        f"Write your reply in {brief.response_language}.",
    ]

    if long_term and long_term.strip():
        sections.append(
            f"What you remember about this place:\n{long_term.strip()}"
        )

    if brief.remembered:
        remembered = "\n".join(f"- {line}" for line in brief.remembered)
        sections.append(f"Also on your mind right now:\n{remembered}")

    if brief.message_summaries:
        summaries = "\n".join(f"- {summary}" for summary in brief.message_summaries)
        sections.append(f"What people said:\n{summaries}")

    if brief.search_findings:
        findings = "\n".join(f"- {finding}" for finding in brief.search_findings)
        sections.append(f"What you looked up:\n{findings}")

    if brief.questions:
        questions = "\n".join(f'- "{question}"' for question in brief.questions)
        sections.append(f"The question(s) in front of you, exactly as asked:\n{questions}")

    if brief.send_voice:
        sections.append(
            "The person explicitly asked for a voice message. Alongside your "
            "written message, also produce a short spoken-form voice_summary — "
            "a few natural, conversational sentences meant to be heard, not read."
        )

    return "\n\n".join(sections)


# One writer agent per resolved (wire model id, reasoning level). The writer is
# the channel's answering model and honours the admin-chosen reasoning, so a
# given model id at two reasoning levels needs distinct model_settings.
_writer_agents: dict[tuple[str, str | None], Agent[None, WriterOutput]] = {}


def get_writer_agent(
    model_id: str, reasoning_level: str | None = None
) -> Agent[None, WriterOutput]:
    """Return (building on first use) the tool-less writer agent for ``model_id``.

    ``model_id`` is the provider wire id of the answering WRITER model (the
    channel's primary ``model_key``); ``reasoning_level`` is the admin-chosen
    level (or ``None`` for the model default). Model resolution mirrors
    ``get_chat_agent``: catalog models route through the shared model router
    (clamping the reasoning onto what the model supports), ad-hoc ids fall back
    to the historical prefix logic, and DigitalOcean-hosted models use
    ``PromptedOutput``. The agent has NO tools and returns ``WriterOutput``.
    Agents are cached per resolved (id, reasoning) pair, so a given
    configuration always reuses one instance.
    """
    cache_key = (model_id, reasoning_level)
    agent = _writer_agents.get(cache_key)
    if agent is None:
        agent = Agent(
            build_agent_model(model_id),
            output_type=_output_type_for(model_id),
            system_prompt=WRITER_SYSTEM_PROMPT + _identity_directive(model_id),
            model_settings=_writer_settings_for(model_id, reasoning_level),
        )
        _writer_agents[cache_key] = agent
    return agent
