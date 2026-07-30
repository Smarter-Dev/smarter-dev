"""Contract tests for the Discord assistant's answer-writing personality."""

from __future__ import annotations

from pathlib import Path

_PROMPTS = (
    Path("smarter_dev/bot/agents/prompts/chat_agent.md"),
    Path("smarter_dev/bot/agents/prompts/writer_agent.md"),
)


def test_answer_prompts_match_short_discord_messages_and_scale_depth():
    for path in _PROMPTS:
        prompt = path.read_text(encoding="utf-8")
        assert "curious dev friend" in prompt
        assert "1-3 sentences" in prompt
        assert "only add the depth the question warrants" in prompt
        assert "asks why" in prompt


def test_answer_prompts_use_socratic_questions_selectively():
    for path in _PROMPTS:
        prompt = path.read_text(encoding="utf-8")
        assert "ask one sharp question at a time" in prompt
        assert "Don't turn simple questions into interviews" in prompt
        assert "challenge ideas warmly — never the person" in prompt
