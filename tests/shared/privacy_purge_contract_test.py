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


def test_stored_values_are_searched_leaf_by_leaf():
    from smarter_dev.shared.privacy_purge import PurgeTarget

    target = PurgeTarget.build("111111111111111111", ["Alice", "Zoë"])
    after_newline = json.dumps({"lines": ["hi\nAlice"]})
    ascii_escaped = json.dumps(["Zoë said hi"], ensure_ascii=True)
    assert target.name_hits(after_newline) == 0  # the raw search misses it
    assert target.stored_hits(after_newline) == (0, 1)
    assert target.stored_hits(ascii_escaped.encode()) == (0, 1)
    assert target.stored_hits("not json, Alice") == (0, 1)
    assert target.stored_hits(json.dumps({"uid": 111111111111111111})) == (1, 0)
    assert target.stored_hits("111111111111111111") == (1, 0)


VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "contracts/privacy/v1/name_matcher_vectors.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("vector", VECTORS, ids=[v["text"] for v in VECTORS])
def test_the_name_matcher_vectors(vector):
    from smarter_dev.shared.privacy_purge import PurgeTarget

    target = PurgeTarget.build("999999999999999999", vector["names"])
    assert (target.name_hits(vector["text"]) > 0) == vector["hit"]
    assert (target.mentions(vector["text"])) == vector["hit"]
    assert list(target.unchecked_names) == vector["unchecked"]


def test_the_vectors_cover_the_required_cases():
    texts = " ".join(v["text"] for v in VECTORS)
    for needle in ("alice_dev", "alice2", "malice", "🦀", "李", "rustacean", "KAI"):
        assert needle in texts
    assert any(v["unchecked"] for v in VECTORS)


def test_json_inside_json_strings_is_searched_to_depth_five():
    from smarter_dev.shared.privacy_purge import PurgeTarget

    target = PurgeTarget.build("111111111111111111", ["Zoë"])
    value = "line\nZoë"
    for _ in range(5):
        value = json.dumps({"args": value})
    assert target.stored_hits(value) == (0, 1)
