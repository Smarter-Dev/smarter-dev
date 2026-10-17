"""Every non-chat use of GPT-6 Luna moved to Claude Haiku 5.5 (#199).

The request-shape tests run against a recorded fake of Anthropic's Messages API
(``tests/claude_wire.py``), so the router, the settings and pydantic-ai's
request building are all real and nothing leaves the box.
"""

from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent

from smarter_dev.shared.model_catalog import CatalogModel
from smarter_dev.shared.model_catalog import ModelProvider
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import direct_claude_model
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.shared.model_router import ANTHROPIC_CACHE_SETTINGS
from smarter_dev.shared.model_router import model_settings_for
from smarter_dev.shared.skrift_models import agent_model_name
from smarter_dev.shared.skrift_models import agent_model_settings
from tests.claude_wire import ClaudeServer
from tests.claude_wire import cache_controls
from tests.claude_wire import serve
from tests.claude_wire import text_reply

ONE_HOUR = {"type": "ephemeral", "ttl": "1h"}
HAIKU = get_model("claude-haiku-5-5")


@pytest.fixture(autouse=True)
def _claude_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


# -- settings ------------------------------------------------------------------


def test_single_request_agents_drop_only_the_message_breakpoint():
    assert model_settings_for(HAIKU, ReasoningLevel.LOW, cache_messages=False) == {
        "anthropic_thinking": {"type": "adaptive"},
        "anthropic_effort": "low",
        "anthropic_cache_messages": False,
    }
    # The default keeps the model's own breakpoints untouched.
    assert "anthropic_cache_messages" not in model_settings_for(HAIKU)
    # An ad-hoc Claude id has no effort ladder but still drops the breakpoint.
    assert model_settings_for(
        direct_claude_model("claude-opus-5-5"), cache_messages=False
    ) == {"anthropic_cache_messages": False}
    # Other providers are unaffected.
    luna = get_model("gpt-6-luna")
    assert model_settings_for(luna, cache_messages=False) == model_settings_for(luna)


def test_skrift_names_and_settings_match_the_router():
    assert agent_model_name(HAIKU) == "anthropic:claude-haiku-5-5"
    assert agent_model_name(get_model("gpt-6-luna")) == "openai-responses:gpt-6-luna"
    assert agent_model_settings(HAIKU, ReasoningLevel.MEDIUM) == {
        **ANTHROPIC_CACHE_SETTINGS,
        **model_settings_for(HAIKU, ReasoningLevel.MEDIUM),
    }
    assert agent_model_settings(HAIKU, ReasoningLevel.LOW, cache_messages=False) == {
        "anthropic_cache_tool_definitions": "1h",
        "anthropic_cache_instructions": "1h",
        "anthropic_cache_messages": False,
        "anthropic_thinking": {"type": "adaptive"},
        "anthropic_effort": "low",
    }
    assert agent_model_settings(get_model("gpt-6-luna"), ReasoningLevel.LOW) == {
        "openai_reasoning_effort": "low"
    }
    with pytest.raises(ValueError):
        agent_model_name(
            CatalogModel(
                key="x",
                label="x",
                family="x",
                provider=ModelProvider.DIGITALOCEAN,
                model_id="x",
            )
        )


def test_skrift_models_stays_free_of_pydantic_ai():
    """Title, Resources and blogging modules load on the web tier, which cannot
    afford pydantic-ai's memory; skrift_models must not drag it in."""
    source = Path("smarter_dev/shared/skrift_models.py").read_text()
    assert "pydantic_ai" not in source.split('"""', 2)[2]


# -- on the wire -----------------------------------------------------------------


class Verdict(BaseModel):
    approved: bool
    reason: str


async def test_a_one_shot_structured_agent_caches_only_its_prompt(monkeypatch):
    """The image-prompt reviewer: Luna's openai_reasoning_effort became Claude's
    effort, structured output arrives natively with no tool forced, and only
    the policy prompt carries a 1-hour breakpoint."""
    from smarter_dev.bot.agents import image_prompt_reviewer

    monkeypatch.setattr(image_prompt_reviewer, "_reviewer_agent", None)
    monkeypatch.delenv("IMAGE_REVIEWER_MODEL", raising=False)
    server = serve(
        monkeypatch,
        text_reply(
            '{"approved": true, "reason": "A B-tree diagram."}', cache_read=1_200
        ),
    )

    decision = await image_prompt_reviewer.review_image_prompt("a B-tree diagram")

    assert decision.approved is True
    (body,) = server.requests
    assert body["model"] == "claude-haiku-5-5"
    assert body.get("tool_choice", {"type": "auto"})["type"] == "auto"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"]["effort"] == "low"
    assert "reasoning" not in body
    assert body["system"][-1]["cache_control"] == ONE_HOUR
    assert cache_controls(body) == [ONE_HOUR]


async def test_a_skrift_model_name_never_forces_a_tool(monkeypatch):
    """What Skrift materializes in the worker: an agent on the model *name*,
    with skrift_models' settings, asking for structured output."""
    import pydantic_ai.providers.anthropic as anthropic_providers

    server = ClaudeServer(
        [text_reply('{"approved": false, "reason": "off topic"}', cache_write_1h=700)]
    )
    monkeypatch.setattr(
        anthropic_providers, "AnthropicProvider", lambda **_: server.provider()
    )
    agent = Agent(
        agent_model_name(HAIKU),
        output_type=Verdict,
        system_prompt="Judge the prompt.",
        model_settings=agent_model_settings(
            HAIKU, ReasoningLevel.MEDIUM, cache_messages=False
        ),
    )

    result = await agent.run("a cat")

    assert result.output == Verdict(approved=False, reason="off topic")
    (body,) = server.requests
    assert body.get("tool_choice", {"type": "auto"})["type"] == "auto"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["output_config"]["effort"] == "medium"
    assert cache_controls(body) == [ONE_HOUR]
    assert result.usage.cache_write_tokens == 700


async def test_the_web_search_query_agent_reports_its_cache_writes(monkeypatch):
    from smarter_dev.web.web_search import queries

    monkeypatch.delenv("LITE_LLM_PROXY_HOST", raising=False)
    payload = (
        '{"queries": ['
        + ",".join(
            f'{{"angle": "a{n}", "query": "postgres count {n}"}}' for n in range(5)
        )
        + '], "needs_answer": true}'
    )
    server = serve(monkeypatch, text_reply(payload, cache_read=300, cache_write_1h=900))

    planned, needs_answer, usage = await queries.plan_queries("why is count slow")

    assert len(planned) == 5 and needs_answer is True
    assert usage == {
        "model": "claude-haiku-5-5",
        "requests": 1,
        "input_tokens": 10 + 300 + 900,
        "output_tokens": 5,
        "cache_read_tokens": 300,
        "cache_write_tokens": 900,
    }
    (body,) = server.requests
    assert body["output_config"]["effort"] == "medium"
    assert cache_controls(body) == [ONE_HOUR]


def test_an_openai_query_model_keeps_the_responses_api(monkeypatch):
    from pydantic_ai.models.openai import OpenAIResponsesModel

    from smarter_dev.web.web_search import queries

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert isinstance(queries.model_for("gpt-6-luna"), OpenAIResponsesModel)
    assert queries.settings_for("gpt-6-luna", "low", cache_messages=False) == {
        "openai_reasoning_effort": "low"
    }


# -- compaction ------------------------------------------------------------------


def test_a_discord_compaction_over_100k_tokens_bills_haikus_tier():
    from smarter_dev.web.api_native.chat_conversations import _billed_summarizer
    from smarter_dev.web.api_native.schemas import ChatAgentCompactionEventCreate

    def billed(tokens_in: int):
        return _billed_summarizer(
            ChatAgentCompactionEventCreate(
                event_kind="conversation",
                original_content="",
                summary="",
                original_chars=0,
                summary_chars=0,
                summarizer_tokens_input=tokens_in,
                summarizer_tokens_output=1_000,
                summarizer_model_name="claude-haiku-5-5",
            )
        ).cost_usd

    # $0.10 in, $0.50 out per million at the base rate.
    assert billed(100_000) == Decimal("0.0105")
    # Over 100,000 tokens the whole request bills 5x: $0.50 in, $2.50 out.
    assert billed(200_000) == Decimal("0.1025")


def test_the_migration_moves_only_a_luna_compaction_setting():
    path = next(Path("alembic/main/versions").glob("*_d8a3f1c6e9b2_*.py"))
    spec = importlib.util.spec_from_file_location("haiku_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    executed = []
    migration.op = type("Op", (), {"execute": staticmethod(executed.append)})

    migration.upgrade()
    migration.downgrade()

    (up, down) = executed
    assert "UPDATE chat_settings SET compaction_model_key = :new" in str(up)
    assert "WHERE compaction_model_key = :old" in str(up)
    assert up.compile().params == {"old": "gpt-6-luna", "new": "claude-haiku-5-5"}
    assert down.compile().params == {"old": "claude-haiku-5-5", "new": "gpt-6-luna"}
    assert migration.down_revision == "c5e1a8d3f7b2"


def test_new_databases_compact_web_chat_on_haiku():
    from smarter_dev.web.chat import settings

    assert settings.DEFAULT_MODEL == "gpt-6-luna"
    assert settings.DEFAULT_COMPACTION_MODEL == "claude-haiku-5-5"
