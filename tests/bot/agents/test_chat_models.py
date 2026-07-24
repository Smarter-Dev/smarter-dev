"""Tests for the two-stage chat models: WriterBrief, BriefingDecision, WriterOutput.

These mirror TurnDecision's orchestration invariants (non-empty rankings, the
"stay silent when the top ranking scored < 5" rule, and English-redirect
parity) for the WORKER stage, plus the WRITER stage's output shape.
"""

from __future__ import annotations

import pytest

from smarter_dev.bot.agents.chat_models import (
    BriefingDecision,
    MessageScore,
    WriterBrief,
    WriterOutput,
)


def _brief(
    *,
    message_summaries: list[str] | None = None,
    search_findings: list[str] | None = None,
    questions: list[str] | None = None,
    response_language: str = "english",
    send_voice: bool = False,
    reply_directly: bool = False,
) -> WriterBrief:
    return WriterBrief(
        message_summaries=(
            ["Alice asked whether async generators leak"]
            if message_summaries is None
            else message_summaries
        ),
        search_findings=[] if search_findings is None else search_findings,
        questions=(
            ["Do async generators leak memory?"]
            if questions is None
            else questions
        ),
        response_language=response_language,
        send_voice=send_voice,
        reply_directly=reply_directly,
    )


# --------------------------------------------------------------------------- #
# WriterBrief
# --------------------------------------------------------------------------- #


def test_writer_brief_valid():
    brief = _brief(
        search_findings=["The Python docs confirm generators are GC'd normally"],
    )
    assert brief.message_summaries == ["Alice asked whether async generators leak"]
    assert brief.search_findings == [
        "The Python docs confirm generators are GC'd normally"
    ]
    assert brief.questions == ["Do async generators leak memory?"]
    assert brief.response_language == "english"
    assert brief.send_voice is False
    assert brief.reply_directly is False


def test_writer_brief_search_findings_may_be_empty():
    brief = _brief(search_findings=[])
    assert brief.search_findings == []


def test_writer_brief_normalizes_response_language():
    brief = _brief(response_language="  Spanish ")
    assert brief.response_language == "spanish"


def test_writer_brief_rejects_empty_response_language():
    with pytest.raises(ValueError):
        _brief(response_language="   ")


def test_writer_brief_rejects_entirely_empty_content():
    """A populated brief must carry at least one summary/finding/question."""
    with pytest.raises(ValueError):
        _brief(message_summaries=[], search_findings=[], questions=[])


def test_writer_brief_non_english_redirect_must_be_text_only():
    """Parity with TurnDecision's 'English redirect must be text-only' rule:
    a non-English turn may not also request a voice message."""
    with pytest.raises(ValueError):
        _brief(response_language="french", send_voice=True)


def test_writer_brief_english_allows_voice():
    brief = _brief(response_language="english", send_voice=True)
    assert brief.send_voice is True


# --------------------------------------------------------------------------- #
# BriefingDecision
# --------------------------------------------------------------------------- #


def test_briefing_decision_valid_with_brief():
    decision = BriefingDecision(
        rankings=[MessageScore(message_id="1", score=9, reasoning="direct mention")],
        brief=_brief(),
        continue_watching=True,
        topic="async generators",
        notes="alice: worried about leaks",
    )
    assert decision.brief is not None
    assert decision.brief.response_language == "english"


def test_briefing_decision_silent_brief_none():
    decision = BriefingDecision(
        rankings=[MessageScore(message_id="1", score=2, reasoning="bystander")],
        brief=None,
        continue_watching=True,
        topic="chit chat",
    )
    assert decision.brief is None


def test_briefing_decision_requires_non_empty_rankings():
    with pytest.raises(ValueError):
        BriefingDecision(
            rankings=[],
            brief=None,
            continue_watching=True,
            topic="x",
        )


def test_briefing_decision_rejects_brief_when_top_score_below_five():
    """Mirror TurnDecision: must stay silent (brief=None) when max ranking < 5."""
    with pytest.raises(ValueError):
        BriefingDecision(
            rankings=[MessageScore(message_id="1", score=4, reasoning="not for me")],
            brief=_brief(),
            continue_watching=True,
            topic="x",
        )


def test_briefing_decision_allows_brief_at_exactly_five():
    decision = BriefingDecision(
        rankings=[MessageScore(message_id="1", score=5, reasoning="borderline")],
        brief=_brief(),
        continue_watching=True,
        topic="x",
    )
    assert decision.brief is not None


# --------------------------------------------------------------------------- #
# WriterOutput
# --------------------------------------------------------------------------- #


def test_writer_output_with_voice_summary():
    output = WriterOutput(
        message="Async generators are cleaned up like any other object.",
        voice_summary="Async generators get garbage collected normally.",
    )
    assert output.message.startswith("Async generators")
    assert output.voice_summary is not None


def test_writer_output_without_voice_summary():
    output = WriterOutput(message="They're garbage collected normally.")
    assert output.voice_summary is None


def test_writer_output_rejects_empty_message():
    with pytest.raises(ValueError):
        WriterOutput(message="   ")
