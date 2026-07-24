"""Discord chat WRITER agent — the large, tool-less second stage of two-stage mode.

In two-stage chat mode the small WORKER runs the agentic turn (tools, rankings,
silence/continue decisions) and, instead of writing the reply, emits a
:class:`~smarter_dev.bot.agents.chat_models.WriterBrief`. This module builds the
WRITER: a single generation call on a large model with NO tools whose system
prompt makes it BE the bot. It is handed the brief's relevant information and
writes one friendly Discord message in response.

Usage:
    from smarter_dev.bot.agents.writer_agent import build_writer_prompt
    from smarter_dev.bot.agents.writer_agent import get_writer_agent

    agent = get_writer_agent(writer_model_id)
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

WRITER_SYSTEM_PROMPT = (
    Path(__file__).parent / "prompts" / "writer_agent.md"
).read_text(encoding="utf-8")


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
    if catalog_model is not None and catalog_model.provider is ModelProvider.DIGITALOCEAN:
        return PromptedOutput(WriterOutput)
    return WriterOutput


def _writer_model_settings_for(model_id: str) -> ModelSettings | None:
    """Per-provider model settings for the writer at its default reasoning.

    ``get_writer_agent`` has no per-channel reasoning knob — the writer is a
    single tool-less generation — so a catalog model uses its own
    ``default_reasoning`` (``None`` request) and a non-catalog id gets no
    explicit settings (the provider default applies).
    """
    catalog_model = _catalog_model_for_id(model_id)
    if catalog_model is None:
        return None
    return model_settings_for(catalog_model, None)


def build_writer_prompt(brief: WriterBrief) -> str:
    """Render ``brief`` into the writer's user message (pure function).

    Deterministically lays out the attributed message summaries, the search
    findings, and the verbatim question(s), and states the language to reply in.
    When ``brief.send_voice`` is True it appends an explicit instruction to also
    produce a short spoken-form ``voice_summary``; when False, no voice text is
    added. Empty sections are omitted. Does not mutate ``brief``.
    """
    sections: list[str] = [
        f"Write your reply in {brief.response_language}.",
    ]

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


# One writer agent per wire model id. The writer takes no reasoning override, so
# a single model id maps to exactly one cached ``Agent``.
_writer_agents: dict[str, Agent[None, WriterOutput]] = {}


def get_writer_agent(model_id: str) -> Agent[None, WriterOutput]:
    """Return (building on first use) the tool-less writer agent for ``model_id``.

    ``model_id`` is the provider wire id of the large WRITER model (the channel
    override's ``writer_model``). Model resolution mirrors ``get_chat_agent``:
    catalog models route through the shared model router, ad-hoc ids fall back to
    the historical prefix logic, and DigitalOcean-hosted models use
    ``PromptedOutput``. The agent has NO tools and returns ``WriterOutput``.
    Agents are cached per model id, so a given writer model always reuses one
    instance.
    """
    agent = _writer_agents.get(model_id)
    if agent is None:
        agent = Agent(
            build_agent_model(model_id),
            output_type=_output_type_for(model_id),
            system_prompt=WRITER_SYSTEM_PROMPT,
            model_settings=_writer_model_settings_for(model_id),
        )
        _writer_agents[model_id] = agent
    return agent
