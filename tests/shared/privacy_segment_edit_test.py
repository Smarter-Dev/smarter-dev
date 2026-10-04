"""The shared segment/edit rebuild agrees with its test vectors.

``contracts/privacy/v1/segment_edit_vectors.json`` is copied byte-identical
by the worker, which runs the same cases against its copy of the module.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from smarter_dev.shared.privacy_purge import PurgeTarget
from smarter_dev.shared.privacy_segment_edit import SegmentEdit
from smarter_dev.shared.privacy_segment_edit import SegmentEditError
from smarter_dev.shared.privacy_segment_edit import apply_edits
from smarter_dev.shared.privacy_segment_edit import editable_segments
from smarter_dev.shared.privacy_segment_edit import segments

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads(
    (ROOT / "contracts/privacy/v1/segment_edit_vectors.json").read_text(encoding="utf-8")
)
USER_ID = "111111111111111111"


def _id(vector: dict) -> str:
    return vector.get("description") or vector["text"][:40] or "(empty)"


@pytest.mark.parametrize(
    "vector", [v for v in VECTORS if v["kind"] == "segments"], ids=_id
)
def test_segmentation_vectors(vector):
    target = PurgeTarget.build(USER_ID, vector["names"])
    found = segments(vector["location"], vector["text"], target)
    assert [seg.text for seg in found] == vector["segments"]
    assert [
        seg.id for seg in editable_segments(vector["location"], vector["text"], target)
    ] == vector["editable"]


@pytest.mark.parametrize("vector", [v for v in VECTORS if v["kind"] == "apply"], ids=_id)
def test_rebuild_vectors(vector):
    target = PurgeTarget.build(USER_ID, vector["names"])
    edits = [SegmentEdit(**edit) for edit in vector["edits"]]
    if vector["error"]:
        with pytest.raises(SegmentEditError):
            apply_edits(vector["texts"], edits, target, unresolved=vector["unresolved"])
    else:
        assert (
            apply_edits(vector["texts"], edits, target, unresolved=vector["unresolved"])
            == vector["result"]
        )


def test_the_vectors_cover_the_required_cases():
    descriptions = {v.get("description") for v in VECTORS}
    for needed in (
        "remove",
        "rewrite",
        "keep with unresolved",
        "untouched text that does not mention",
        "empty stays empty",
        "missing edit",
        "extra edit on a bystander segment",
    ):
        assert needed in descriptions


def test_the_module_imports_only_the_stdlib_pydantic_and_the_matcher():
    tree = ast.parse((ROOT / "smarter_dev/shared/privacy_segment_edit.py").read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    smarter = {m for m in modules if m and m.startswith("smarter_dev")}
    assert smarter == {"smarter_dev.shared.privacy_purge"}
    assert not {m for m in modules if m and m.split(".")[0] in {"sqlalchemy", "skrift", "litestar"}}
