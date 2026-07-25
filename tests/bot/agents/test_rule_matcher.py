"""Tests for the ``/rule`` rule-matcher agent.

No live API calls. The pure pieces — candidate selection, prompt construction
and citation validation — are exercised directly; the one end-to-end shape test
drives the real agent through pydantic_ai's ``TestModel`` via ``agent.override``.
"""

from __future__ import annotations

import pytest
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from smarter_dev.bot.agents import rule_matcher
from smarter_dev.bot.agents.rule_matcher import RejectedCitation
from smarter_dev.bot.agents.rule_matcher import RuleCitation
from smarter_dev.bot.agents.rule_matcher import RuleMatchMessage
from smarter_dev.bot.agents.rule_matcher import ValidatedCitation
from smarter_dev.bot.agents.rule_matcher import build_rule_match_prompt
from smarter_dev.bot.agents.rule_matcher import candidate_target_ids
from smarter_dev.bot.agents.rule_matcher import match_rule_violation
from smarter_dev.bot.agents.rule_matcher import validate_rule_citation
from smarter_dev.web.guild_rules import parse_guild_rules

_RULES_MARKDOWN = """\
## Be civil
No personal attacks, harassment, or slurs.

## No self-promotion
Do not advertise your own product without asking a moderator first.
"""


@pytest.fixture(autouse=True)
def _offline_rule_matcher(monkeypatch):
    # The Google SDK wants a key to construct the model; supply a dummy one.
    # Every test that reaches the model swaps it out via ``agent.override``, so
    # the key is never used for a real request. The module-level singleton is
    # reset around each test so no override leaks between tests.
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    rule_matcher._rule_matcher_agent = None
    yield
    rule_matcher._rule_matcher_agent = None


def _rules():
    return parse_guild_rules(_RULES_MARKDOWN)


def _messages() -> list[RuleMatchMessage]:
    return [
        RuleMatchMessage(
            message_id="100",
            author_id="alice_id",
            author_display="alice",
            content="anyone used the new profiler?",
        ),
        RuleMatchMessage(
            message_id="101",
            author_id="bob_id",
            author_display="bob",
            content="buy my course at example.com, 50% off today",
        ),
        RuleMatchMessage(
            message_id="102",
            author_id="alice_id",
            author_display="alice",
            content="please don't spam here",
        ),
    ]


class TestCandidateTargetIds:
    def test_all_messages_are_candidates_without_a_target_user(self):
        assert candidate_target_ids(_messages()) == ("100", "101", "102")

    def test_only_the_target_users_messages_are_candidates(self):
        assert candidate_target_ids(_messages(), target_author_id="alice_id") == (
            "100",
            "102",
        )

    def test_unknown_target_user_has_no_candidates(self):
        assert candidate_target_ids(_messages(), target_author_id="nobody") == ()

    def test_the_excluded_authors_messages_are_never_candidates(self):
        assert candidate_target_ids(_messages(), excluded_author_id="bob_id") == (
            "100",
            "102",
        )

    def test_exclusion_applies_on_top_of_a_named_target_user(self):
        assert (
            candidate_target_ids(
                _messages(), target_author_id="bob_id", excluded_author_id="bob_id"
            )
            == ()
        )

    def test_does_not_mutate_the_messages_it_is_given(self):
        messages = _messages()
        candidate_target_ids(messages, target_author_id="bob_id")
        assert messages == _messages()


class TestBuildRuleMatchPrompt:
    def test_includes_every_rule_with_its_index_title_and_body(self):
        prompt = build_rule_match_prompt(
            "bob is advertising his course",
            _rules(),
            _messages(),
        )
        assert "1. Be civil" in prompt
        assert "No personal attacks, harassment, or slurs." in prompt
        assert "2. No self-promotion" in prompt
        assert "Do not advertise your own product" in prompt

    def test_includes_the_moderator_description(self):
        prompt = build_rule_match_prompt(
            "bob is advertising his course",
            _rules(),
            _messages(),
        )
        assert "bob is advertising his course" in prompt

    def test_includes_every_message_id_author_and_content(self):
        prompt = build_rule_match_prompt("spam", _rules(), _messages())
        for message in _messages():
            assert message.message_id in prompt
            assert message.author_display in prompt
            assert message.content in prompt

    def test_context_messages_remain_when_a_target_user_narrows_candidates(self):
        prompt = build_rule_match_prompt(
            "he is advertising",
            _rules(),
            _messages(),
            target_author_id="bob_id",
        )
        # Only bob's message may be targeted...
        candidate_section = prompt.split("CANDIDATE MESSAGES")[1]
        assert "101" in candidate_section
        assert "100" not in candidate_section
        # ...but alice's messages are still present as conversation context.
        assert "anyone used the new profiler?" in prompt
        assert "please don't spam here" in prompt

    def test_every_message_is_a_candidate_without_a_target_user(self):
        prompt = build_rule_match_prompt("spam", _rules(), _messages())
        candidate_section = prompt.split("CANDIDATE MESSAGES")[1]
        for message_id in ("100", "101", "102"):
            assert message_id in candidate_section

    def test_excluded_author_messages_are_context_but_never_candidates(self):
        """The prompt must not offer ids the caller's validation always refuses."""
        prompt = build_rule_match_prompt(
            "someone is advertising",
            _rules(),
            _messages(),
            excluded_author_id="bob_id",
        )
        candidate_section = prompt.split("CANDIDATE MESSAGES")[1]
        assert "101" not in candidate_section
        assert "100" in candidate_section
        # bob's message is still there as conversation context.
        assert "buy my course at example.com, 50% off today" in prompt

    def test_says_so_when_every_message_is_excluded(self):
        prompt = build_rule_match_prompt(
            "the bot was rude",
            _rules(),
            [_messages()[1]],
            excluded_author_id="bob_id",
        )
        candidate_section = prompt.split("CANDIDATE MESSAGES")[1]
        assert "none" in candidate_section
        assert "101" not in candidate_section

    def test_says_so_when_the_named_user_has_no_messages(self):
        prompt = build_rule_match_prompt(
            "he was rude",
            _rules(),
            _messages(),
            target_author_id="nobody",
        )
        candidate_section = prompt.split("CANDIDATE MESSAGES")[1]
        assert "none" in candidate_section
        for message_id in ("100", "101", "102"):
            assert message_id not in candidate_section

    def test_is_pure(self):
        rules, messages = _rules(), _messages()
        build_rule_match_prompt("spam", rules, messages, target_author_id="bob_id")
        assert rules == _rules()
        assert messages == _messages()

    def test_raises_when_there_are_no_rules(self):
        with pytest.raises(ValueError, match="no rules"):
            build_rule_match_prompt("spam", [], _messages())

    def test_raises_when_there_are_no_messages(self):
        with pytest.raises(ValueError, match="no messages"):
            build_rule_match_prompt("spam", _rules(), [])


class TestValidateRuleCitation:
    def test_accepts_a_citation_naming_known_rules_and_a_candidate_message(self):
        citation = RuleCitation(
            matched_rule_indices=[2],
            target_message_id="101",
            explanation="  advertising a paid course unprompted  ",
        )
        result = validate_rule_citation(citation, {1, 2}, {"100", "101"})
        assert result == ValidatedCitation(
            matched_rule_indices=(2,),
            target_message_id="101",
            explanation="advertising a paid course unprompted",
        )

    def test_accepts_an_empty_match_list_as_a_valid_no_match(self):
        citation = RuleCitation(
            matched_rule_indices=[],
            target_message_id="101",
            explanation="nothing here breaks a rule",
        )
        result = validate_rule_citation(citation, {1, 2}, {"101"})
        assert isinstance(result, ValidatedCitation)
        assert result.matched_rule_indices == ()

    def test_dedupes_repeated_indices_keeping_first_appearance_order(self):
        citation = RuleCitation(
            matched_rule_indices=[2, 1, 2, 1],
            target_message_id="101",
            explanation="both apply",
        )
        result = validate_rule_citation(citation, {1, 2}, {"101"})
        assert isinstance(result, ValidatedCitation)
        assert result.matched_rule_indices == (2, 1)

    def test_rejects_an_invented_message_id(self):
        citation = RuleCitation(
            matched_rule_indices=[1],
            target_message_id="999",
            explanation="whatever",
        )
        result = validate_rule_citation(citation, {1, 2}, {"100", "101"})
        assert isinstance(result, RejectedCitation)
        assert "999" in result.reason

    def test_rejects_an_empty_message_id(self):
        citation = RuleCitation(
            matched_rule_indices=[1],
            target_message_id="",
            explanation="whatever",
        )
        result = validate_rule_citation(citation, {1, 2}, {"100"})
        assert isinstance(result, RejectedCitation)

    def test_rejects_a_message_id_that_is_context_but_not_a_candidate(self):
        citation = RuleCitation(
            matched_rule_indices=[1],
            target_message_id="100",
            explanation="whatever",
        )
        # Only bob's message is a candidate target; alice's "100" is context.
        result = validate_rule_citation(citation, {1, 2}, {"101"})
        assert isinstance(result, RejectedCitation)
        assert "100" in result.reason

    def test_rejects_an_out_of_range_rule_index(self):
        citation = RuleCitation(
            matched_rule_indices=[1, 7],
            target_message_id="101",
            explanation="whatever",
        )
        result = validate_rule_citation(citation, {1, 2}, {"101"})
        assert isinstance(result, RejectedCitation)
        assert "7" in result.reason

    def test_rejects_rule_index_zero(self):
        citation = RuleCitation(
            matched_rule_indices=[0],
            target_message_id="101",
            explanation="whatever",
        )
        result = validate_rule_citation(citation, {1, 2}, {"101"})
        assert isinstance(result, RejectedCitation)
        assert "0" in result.reason

    def test_rejects_a_negative_rule_index(self):
        citation = RuleCitation(
            matched_rule_indices=[-1],
            target_message_id="101",
            explanation="whatever",
        )
        result = validate_rule_citation(citation, {1, 2}, {"101"})
        assert isinstance(result, RejectedCitation)
        assert "-1" in result.reason

    def test_rejects_a_matched_citation_with_a_blank_explanation(self):
        citation = RuleCitation(
            matched_rule_indices=[1],
            target_message_id="101",
            explanation="   ",
        )
        result = validate_rule_citation(citation, {1, 2}, {"101"})
        assert isinstance(result, RejectedCitation)
        assert "explanation" in result.reason

    def test_accepts_indices_from_the_real_parsed_rules(self):
        rules = _rules()
        citation = RuleCitation(
            matched_rule_indices=[rule.index for rule in rules],
            target_message_id="101",
            explanation="both",
        )
        result = validate_rule_citation(
            citation, {rule.index for rule in rules}, {"101"}
        )
        assert isinstance(result, ValidatedCitation)
        assert result.matched_rule_indices == (1, 2)

    def test_is_pure(self):
        citation = RuleCitation(
            matched_rule_indices=[2, 2],
            target_message_id="101",
            explanation="x",
        )
        valid_indices = {1, 2}
        candidates = {"101"}
        validate_rule_citation(citation, valid_indices, candidates)
        assert citation.matched_rule_indices == [2, 2]
        assert valid_indices == {1, 2}
        assert candidates == {"101"}


class TestMatchRuleViolation:
    async def test_returns_the_models_raw_structured_output(self):
        agent = rule_matcher.get_rule_matcher_agent()
        output = {
            "matched_rule_indices": [2],
            "target_message_id": "101",
            "explanation": "unprompted advertising",
        }
        with agent.override(model=TestModel(custom_output_args=output)):
            citation = await match_rule_violation(
                "bob is advertising", _rules(), _messages()
            )
        assert isinstance(citation, RuleCitation)
        assert citation.matched_rule_indices == [2]
        assert citation.target_message_id == "101"
        assert citation.explanation == "unprompted advertising"

    async def test_returns_an_unvalidated_hallucination_for_the_caller_to_reject(self):
        agent = rule_matcher.get_rule_matcher_agent()
        output = {
            "matched_rule_indices": [9],
            "target_message_id": "does-not-exist",
            "explanation": "made up",
        }
        with agent.override(model=TestModel(custom_output_args=output)):
            citation = await match_rule_violation("spam", _rules(), _messages())
        # The agent hands back exactly what the model said; validation is the
        # caller's step, and it must reject this.
        assert citation.target_message_id == "does-not-exist"
        rejected = validate_rule_citation(
            citation,
            {rule.index for rule in _rules()},
            set(candidate_target_ids(_messages())),
        )
        assert isinstance(rejected, RejectedCitation)

    async def test_forwards_the_excluded_author_to_the_prompt(self, monkeypatch):
        captured: dict[str, str | None] = {}
        real_builder = rule_matcher.build_rule_match_prompt

        def capturing_builder(
            moderator_description,
            rules,
            messages,
            target_author_id=None,
            excluded_author_id=None,
        ):
            captured["excluded_author_id"] = excluded_author_id
            return real_builder(
                moderator_description,
                rules,
                messages,
                target_author_id,
                excluded_author_id,
            )

        monkeypatch.setattr(rule_matcher, "build_rule_match_prompt", capturing_builder)
        agent = rule_matcher.get_rule_matcher_agent()
        output = {
            "matched_rule_indices": [2],
            "target_message_id": "101",
            "explanation": "unprompted advertising",
        }
        with agent.override(model=TestModel(custom_output_args=output)):
            await match_rule_violation(
                "spam", _rules(), _messages(), excluded_author_id="alice_id"
            )

        assert captured["excluded_author_id"] == "alice_id"

    async def test_model_failure_propagates(self):
        def _boom(messages, info: AgentInfo):
            raise RuntimeError("gemini outage")

        agent = rule_matcher.get_rule_matcher_agent()
        with agent.override(model=FunctionModel(_boom)):
            with pytest.raises(RuntimeError, match="gemini outage"):
                await match_rule_violation("spam", _rules(), _messages())

    async def test_refuses_to_call_the_model_without_rules(self):
        with pytest.raises(ValueError, match="no rules"):
            await match_rule_violation("spam", [], _messages())

    async def test_refuses_to_call_the_model_without_messages(self):
        with pytest.raises(ValueError, match="no messages"):
            await match_rule_violation("spam", _rules(), [])


class TestAgentConstruction:
    def test_uses_a_catalog_model_so_spend_is_attributed(self):
        from smarter_dev.shared.model_catalog import get_model

        assert get_model(rule_matcher.RULE_MATCHER_MODEL_KEY) is not None

    def test_is_a_singleton(self):
        assert rule_matcher.get_rule_matcher_agent() is (
            rule_matcher.get_rule_matcher_agent()
        )

    def test_system_prompt_forbids_quoting_rule_text(self):
        prompt = rule_matcher.SYSTEM_PROMPT.lower()
        assert "index" in prompt
        assert "quote" in prompt or "paraphrase" in prompt
