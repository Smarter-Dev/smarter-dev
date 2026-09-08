"""No feature-parity plan may still teach a handler that reacts to command text.

Prefix commands are prohibited under Discord's message-content-intent policy,
and ``handler_lint`` rejects a script that branches on a message's leading
command word. A plan that sketches one anyway sends its next reader to write a
handler the pipeline refuses, so every plan that shows or endorses a text
command has to say the surface was retired and what replaced it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from smarter_dev.web.handler_lint import check_static

PLANS_DIRECTORY = Path(__file__).resolve().parents[2] / "docs" / "v2" / "feature-parity"
PLANS = sorted(PLANS_DIRECTORY.glob("*.md"))

REMOVAL_NOTE = "prefix commands are prohibited"
RETIREMENT_MARKERS = ("REMOVED", "RETIRED")
TEXT_COMMAND_ENDORSEMENTS = ("message-text command", "plain-text command")

_FENCED_BLOCK = re.compile(r"^```[a-z]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
_HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)


def _is_prefix_command_branch(line: str) -> bool:
    reason = check_static(line)
    return reason is not None and "prefix commands are prohibited" in reason


def _prefix_command_sketch_offsets(plan_text: str) -> list[int]:
    """Where each sketch the lint would reject as a prefix command starts.

    Line by line, because a sketch usually breaks an earlier lint rule too and
    ``check_static`` reports only the first reason it finds.
    """
    return [
        block.start()
        for block in _FENCED_BLOCK.finditer(plan_text)
        if any(_is_prefix_command_branch(line) for line in block.group(1).splitlines())
    ]


def _section_before(plan_text: str, offset: int) -> str:
    headings = list(_HEADING.finditer(plan_text, 0, offset))
    return plan_text[headings[-1].start() : offset] if headings else plan_text[:offset]


def test_the_plans_are_where_this_test_looks():
    assert len(PLANS) >= 5


@pytest.mark.parametrize("plan", PLANS, ids=lambda plan: plan.name)
def test_a_plan_that_shows_a_text_command_says_the_surface_was_retired(plan):
    plan_text = plan.read_text()
    shows_a_sketch = bool(_prefix_command_sketch_offsets(plan_text))
    endorses_the_style = any(
        phrase in plan_text for phrase in TEXT_COMMAND_ENDORSEMENTS
    )
    if shows_a_sketch or endorses_the_style:
        assert REMOVAL_NOTE in plan_text


@pytest.mark.parametrize("plan", PLANS, ids=lambda plan: plan.name)
def test_every_text_command_sketch_is_marked_retired_where_it_sits(plan):
    plan_text = plan.read_text()
    for offset in _prefix_command_sketch_offsets(plan_text):
        section = _section_before(plan_text, offset)
        assert any(marker in section for marker in RETIREMENT_MARKERS), (
            f"{plan.name}: the sketch at line "
            f"{plan_text[:offset].count(chr(10)) + 1} is not marked retired"
        )
