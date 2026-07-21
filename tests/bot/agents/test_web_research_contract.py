"""Tests for the chat agent's search-to-read research contract."""

from smarter_dev.bot.agents.chat_agent import SYSTEM_PROMPT
from smarter_dev.bot.agents.chat_tools import web_read
from smarter_dev.bot.agents.chat_tools import web_search


def test_system_prompt_distinguishes_search_from_reading():
    assert "Search is the discovery step" in SYSTEM_PROMPT
    assert "Reading is the evidence step" in SYSTEM_PROMPT
    assert "quick, low-stakes answer" in SYSTEM_PROMPT
    assert "For an accurate or deep answer" in SYSTEM_PROMPT


def test_web_search_description_requires_reading_for_deep_answers():
    description = web_search.__doc__ or ""
    assert "first stage of web research" in description
    assert "Snippets are enough for quick" in description
    assert "call ``web_read``" in description
    assert "before replying" in description


def test_web_read_description_identifies_evidence_stage():
    description = web_read.__doc__ or ""
    assert "evidence stage after ``web_search``" in description
    assert "accurate, deep, precise, nuanced, verified" in description
