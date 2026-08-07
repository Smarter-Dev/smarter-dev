"""Tests for splitting LaTeX out of final chatbot messages.

LaTeX reaches the parser two ways: the legacy ```latex fenced block, and the
standard math delimiters ``$$…$$``, ``\\[…\\]``, and ``\\(…\\)``. A bare single
``$`` is intentionally never treated as math so currency text passes through
untouched.
"""

from smarter_dev.bot.agents.latex_blocks import MAX_LATEX_BLOCKS
from smarter_dev.bot.agents.latex_blocks import MAX_LATEX_SOURCE_CHARS
from smarter_dev.bot.agents.latex_blocks import LatexSection
from smarter_dev.bot.agents.latex_blocks import TextSection
from smarter_dev.bot.agents.latex_blocks import has_latex_section
from smarter_dev.bot.agents.latex_blocks import split_latex_sections


# ---------------------------------------------------------------------------
# Existing ```latex fence behaviour — must keep passing unchanged.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Standard math delimiters — new behaviour.
# ---------------------------------------------------------------------------


def test_display_double_dollar_split_in_order():
    message = "before $$E = mc^2$$ after"

    assert split_latex_sections(message) == [
        TextSection("before "),
        LatexSection("E = mc^2", "$$E = mc^2$$"),
        TextSection(" after"),
    ]


def test_display_bracket_split_in_order():
    message = "area \\[ \\pi r^2 \\] done"

    assert split_latex_sections(message) == [
        TextSection("area "),
        LatexSection("\\pi r^2", "\\[ \\pi r^2 \\]"),
        TextSection(" done"),
    ]


def test_inline_paren_split_mid_sentence():
    message = "the value \\(x\\) is 5"

    assert split_latex_sections(message) == [
        TextSection("the value "),
        LatexSection("x", "\\(x\\)"),
        TextSection(" is 5"),
    ]


def test_display_delimiter_can_span_lines():
    message = "$$\nE = mc^2\n$$"

    assert split_latex_sections(message) == [
        LatexSection("E = mc^2", "$$\nE = mc^2\n$$"),
    ]


def test_single_dollar_is_never_math():
    message = "It costs $5 today"

    assert split_latex_sections(message) == [TextSection(message)]
    assert has_latex_section(message) is False


def test_two_single_dollars_are_not_a_display_block():
    message = "Between $5 and $10 total"

    assert split_latex_sections(message) == [TextSection(message)]


def test_escaped_dollars_stay_literal():
    message = "price \\$$x$$ here"

    # The escaped ``\$`` consumes the first dollar, leaving a lone ``$`` that is
    # not a delimiter, so nothing renders.
    assert split_latex_sections(message) == [TextSection(message)]


def test_double_dollar_yields_single_expression():
    message = "$$a$$"

    assert split_latex_sections(message) == [LatexSection("a", "$$a$$")]


def test_delimiter_inside_ordinary_fence_is_text():
    message = "```python\nprint('$$x$$')\n```\nafter"

    assert split_latex_sections(message) == [TextSection(message)]
    assert has_latex_section(message) is False


def test_delimiter_inside_latex_fence_is_not_double_counted():
    message = "```latex\n$$x$$\n```"
    sections = split_latex_sections(message)

    assert sum(isinstance(section, LatexSection) for section in sections) == 1


def test_unclosed_delimiter_remains_text():
    message = "before $$x + y"

    assert split_latex_sections(message) == [TextSection(message)]


def test_empty_delimiters_remain_text():
    assert split_latex_sections("a $$   $$ b") == [TextSection("a $$   $$ b")]
    assert split_latex_sections("a \\(\\) b") == [TextSection("a \\(\\) b")]


def test_oversized_delimited_source_remains_text():
    source = "x" * (MAX_LATEX_SOURCE_CHARS + 1)
    message = f"$${source}$$"

    assert split_latex_sections(message) == [TextSection(message)]


def test_combined_limit_across_fence_and_delimiter_forms():
    fences = "".join(f"```latex\nf_{index}\n```\n" for index in range(3))
    delimiters = " ".join(f"$$d_{index}$$" for index in range(4))
    message = fences + delimiters
    sections = split_latex_sections(message)

    # Three fences plus the first two delimited expressions reach the cap; the
    # third delimited expression (``d_2``) onward stay text.
    assert sum(isinstance(section, LatexSection) for section in sections) == (
        MAX_LATEX_BLOCKS
    )
    assert any(
        isinstance(section, TextSection) and "d_2" in section.text
        for section in sections
    )


def test_mixed_fence_and_delimiter_kept_in_order():
    message = "start\n```latex\nA\n```\nmid $$B$$ end"

    assert split_latex_sections(message) == [
        TextSection("start\n"),
        LatexSection("A", "```latex\nA\n```\n"),
        TextSection("mid "),
        LatexSection("B", "$$B$$"),
        TextSection(" end"),
    ]


def test_has_latex_section_true_for_delimiter_only_message():
    assert has_latex_section("look: \\[y = x\\]") is True
