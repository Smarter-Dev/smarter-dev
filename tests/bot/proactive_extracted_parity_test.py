"""Lock the split service's observable contracts to the integrated runtime."""

# ruff: noqa: E402 -- the extracted package path is intentionally injected below.

from __future__ import annotations

import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from uuid import uuid4

SERVICE_SRC = Path(__file__).resolve().parents[2] / "services/proactive-agent/src"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICE_SRC))

from proactive_agent import agent as extracted_agent  # noqa: E402
from proactive_agent import capabilities as extracted_capabilities  # noqa: E402
from proactive_agent import contracts as extracted_contracts  # noqa: E402
from proactive_agent import engine as extracted_engine  # noqa: E402
from proactive_agent import image_generator as extracted_image_generator  # noqa: E402
from proactive_agent import parity as extracted_parity  # noqa: E402
from proactive_agent.response_fitting import (
    split_for_discord as extracted_split,  # noqa: E402
)
from pydantic_ai.models.test import TestModel

from smarter_dev.bot.agents.chat_tools import chat_tool_functions
from smarter_dev.bot.agents.handler_tools import handler_tool_functions
from smarter_dev.bot.agents.image_generator import STYLE_PREAMBLE
from smarter_dev.bot.agents.image_prompt_reviewer import SYSTEM_PROMPT as IMAGE_PROMPT
from smarter_dev.bot.agents.media_reader import SYSTEM_PROMPT as MEDIA_PROMPT
from smarter_dev.bot.agents.response_fitting import split_for_discord
from smarter_dev.bot.agents.web_summarizer import SYSTEM_PROMPT as WEB_PROMPT
from smarter_dev.bot.proactive import adapter
from smarter_dev.bot.proactive import agent
from smarter_dev.bot.proactive.contracts import NotificationEnvelope
from smarter_dev.bot.proactive.environment import InstructionStore
from smarter_dev.bot.proactive.notifications import Notification
from smarter_dev.bot.proactive.parity import (
    build_proactive_agent as build_integrated_agent,
)


def test_prompts_and_cadence_policy_are_identical():
    assert extracted_agent.OPERATING_POLICY_BRIEF == agent.OPERATING_POLICY_BRIEF
    assert extracted_agent.GUILD_AGENT_SYSTEM_PROMPT == agent.GUILD_AGENT_SYSTEM_PROMPT
    assert extracted_agent.PASSIVE_SWEEP_MINUTES == agent.PASSIVE_SWEEP_MINUTES
    assert extracted_agent.ACTIVE_WINDOW_MINUTES == agent.ACTIVE_WINDOW_MINUTES
    assert extracted_agent.MAX_SENDS_PER_WAKE == agent.MAX_SENDS_PER_WAKE
    assert extracted_agent.TOOL_CALL_LIMIT == agent.TOOL_CALL_LIMIT
    assert extracted_capabilities.WEB_SUMMARY_PROMPT == WEB_PROMPT
    assert extracted_capabilities.IMAGE_REVIEW_PROMPT == IMAGE_PROMPT
    assert extracted_capabilities.MEDIA_READER_PROMPT == MEDIA_PROMPT
    assert extracted_image_generator.STYLE_PREAMBLE == STYLE_PREAMBLE


def test_wake_brief_is_byte_for_byte_identical():
    created_at = datetime(2026, 9, 1, 17, 0, tzinfo=UTC)
    integrated_notification = Notification(
        kind="mention",
        created_at=created_at,
        body="alice mentioned you",
        channel_id="22",
        channel_name="general",
        message_ids=("33",),
        wakes=True,
    )
    extracted_notification = extracted_contracts.NotificationEnvelope(
        schema_version=1,
        notification_id=uuid4(),
        guild_id="11",
        channel_id="22",
        channel_name="general",
        kind="mention",
        created_at=created_at,
        body="alice mentioned you",
        message_ids=("33",),
        wakes=True,
        passive=False,
        watcher_usage={},
        trace_id=uuid4(),
    )
    integrated_store = InstructionStore.from_stored(agent.OPERATING_POLICY_BRIEF, "")
    extracted_store = extracted_engine.InstructionStore.from_stored(
        extracted_agent.OPERATING_POLICY_BRIEF, ""
    )

    integrated = adapter.build_wake_brief(
        (integrated_notification,),
        0,
        instruction_stores={"22": integrated_store},
        enabled_channels={"22": "general"},
    )
    extracted = extracted_engine.build_wake_brief(
        (extracted_notification,),
        0,
        instruction_stores={"22": extracted_store},
        enabled_channels={"22": "general"},
    )
    assert extracted == integrated


def test_notification_wire_round_trips_between_repositories():
    root = NotificationEnvelope(
        guild_id="11",
        channel_id="22",
        channel_name="general",
        kind="watcher_summary",
        created_at=datetime(2026, 9, 1, 17, 0, tzinfo=UTC),
        body="interesting",
        message_ids=("33",),
        wakes=True,
        passive=True,
        watcher_usage={"watcher": {"input_tokens": 2, "output_tokens": 1}},
    )
    split = extracted_contracts.NotificationEnvelope.model_validate_json(
        root.model_dump_json()
    )
    assert split.model_dump(mode="json") == root.model_dump(mode="json")


def test_canonical_schema_copies_are_identical():
    for filename in ("notification.schema.json", "control-command.schema.json"):
        root = json.loads(
            (REPOSITORY_ROOT / "contracts/proactive/v1" / filename).read_text()
        )
        split = json.loads(
            (
                REPOSITORY_ROOT
                / "services/proactive-agent/contracts/proactive/v1"
                / filename
            ).read_text()
        )
        assert split == root


def test_tool_names_and_discord_splitting_match():
    integrated_tools = chat_tool_functions() + handler_tool_functions()
    assert {tool.__name__ for tool in extracted_parity.parity_tool_functions()} == {
        tool.__name__ for tool in integrated_tools
    }
    samples = ["short", "x" * 2000, "a" * 1500 + "\n" + "b" * 1200]
    assert [extracted_split(value) for value in samples] == [
        split_for_discord(value) for value in samples
    ]


def test_tool_argument_contracts_match():
    integrated = build_integrated_agent(
        TestModel(custom_output_text="done"), system_prompt="s"
    )
    split = extracted_parity.build_proactive_agent(
        TestModel(custom_output_text="done"), system_prompt="s"
    )
    for name in {tool.__name__ for tool in extracted_parity.parity_tool_functions()}:
        integrated_schema = integrated._function_toolset.tools[
            name
        ].function_schema.json_schema
        split_schema = split._function_toolset.tools[name].function_schema.json_schema
        assert split_schema.get("properties") == integrated_schema.get("properties"), (
            name
        )
        assert split_schema.get("required") == integrated_schema.get("required"), name
