"""Tests for splitting fenced LaTeX out of final chatbot messages."""

from smarter_dev.bot.agents.latex_blocks import MAX_LATEX_BLOCKS
from smarter_dev.bot.agents.latex_blocks import MAX_LATEX_SOURCE_CHARS
from smarter_dev.bot.agents.latex_blocks import LatexSection
from smarter_dev.bot.agents.latex_blocks import TextSection
from smarter_dev.bot.agents.latex_blocks import has_latex_section
from smarter_dev.bot.agents.latex_blocks import split_latex_sections


def test_splits_text_latex_text_in_order():
    message = "before\n```latex\nE = mc^2\n```\nafter"

    assert split_latex_sections(message) == [
        TextSection("before\n"),
        LatexSection("E = mc^2", "```latex\nE = mc^2\n```\n"),
        TextSection("after"),
    ]


def test_supports_multiple_and_adjacent_latex_blocks():
    message = "```latex\nx\n```\n```LATEX\ny\n```"

    assert split_latex_sections(message) == [
        LatexSection("x", "```latex\nx\n```\n"),
        LatexSection("y", "```LATEX\ny\n```"),
    ]


def test_ordinary_fence_hides_latex_looking_content():
    message = "```python\n```latex\nx\n```\n```\nafter"

    assert split_latex_sections(message) == [TextSection(message)]
    assert has_latex_section(message) is False


def test_unclosed_latex_fence_remains_text():
    message = "before\n```latex\nx + y"

    assert split_latex_sections(message) == [TextSection(message)]


def test_empty_latex_fence_remains_text():
    message = "before\n```latex\n\n```\nafter"

    assert split_latex_sections(message) == [TextSection(message)]


def test_oversized_latex_fence_remains_text():
    source = "x" * (MAX_LATEX_SOURCE_CHARS + 1)
    message = f"```latex\n{source}\n```"

    assert split_latex_sections(message) == [TextSection(message)]


def test_excess_latex_fences_remain_text():
    message = "".join(f"```latex\nx_{index}\n```\n" for index in range(7))
    sections = split_latex_sections(message)

    assert sum(isinstance(section, LatexSection) for section in sections) == (
        MAX_LATEX_BLOCKS
    )
    assert isinstance(sections[-1], TextSection)
    assert "x_5" in sections[-1].text
    assert "x_6" in sections[-1].text


def test_tilde_latex_fence_is_ordinary_text():
    message = "~~~latex\nx\n~~~"

    assert split_latex_sections(message) == [TextSection(message)]


def test_empty_message_has_no_sections():
    assert split_latex_sections("") == []
