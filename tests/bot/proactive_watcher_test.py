from __future__ import annotations

from types import SimpleNamespace

from pydantic_ai.models.test import TestModel

from smarter_dev.bot.proactive.watcher import JevWatcherJudgments
from smarter_dev.bot.proactive.watcher import JevWatcherRunner
from smarter_dev.bot.proactive.watcher import WatcherDecision
from smarter_dev.bot.proactive.watcher import build_jev_watcher_instructions
from smarter_dev.bot.proactive.watcher import build_jev_watcher_material
from smarter_dev.bot.proactive.watcher import decision_from_jev


def _judgments(**overrides) -> JevWatcherJudgments:
    values = {
        "addressed_to_bot_by_name": False,
        "matches_watch_criteria": False,
        "useful_intervention": False,
        "specific_people_exchange": False,
        **overrides,
    }
    return JevWatcherJudgments(**values)


def test_jev_schema_asks_one_described_boolean_per_field() -> None:
    properties = JevWatcherJudgments.model_json_schema()["properties"]

    assert set(properties) == {
        "addressed_to_bot_by_name",
        "matches_watch_criteria",
        "useful_intervention",
        "specific_people_exchange",
    }
    assert all(field["type"] == "boolean" for field in properties.values())
    assert all(field.get("description") for field in properties.values())


def test_jev_material_is_separate_from_dynamic_policy() -> None:
    material = build_jev_watcher_material(
        context_transcript="old context", new_transcript="new material"
    )
    instructions = build_jev_watcher_instructions(
        instructions="WAKE ON RELEASES",
        bot_user_id="B1",
        bot_display_name="helper",
    )

    assert "WAKE ON RELEASES" not in material
    assert "WAKE ON RELEASES" in instructions
    assert "new material" in material
    assert "new material" not in instructions


def test_jev_decision_uses_deterministic_message_selection_and_metadata() -> None:
    decision = decision_from_jev(
        _judgments(useful_intervention=True),
        new_message_ids=["m1", "m2"],
        provider_details={
            "confidence": {"useful_intervention": 0.9},
            "probabilities": {"useful_intervention": {"true": 0.95}},
        },
        resolved_model_name="jev-1.13.0",
        minimum_confidence=0.2,
    )

    assert decision.wake is True
    assert decision.relevant_message_ids == ["m1", "m2"]
    assert decision.summary == "Review the complete newly classified message burst."
    assert decision.details()["classifier"]["resolved_model_name"] == "jev-1.13.0"
    assert decision.details()["classifier"]["confidence"] == {
        "useful_intervention": 0.9
    }


def test_jev_does_not_interrupt_a_specific_people_exchange() -> None:
    decision = decision_from_jev(
        _judgments(useful_intervention=True, specific_people_exchange=True),
        new_message_ids=["m1"],
        provider_details={"confidence": {"useful_intervention": 0.8}},
        resolved_model_name="jev-1.13.0",
        minimum_confidence=0.2,
    )

    assert decision.wake is False
    assert decision.relevant_message_ids == []


def test_jev_low_confidence_abstains_without_fallback() -> None:
    decision = decision_from_jev(
        _judgments(matches_watch_criteria=True),
        new_message_ids=["m1"],
        provider_details={"confidence": {"matches_watch_criteria": 0.1}},
        resolved_model_name="jev-1.13.0",
        minimum_confidence=0.2,
    )

    assert decision.wake is False
    assert decision.details()["classifier"]["abstained"] is True


class _FailingAgent:
    async def run(self, *args, **kwargs):
        raise RuntimeError("provider body must not leak")


class _Fallback:
    async def decide(self, **kwargs):
        return WatcherDecision(wake=True, reason="fallback"), {
            "input_tokens": 7,
            "output_tokens": 2,
            "cache_read_tokens": 0,
        }


async def test_jev_api_failure_uses_configured_fallback_without_error_text() -> None:
    runner = JevWatcherRunner(
        TestModel(),
        fallback=_Fallback(),
        fallback_model_id="z-ai/glm-5.3-flash",
    )
    runner._agent = _FailingAgent()

    decision, usage = await runner.decide(
        instructions="policy",
        context_transcript="context",
        new_transcript="new",
        bot_user_id="B1",
        new_message_ids=["m1"],
    )

    assert decision.wake is True
    metadata = decision.details()["classifier"]
    assert metadata["failure"] == "RuntimeError"
    assert "provider body" not in str(metadata)
    assert usage["usage_by_model"]["z-ai/glm-5.3-flash"]["input_tokens"] == 7


class _SuccessfulAgent:
    async def run(self, *args, **kwargs):
        return SimpleNamespace(
            output=_judgments(matches_watch_criteria=True),
            response=SimpleNamespace(
                provider_details={
                    "confidence": {"matches_watch_criteria": 0.1}
                },
                model_name="jev-1.13.0",
            ),
            usage=SimpleNamespace(
                input_tokens=11,
                output_tokens=4,
                cache_read_tokens=0,
            ),
        )


async def test_jev_low_confidence_fallback_accounts_for_both_calls() -> None:
    runner = JevWatcherRunner(
        TestModel(),
        model_id="typesafe:jev-latest",
        fallback=_Fallback(),
        fallback_model_id="z-ai/glm-5.3-flash",
        minimum_confidence=0.2,
    )
    runner._agent = _SuccessfulAgent()

    decision, usage = await runner.decide(
        instructions="policy",
        context_transcript="context",
        new_transcript="new",
        bot_user_id="B1",
        new_message_ids=["m1"],
    )

    assert decision.details()["classifier"]["fallback_used"] is True
    assert usage["usage_by_model"]["typesafe:jev-latest"]["input_tokens"] == 11
    assert usage["usage_by_model"]["z-ai/glm-5.3-flash"]["input_tokens"] == 7
