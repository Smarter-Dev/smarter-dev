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
            apply_edits(vector["texts"], edits, target)
    else:
        assert apply_edits(vector["texts"], edits, target) == vector["result"]


def test_the_vectors_cover_the_required_cases():
    descriptions = {v.get("description") for v in VECTORS}
    for needed in (
        "remove",
        "rewrite",
        "keep leaves the segment",
        "untouched text that does not mention",
        "empty stays empty",
        "removing every segment empties the text (the caller decides)",
        "missing edit",
        "extra edit on a bystander segment",
        "keep with the ID",
    ):
        assert needed in descriptions


def test_the_module_imports_only_the_stdlib_pydantic_and_the_matcher():
    tree = ast.parse((ROOT / "smarter_dev/shared/privacy_segment_edit.py").read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules |= {a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names}
    smarter = {m for m in modules if m and m.startswith("smarter_dev")}
    assert smarter == {"smarter_dev.shared.privacy_purge"}
    assert not {m for m in modules if m and m.split(".")[0] in {"sqlalchemy", "skrift", "litestar"}}


# -- property: every byte outside a removed or rewritten segment survives ------------

_POOL = ["bob", "nia", "likes", "tea", "rust", "the", "meetup", "runs", "on", "Carol"]
_PREFIXES = ["", "- ", "  - ", "1. ", "* ", "    "]
_SEPS = [" ", "  ", "\t", " \t "]
_TAILS = ["", "\r", "  ", " \r", "\t"]
# Marker-like text a later sentence may start with; it is that sentence's own.
_INNER_MARKERS = ["", "", "1) ", "- ", "* "]


def _random_sentence(rng, with_target: bool) -> list[str]:
    words = [rng.choice(_POOL) for _ in range(rng.randint(1, 5))]
    if with_target:
        words.insert(rng.randint(0, len(words)), "kai")
    return words


def test_rebuild_keeps_every_byte_outside_the_edited_segments():
    import random

    rng = random.Random(1337)
    target = PurgeTarget.build(USER_ID, ["kai"])
    for _ in range(3000):
        lines, expected_lines, edits = [], [], []
        last_kept = -1
        index = 0
        for _line in range(rng.randint(1, 5)):
            prefix, tail = rng.choice(_PREFIXES), rng.choice(_TAILS)
            sentences = [
                _random_sentence(rng, rng.random() < 0.4) for _ in range(rng.randint(1, 4))
            ]
            bodies = [
                (rng.choice(_INNER_MARKERS) if k else "")
                + " ".join(words)
                + rng.choice([".", "!", "?"])
                for k, words in enumerate(sentences)
            ]
            seps = [rng.choice(_SEPS) for _ in bodies[1:]]
            line = prefix + bodies[0] + "".join(s + b for s, b in zip(seps, bodies[1:])) + tail
            lines.append(line)
            survivors: list[tuple[int, str]] = []
            for k, (words, body) in enumerate(zip(sentences, bodies)):
                seg_id = f"memory:{index + k}"
                if "kai" not in words:
                    survivors.append((k, body))
                    continue
                kept = [w for w in words if w != "kai"]
                if kept and rng.random() < 0.5:
                    marker = body[: len(body) - len(body.lstrip("-*1) "))] if k else ""
                    new_body = marker + " ".join(kept) + body[-1]
                    edits.append({"id": seg_id, "action": "rewrite", "text": new_body})
                    survivors.append((k, new_body))
                else:
                    edits.append({"id": seg_id, "action": "remove"})
            index += len(bodies)
            if not any(len(s) for s in sentences if "kai" in s):
                expected_lines.append(line)
                last_kept = _line
            elif survivors:
                out = prefix + survivors[0][1]
                for k, body in survivors[1:]:
                    out += seps[k - 1] + body
                expected_lines.append(out + tail)
                last_kept = _line
        trailing_newline = rng.random() < 0.5
        if (
            not trailing_newline
            and expected_lines
            and last_kept != len(lines) - 1
            and expected_lines[-1].endswith("\r")
        ):
            expected_lines[-1] = expected_lines[-1][:-1]  # the last terminator goes whole
        text = "\n".join(lines + ([""] if trailing_newline else []))
        expected = "\n".join(expected_lines + ([""] if trailing_newline else []))
        result = apply_edits(
            {"memory": text}, [SegmentEdit(**e) for e in edits], target
        )["memory"]
        assert result == expected, (text, edits)


def test_removing_the_last_crlf_line_leaves_no_dangling_carriage_return():
    target = PurgeTarget.build(USER_ID, ["Alice"])
    text = "Bob likes tea.\r\nAlice likes rust."
    edits = [SegmentEdit(id="memory:1", action="remove")]
    assert apply_edits({"memory": text}, edits, target)["memory"] == "Bob likes tea."
    lf = apply_edits({"memory": text.replace("\r", "")}, edits, target)["memory"]
    assert lf == "Bob likes tea."


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Alice left. 1) Bob stays. 2) Carol too.", "1) Bob stays. 2) Carol too."),
        ("Alice left; - Bob stays", "- Bob stays"),
        ("  - Alice left. 1) Bob stays.", "  - 1) Bob stays."),
    ],
)
def test_only_the_lines_own_prefix_moves_when_its_first_segment_goes(text, expected):
    target = PurgeTarget.build(USER_ID, ["Alice"])
    edits = [SegmentEdit(id="memory:0", action="remove")]
    assert apply_edits({"memory": text}, edits, target)["memory"] == expected
