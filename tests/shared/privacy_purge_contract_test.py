"""The purge command model and its JSON Schema copy agree on what is valid.

The schema is byte-identical to proactive-agent's copy; the worker validates
the same golden payload against its own model.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from smarter_dev.shared.privacy_purge import PurgeCommand

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "contracts/privacy/v1/purge_command.schema.json"
    ).read_text()
)

GOLDEN = {
    "schema_version": 1,
    "request_id": "6f1c2a52-6d55-4d0b-9b6f-6a8f4f1c0a01",
    "run_id": "0b8e0c41-3d1b-4a3a-8d7e-2d6b9d1e7f02",
    "user_id": "111111111111111111",
    "names": ["kai", "Kai the Rustacean"],
    "guild_ids": ["123456789012345678"],
    "created_at": "2026-10-04T12:00:00Z",
}


def test_the_golden_payload_is_valid_for_both():
    jsonschema.validate(GOLDEN, SCHEMA)
    command = PurgeCommand.model_validate(GOLDEN)
    assert command.user_id not in repr(command)
    assert "kai" not in str(command)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"extra": True},
        {"user_id": "kai"},
        {"guild_ids": []},
        {"names": [""]},
    ],
)
def test_both_reject_the_same_bad_payloads(change):
    payload = {**GOLDEN, **change}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, SCHEMA)
    with pytest.raises(ValidationError):
        PurgeCommand.model_validate(payload)
