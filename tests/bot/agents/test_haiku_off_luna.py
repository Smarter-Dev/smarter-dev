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
    tokens = {
        "input_tokens": 10 + 300 + 900,
        "output_tokens": 5,
        "cache_read_tokens": 300,
        "cache_write_tokens": 900,
    }
    # The one request's own counts ride along, for per-request pricing.
    assert usage == {
        "model": "claude-haiku-5-5",
        "requests": 1,
        **tokens,
        "request_tokens": [tokens],
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


async def test_a_discord_fold_sends_each_summarizer_requests_counts(monkeypatch):
    from smarter_dev.bot.agents import chat_compaction
    from smarter_dev.bot.services import chat_conversation_persistence

    monkeypatch.setattr(chat_compaction, "_summarizer_agent", None)
    monkeypatch.delenv(chat_compaction.COMPACT_MODEL_ENV_VAR, raising=False)
    serve(monkeypatch, text_reply("They chose hash partitioning.", cache_read=40))

    summary = await chat_compaction._summarise_conversation("[user_input]\nhi")

    assert summary.request_tokens == [
        {
            "input_tokens": 50,
            "output_tokens": 5,
            "cache_read_tokens": 40,
            "cache_write_tokens": 0,
        }
    ]
    posted = {}

    class Client:
        async def post(self, path, json_data):
            posted.update(json_data)
            return type("Response", (), {"status_code": 201})()

    monkeypatch.setattr(
        chat_conversation_persistence, "_get_api_client", lambda bot: Client()
    )
    event = chat_compaction.CompactionEvent(
        event_kind="conversation",
        tool_name=None,
        original_content="",
        summary=summary.text,
        original_chars=0,
        summary_chars=len(summary.text),
        summarizer_tokens_input=summary.tokens_input,
        summarizer_tokens_output=summary.tokens_output,
        summarizer_model_name=summary.model_name,
        summarizer_reasoning_level="low",
        summarizer_request_tokens=summary.request_tokens,
    )
    await chat_conversation_persistence.persist_turn(
        bot=None,
        engagement_id="00000000-0000-0000-0000-000000000001",
        request_id="r",
        turn_kind="initial",
        output_kind="no_response",
        triggering_messages=[],
        agent_output={},
        new_model_messages=[],
        duration_ms=None,
        chat_tokens_input=0,
        chat_tokens_output=0,
        chat_model_name=None,
        compaction_events=[event],
    )
    (sent,) = posted["compaction_events"]
    assert sent["summarizer_request_tokens"] == summary.request_tokens


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


def _migration():
    path = next(Path("alembic/main/versions").glob("*_d8a3f1c6e9b2_*.py"))
    spec = importlib.util.spec_from_file_location("haiku_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "c5e1a8d3f7b2"
    return migration


@pytest.mark.parametrize(
    ("before", "after_upgrade"),
    [
        ("gpt-6-luna", "claude-haiku-5-5"),
        ("gemini-3.8-flash", "gemini-3.8-flash"),
        # Chosen before the upgrade: the downgrade must not turn it into Luna.
        ("claude-haiku-5-5", "claude-haiku-5-5"),
    ],
)
def test_the_migration_round_trip_restores_every_setting(
    tmp_path, before, after_upgrade
):
    from sqlalchemy import create_engine
    from sqlalchemy import text

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE chat_settings (id INTEGER PRIMARY KEY, "
                "compaction_model_key VARCHAR(100) NOT NULL)"
            )
        )
        connection.execute(
            text("INSERT INTO chat_settings VALUES (1, :key)"), {"key": before}
        )
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))

        def setting():
            return connection.execute(
                text("SELECT compaction_model_key FROM chat_settings")
            ).scalar_one()

        migration.upgrade()
        assert setting() == after_upgrade
        migration.downgrade()
        assert setting() == before
        # The marker table goes with the downgrade.
        assert "migration_d8a3f1c6e9b2_moved_settings" not in {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }


def test_a_downgrade_keeps_a_choice_made_after_the_upgrade(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy import text

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE chat_settings (id INTEGER PRIMARY KEY, "
                "compaction_model_key VARCHAR(100) NOT NULL)"
            )
        )
        connection.execute(text("INSERT INTO chat_settings VALUES (1, 'gpt-6-luna')"))
        migration = _migration()
        migration.op = Operations(MigrationContext.configure(connection))

        migration.upgrade()
        connection.execute(
            text("UPDATE chat_settings SET compaction_model_key = 'deepseek-v4'")
        )
        migration.downgrade()

        assert (
            connection.execute(
                text("SELECT compaction_model_key FROM chat_settings")
            ).scalar_one()
            == "deepseek-v4"
        )


def test_new_databases_compact_web_chat_on_haiku():
    from smarter_dev.web.chat import settings

    assert settings.DEFAULT_MODEL == "gpt-6-luna"
    assert settings.DEFAULT_COMPACTION_MODEL == "claude-haiku-5-5"


# -- one-request agents cache no messages ------------------------------------


def test_one_request_agents_on_claude_drop_the_message_breakpoint(monkeypatch):
    """Each answers in one request (bar a rare retry), so a message breakpoint
    would only bill its prompt at the 1-hour write rate."""
    from smarter_dev.bot.agents import rule_matcher
    from smarter_dev.web import chat_memory_dream
    from smarter_dev.web import chat_memory_purge

    monkeypatch.setattr(rule_matcher, "RULE_MATCHER_MODEL_KEY", "claude-haiku-5-5")
    monkeypatch.setattr(rule_matcher, "_rule_matcher_agent", None)
    monkeypatch.setenv(chat_memory_dream.DREAM_MODEL_ENV_VAR, "claude-haiku-5-5")
    monkeypatch.setattr(chat_memory_dream, "_dream_agent", None)
    monkeypatch.setattr(chat_memory_purge, "_purge_agent", None)

    for agent in (
        rule_matcher.get_rule_matcher_agent(),
        chat_memory_dream.get_dream_agent(),
        chat_memory_purge.get_purge_agent(),
    ):
        assert agent.model_settings["anthropic_cache_messages"] is False
