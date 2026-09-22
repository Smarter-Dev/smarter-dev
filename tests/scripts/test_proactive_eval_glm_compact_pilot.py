from __future__ import annotations

from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.proactive_eval.glm_compact_pilot import compact_messages
from scripts.proactive_eval.glm_compact_pilot import compact_response_format
from scripts.proactive_eval.glm_compact_pilot import parse_completion
from scripts.proactive_eval.glm_compact_pilot import run
from scripts.proactive_eval.glm_compact_pilot import safe_exception_diagnostics
from smarter_dev.bot.proactive.types import ChannelMessage


def test_compact_schema_matches_jev_atomic_boolean_contract() -> None:
    schema = compact_response_format()["json_schema"]["schema"]

    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {
        "addressed_to_bot_by_name",
        "matches_watch_criteria",
        "useful_intervention",
        "specific_people_exchange",
    }
    assert all(item["type"] == "boolean" for item in schema["properties"].values())
    assert all(item.get("description") for item in schema["properties"].values())


def test_compact_messages_keep_policy_out_of_classification_material() -> None:
    message = ChannelMessage(
        id="m1",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        author_id="u1",
        author_name="user",
        author_display="User",
        is_bot=False,
        content="hello",
        reply_to_id=None,
        mention_user_ids=(),
        mention_everyone=False,
        attachment_count=0,
        sticker_count=0,
        message_type=0,
    )
    case = {
        "history": [],
        "new_messages": [message],
        "meta": {"bot_user_id": "b1"},
    }

    messages = compact_messages(case)

    assert messages[0]["role"] == "system"
    assert "Explicit wake criteria" in messages[0]["content"]
    assert messages[1]["role"] == "user"
    assert "Explicit wake criteria" not in messages[1]["content"]


def _response(content: str, *, finish_reason: str = "stop") -> SimpleNamespace:
    return SimpleNamespace(
        model="glm-5.3-flash",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, refusal=None),
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=25,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=8),
        ),
    )


def test_parse_completion_retains_safe_finish_usage_and_reasoning_metadata() -> None:
    output = (
        '{"addressed_to_bot_by_name":false,"matches_watch_criteria":true,'
        '"useful_intervention":true,"specific_people_exchange":false}'
    )

    judgments, usage, diagnostics, failure = parse_completion(_response(output))

    assert judgments is not None and judgments.matches_watch_criteria is True
    assert failure is None
    assert usage == {
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_read_tokens": 10,
    }
    assert diagnostics["finish_reason"] == "stop"
    assert diagnostics["reasoning_tokens"] == 8
    assert diagnostics["schema_valid"] is True


def test_parse_failure_diagnostics_never_include_raw_output() -> None:
    secret = "private-message-content"

    _, _, diagnostics, failure = parse_completion(_response(secret))

    assert failure == {"category": "invalid_json"}
    assert secret not in str(diagnostics)
    assert secret not in str(failure)


def test_parse_failure_diagnostics_never_include_arbitrary_json_keys() -> None:
    secret = "private-message-content"

    _, _, diagnostics, failure = parse_completion(_response('{"' + secret + '": true}'))

    assert failure is not None
    assert diagnostics["recognized_fields"] == []
    assert diagnostics["missing_field_count"] == 4
    assert diagnostics["unexpected_field_count"] == 1
    assert secret not in str(diagnostics)
    assert secret not in str(failure)


def test_exception_diagnostics_never_include_exception_text() -> None:
    secret = "private-message-content"
    try:
        raise ValueError(secret)
    except ValueError as error:
        diagnostics = safe_exception_diagnostics(error)

    assert diagnostics["category"] == "client_or_provider_error"
    assert secret not in str(diagnostics)


@pytest.mark.asyncio
async def test_compact_run_refuses_to_resume_or_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing overwrite/resume"):
        await run(
            tmp_path / "manifest.json",
            tmp_path / "selection.json",
            tmp_path / "plan.json",
            output,
        )
