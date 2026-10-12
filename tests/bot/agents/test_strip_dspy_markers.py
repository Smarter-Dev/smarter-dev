"""DSPy's ``[[ ## field ## ]]`` markers must never reach Discord, even when the
model writes them loosely enough that DSPy's parser leaves them in the text."""

from __future__ import annotations

import pytest

from smarter_dev.bot.agents.base import strip_dspy_markers


@pytest.mark.parametrize(
    "text",
    [
        "Nice run!\n[[ ## completed ## ]]",
        "Nice run!\n[[ ## completed ##]]",
        "Nice run!\n[[## completed##]]",
        "Nice run!\n[[ ##completed## ]]",
        "Nice run!\n[[ ## COMPLETED ## ]]\n",
        "Nice run!  \n\n[[ ## response ## ]]",
        "Nice run![[ ## summary ## ]]",
    ],
)
def test_a_marker_at_the_end_is_removed_with_its_whitespace(text: str) -> None:
    assert strip_dspy_markers(text) == "Nice run!"


def test_a_marker_at_the_start_is_removed() -> None:
    assert strip_dspy_markers("[[ ## response ## ]]\nHello there") == "Hello there"


def test_a_marker_mid_text_does_not_glue_the_words_together() -> None:
    assert strip_dspy_markers("one [[ ## completed ## ]] two") == "one\ntwo"


def test_several_markers_are_all_removed() -> None:
    text = "[[ ## response ## ]]\nHi\n[[ ## completed ##]]\n[[ ## completed ## ]]"
    assert strip_dspy_markers(text) == "Hi"


def test_text_without_a_marker_is_only_stripped() -> None:
    text = "Use [[wiki links]] and ## headings ## freely.\n\nSecond paragraph."
    assert strip_dspy_markers(text) == text
    assert strip_dspy_markers("  padded \n") == "padded"


def test_only_a_marker_leaves_nothing() -> None:
    assert strip_dspy_markers("[[ ## completed ##]]") == ""
