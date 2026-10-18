"""Moderation triage on pydantic-ai, driven by a scripted model.

The real path runs end to end: ``run_moderation_agent`` builds the triage
tools, the agent calls them, and the tracker and report come out the other
side. Only the model is scripted.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.messages import ModelResponse
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.models.function import FunctionModel

from smarter_dev.bot.agents import mod_tools
from smarter_dev.bot.agents import moderation_agent
from smarter_dev.bot.agents.mod_tools import ActionTracker
from smarter_dev.bot.agents.mod_tools import build_triage_report_embed


@pytest.fixture(autouse=True)
def _model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # The real model is built before the test swaps in its FunctionModel.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")


def _prompt(messages: list[ModelMessage]) -> str:
    return "\n".join(
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    )


def _tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _scripted(*steps: list[tuple[str, dict]], seen: list | None = None) -> FunctionModel:
    """Each step is the tool calls for one model turn; the last turn is the output."""
    turns = list(steps)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append((messages, info))
        calls = turns.pop(0)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name if name == "final" else name, args
                )
                for name, args in calls
            ]
        )

    return FunctionModel(respond)


def _failing(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise RuntimeError("boom: provider internals")


async def _triage(model: FunctionModel, follow_up: FunctionModel | None = None, **kw):
    kwargs = {
        "bot": MagicMock(),
        "guild_id": "1",
        "channel_id": "2",
        "trigger_message_content": "@mods can someone look at this?",
        "trigger_author": "reporter",
        "context_messages": [],
        "guild_instructions": "",
        "enabled_tools": [],
        "trigger_message_id": "3",
        **kw,
    }
    with (
        moderation_agent.get_moderation_agent().override(model=model),
        moderation_agent.get_follow_up_agent().override(
            model=follow_up or FunctionModel(_failing)
        ),
    ):
        return await moderation_agent.run_moderation_agent(**kwargs)


def _field_names(embed) -> list[str]:
    return [f.name for f in embed.fields]


# -- the run ---------------------------------------------------------------------


async def test_triage_sends_its_inputs_and_offers_the_triage_tools() -> None:
    seen: list = []
    model = _scripted(
        [("final", {"assessment": "Friendly banter, nothing to act on."})], seen=seen
    )

    assessment, tracker = await _triage(model)

    assert tracker.failure is None
    assert assessment == "Friendly banter, nothing to act on."
    messages, info = seen[0]
    prompt = _prompt(messages)
    assert "## trigger_message\n@mods can someone look at this?" in prompt
    assert "## trigger_author\nreporter" in prompt
    assert "## guild_instructions\nUse your best judgment to moderate fairly." in prompt
    # An empty enabled_tools means the defaults: timeout and purge, plus utilities.
    assert {tool.name for tool in info.function_tools} == {
        "timeout_user",
        "purge_messages",
        "flag_users",
        "send_mod_message",
        "get_user_info",
        "get_user_history",
    }


async def test_the_tool_schemas_carry_the_docstring_argument_descriptions() -> None:
    seen: list = []

    await _triage(
        _scripted([("final", {"assessment": "ok"})], seen=seen),
        enabled_tools=["timeout", "purge", "delete"],
    )

    tools = {tool.name: tool for tool in seen[0][1].function_tools}
    assert set(tools) >= {"timeout_user", "purge_messages", "delete_message"}
    duration = tools["timeout_user"].parameters_json_schema["properties"]["duration"]
    assert duration["description"] == "Duration string like '10m', '30m', '1h'"
    assert "Prefer short durations" in tools["timeout_user"].description


async def test_tool_calls_land_in_the_tracker() -> None:
    seen: list = []
    model = _scripted(
        [
            ("flag_users", {"user_ids": "42, 43"}),
            ("send_mod_message", {"message": "Please keep it civil."}),
        ],
        [("final", {"assessment": "Heated; flagged both."})],
        seen=seen,
    )

    assessment, tracker = await _triage(model)

    assert assessment == "Heated; flagged both."
    assert tracker.flags == ["42", "43"]
    assert tracker.channel_message == "Please keep it civil."
    returned = [part.content for part in _tool_returns(seen[1][0])]
    assert {"success": True, "result": "Flagged 2 user(s) for moderator review."} in returned


async def test_actions_without_a_channel_message_get_a_follow_up() -> None:
    model = _scripted(
        [("flag_users", {"user_ids": "42"})],
        [("final", {"assessment": "Flagged one user."})],
    )
    follow_up_seen: list = []
    follow_up = _scripted(
        [("final", {"message": " Please stop; a moderator is on the way. "})],
        seen=follow_up_seen,
    )

    _, tracker = await _triage(model, follow_up)

    assert tracker.channel_message == "Please stop; a moderator is on the way."
    prompt = _prompt(follow_up_seen[0][0])
    assert "## actions_summary\nActions were taken." in prompt
    assert "## assessment\nFlagged one user." in prompt


async def test_a_failed_follow_up_posts_the_generic_notice() -> None:
    model = _scripted(
        [("flag_users", {"user_ids": "42"})],
        [("final", {"assessment": "Flagged one user."})],
    )

    _, tracker = await _triage(model, FunctionModel(_failing))

    assert tracker.channel_message == (
        "Moderation action has been taken. A human moderator will review "
        "the situation shortly."
    )


async def test_a_runaway_triage_stops_at_the_request_limit() -> None:
    def forever(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("get_user_history", {"user_id": "42"})])

    _, tracker = await _triage(FunctionModel(forever))

    assert tracker.failure == "UsageLimitExceeded"


def test_triage_runs_on_claude_haiku_5_5_through_the_router() -> None:
    triage = moderation_agent.get_moderation_agent()
    follow_up = moderation_agent.get_follow_up_agent()
    for agent in (triage, follow_up):
        assert isinstance(agent.model, AnthropicModel)
        assert agent.model.model_name == "claude-haiku-5-5"
    # Triage loops over its tools, so its requests read the last one's history
    # back; the follow-up is a single request and skips the message breakpoint.
    assert triage.model_settings.get("anthropic_cache_messages") is None
    assert follow_up.model_settings["anthropic_cache_messages"] is False


# -- failure reporting -----------------------------------------------------------


async def test_a_failed_triage_reports_manual_review_not_no_action() -> None:
    assessment, tracker = await _triage(FunctionModel(_failing))

    assert tracker.failure == "RuntimeError"
    assert not tracker.has_actions
    assert "boom" not in assessment
    embed = build_triage_report_embed(tracker, assessment, "reporter", "2", "3")
    assert embed.title == "Moderation Triage Failed"
    assert "No Action Taken" not in _field_names(embed)
    [status] = [f for f in embed.fields if f.name == "Manual Review Required"]
    assert "not reviewed" in status.value
    assert "no immediate action required" not in json.dumps([f.value for f in embed.fields])


@pytest.mark.parametrize("notice", [None, "A moderator will be here shortly."])
def test_a_triage_failing_after_an_action_keeps_the_action_in_the_report(notice):
    # The tracker a failed run returns still holds what executed before the
    # failure; the report lists it and says whether the channel was told.
    tracker = ActionTracker(flags=["42"], channel_message=notice, failure="BadRequestError")
    embed = build_triage_report_embed(
        tracker, "Moderation triage failed (BadRequestError).", "reporter", "2", "3"
    )
    names = _field_names(embed)
    assert embed.title == "Moderation Triage Failed"
    assert "Flagged for Review" in names
    assert "No Action Taken" not in names
    [status] = [f for f in embed.fields if f.name == "Manual Review Required"]
    assert "already taken" in status.value
    assert ("No notice was posted" in status.value) is (notice is None)


async def test_a_failed_triage_returns_the_tracker_it_ran_with(monkeypatch) -> None:
    created = []
    real = mod_tools.create_moderation_tools

    def spy(**kwargs):
        tools, tracker = real(**kwargs)
        tracker.flags.append("42")  # stands in for an action that ran
        created.append(tracker)
        return tools, tracker

    monkeypatch.setattr(moderation_agent, "create_moderation_tools", spy)

    _, tracker = await _triage(FunctionModel(_failing))

    assert tracker is created[0]
    assert tracker.flags == ["42"]
    assert tracker.failure == "RuntimeError"


def test_a_finished_triage_with_no_action_still_says_so():
    embed = build_triage_report_embed(ActionTracker(), "All fine.", "reporter", "2", "3")
    assert embed.title == "Moderation Triage Report"
    assert "No Action Taken" in _field_names(embed)
    assert "Manual Review Required" not in _field_names(embed)
