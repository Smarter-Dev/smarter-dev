"""Tests for the chat agent's search-to-read research contract."""

from smarter_dev.bot.agents.chat_agent import SYSTEM_PROMPT
from smarter_dev.bot.agents.chat_models import TurnDecision
from smarter_dev.bot.agents.chat_tools import web_read
from smarter_dev.bot.agents.chat_tools import web_search


def test_chat_agent_has_no_blog_functionality():
    assert "Smarter Dev blog" not in SYSTEM_PROMPT
    assert "blog_topic_candidates" not in SYSTEM_PROMPT
    assert "blog_topic_candidates" not in TurnDecision.model_fields


def test_system_prompt_distinguishes_search_from_reading():
    assert "`web_search` discovers snippets" in SYSTEM_PROMPT
    assert "for an accurate or deep answer" in SYSTEM_PROMPT
    assert "`web_read` the best result before replying" in SYSTEM_PROMPT


def test_web_search_description_requires_reading_for_deep_answers():
    description = web_search.__doc__ or ""
    assert "result snippets" in description
    assert "accurate or deep answers" in description
    assert "web_read" in description
    assert "best result" in description


def test_web_read_description_guides_the_summary_with_an_instruction():
    description = web_read.__doc__ or ""
    assert "Read a URL" in description
    assert "message <attachment>" in description
    assert "summary guided by `instruction`" in description
