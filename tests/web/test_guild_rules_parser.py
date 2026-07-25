"""Tests for the guild-rules markdown parser.

The parser turns an admin-authored textarea into an ordered list of addressable
rules. Its output is a contract shared by the ``/rule`` command (which cites a
rule by its 1-based index) and the ``/admin/bot`` preview, so the messy real
cases are pinned down here rather than left to either consumer.
"""

from __future__ import annotations

import pytest

from smarter_dev.web.guild_rules import GuildRule
from smarter_dev.web.guild_rules import find_rule_by_index
from smarter_dev.web.guild_rules import format_rules_for_prompt
from smarter_dev.web.guild_rules import normalize_newlines
from smarter_dev.web.guild_rules import parse_guild_rules

_TWO_RULES = """\
## No self-promotion
Don't advertise your own projects outside #showcase.

## Be kind
Attack the code, never the coder.
"""


# --- happy path ---------------------------------------------------------------


def test_parses_headings_into_ordered_rules():
    rules = parse_guild_rules(_TWO_RULES)

    assert [rule.index for rule in rules] == [1, 2]
    assert [rule.title for rule in rules] == ["No self-promotion", "Be kind"]
    assert rules[0].body == "Don't advertise your own projects outside #showcase."
    assert rules[1].body == "Attack the code, never the coder."


def test_rules_are_frozen_dataclasses_with_the_documented_fields():
    rule = parse_guild_rules("## Title\nBody")[0]

    assert isinstance(rule, GuildRule)
    assert (rule.index, rule.title, rule.body) == (1, "Title", "Body")
    with pytest.raises(AttributeError):
        rule.index = 2


def test_indices_are_one_based_and_contiguous():
    markdown = "\n".join(f"## Rule {n}\nBody {n}" for n in range(1, 8))

    rules = parse_guild_rules(markdown)

    assert [rule.index for rule in rules] == list(range(1, 8))


def test_body_preserves_verbatim_markdown_including_blank_lines():
    markdown = (
        "## Formatting\n"
        "Use **bold** for emphasis.\n"
        "\n"
        "- one\n"
        "- two\n"
    )

    body = parse_guild_rules(markdown)[0].body

    assert body == "Use **bold** for emphasis.\n\n- one\n- two"


def test_every_heading_level_starts_a_rule():
    markdown = "\n".join(f"{'#' * level} Level {level}\nBody" for level in range(1, 7))

    rules = parse_guild_rules(markdown)

    assert [rule.title for rule in rules] == [f"Level {n}" for n in range(1, 7)]


# --- messy real cases ---------------------------------------------------------


def test_text_before_the_first_heading_is_a_preamble_not_a_rule():
    markdown = (
        "These are the server rules. Breaking them gets you muted.\n"
        "\n"
        "## No spam\n"
        "One message is enough.\n"
    )

    rules = parse_guild_rules(markdown)

    assert len(rules) == 1
    assert rules[0].index == 1
    assert rules[0].title == "No spam"
    assert "server rules" not in rules[0].body


def test_markdown_without_any_heading_parses_to_no_rules():
    rules = parse_guild_rules("Just be excellent to each other.\nNo headings here.")

    assert rules == []


@pytest.mark.parametrize("empty", [None, "", "   ", "\n\n", "\r\n\r\n", "\t \n"])
def test_empty_input_parses_to_no_rules(empty):
    assert parse_guild_rules(empty) == []


def test_crlf_line_endings_parse_identically_to_unix_endings():
    assert parse_guild_rules(_TWO_RULES.replace("\n", "\r\n")) == parse_guild_rules(
        _TWO_RULES
    )


def test_lone_carriage_returns_are_normalised():
    assert parse_guild_rules("## Title\rBody\r") == [
        GuildRule(index=1, title="Title", body="Body")
    ]


def test_rule_with_no_body_keeps_its_title_and_an_empty_body():
    rules = parse_guild_rules("## Title only\n\n## Second\nHas a body.\n")

    assert rules[0].title == "Title only"
    assert rules[0].body == ""
    assert rules[1].body == "Has a body."


def test_trailing_rule_with_no_body_is_still_a_rule():
    rules = parse_guild_rules("## First\nBody.\n\n## Last\n\n\n")

    assert [rule.title for rule in rules] == ["First", "Last"]
    assert rules[1].body == ""


def test_trailing_whitespace_is_stripped_from_titles_and_bodies():
    markdown = "##   Padded title   \t\n  Body line one   \nBody line two\t\n\n\n"

    rule = parse_guild_rules(markdown)[0]

    assert rule.title == "Padded title"
    assert rule.body == "  Body line one\nBody line two"


def test_blank_lines_around_a_body_are_trimmed_but_inner_ones_survive():
    markdown = "## Title\n\n\nFirst para.\n\n\nSecond para.\n\n\n## Next\nx\n"

    assert parse_guild_rules(markdown)[0].body == "First para.\n\n\nSecond para."


def test_closed_atx_heading_drops_the_trailing_hashes():
    assert parse_guild_rules("## Balanced ##\nBody")[0].title == "Balanced"


def test_heading_with_no_title_text_falls_back_to_its_index():
    rules = parse_guild_rules("## Real title\nBody\n\n##\nOrphan body\n")

    assert rules[1].title == "Rule 2"
    assert rules[1].body == "Orphan body"


def test_hash_without_a_following_space_is_not_a_heading():
    rules = parse_guild_rules("## Tags\n#hashtag is fine to use\n")

    assert len(rules) == 1
    assert rules[0].body == "#hashtag is fine to use"


def test_headings_inside_a_fenced_code_block_are_body_text():
    markdown = (
        "## Code of conduct\n"
        "Post code like this:\n"
        "```python\n"
        "## not a rule\n"
        "print('hi')\n"
        "```\n"
        "\n"
        "## Second rule\n"
        "Body.\n"
    )

    rules = parse_guild_rules(markdown)

    assert [rule.title for rule in rules] == ["Code of conduct", "Second rule"]
    assert "## not a rule" in rules[0].body


def test_tilde_fences_are_honoured_too():
    markdown = "## One\n~~~\n## fake\n~~~\n\n## Two\nBody\n"

    assert [rule.title for rule in parse_guild_rules(markdown)] == ["One", "Two"]


def test_unterminated_code_fence_swallows_the_rest_without_raising():
    markdown = "## One\nBody\n\n```\n## Two\nstill code\n"

    rules = parse_guild_rules(markdown)

    assert [rule.title for rule in rules] == ["One"]
    assert "## Two" in rules[0].body


def test_heading_indented_up_to_three_spaces_is_still_a_delimiter():
    rules = parse_guild_rules("   ## Indented\nBody\n")

    assert [rule.title for rule in rules] == ["Indented"]


def test_heading_indented_four_spaces_is_code_not_a_delimiter():
    rules = parse_guild_rules("## Real\n    ## indented code\nBody\n")

    assert len(rules) == 1
    assert "## indented code" in rules[0].body


def test_seven_hashes_is_not_a_heading():
    rules = parse_guild_rules("## Real\n####### too deep\n")

    assert len(rules) == 1
    assert "####### too deep" in rules[0].body


@pytest.mark.parametrize(
    "garbage",
    [
        "#",
        "#####",
        "## \n## \n## ",
        "```",
        "~~~~~~",
        "## \x00 null byte\n\x00",
        "## Emoji 🎉\n🎉" * 50,
        "#" * 5000,
        "\n" * 5000 + "## Late\nBody",
    ],
)
def test_malformed_input_never_raises(garbage):
    rules = parse_guild_rules(garbage)

    assert isinstance(rules, list)
    assert all(isinstance(rule, GuildRule) for rule in rules)
    assert [rule.index for rule in rules] == list(range(1, len(rules) + 1))


# --- normalize_newlines -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\r\nb", "a\nb"),
        ("a\rb", "a\nb"),
        ("a\nb", "a\nb"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_newlines(raw, expected):
    assert normalize_newlines(raw) == expected


# --- find_rule_by_index -------------------------------------------------------


def test_find_rule_by_index_returns_the_matching_rule():
    rules = parse_guild_rules(_TWO_RULES)

    assert find_rule_by_index(rules, 2).title == "Be kind"


@pytest.mark.parametrize("index", [0, -1, 3, 999])
def test_find_rule_by_index_returns_none_when_out_of_range(index):
    assert find_rule_by_index(parse_guild_rules(_TWO_RULES), index) is None


def test_find_rule_by_index_on_an_empty_list_is_none():
    assert find_rule_by_index([], 1) is None


# --- format_rules_for_prompt --------------------------------------------------


def test_format_rules_for_prompt_labels_every_rule_with_its_index():
    formatted = format_rules_for_prompt(parse_guild_rules(_TWO_RULES))

    assert "1. No self-promotion" in formatted
    assert "2. Be kind" in formatted
    assert "Attack the code, never the coder." in formatted


def test_format_rules_for_prompt_of_no_rules_is_empty():
    assert format_rules_for_prompt([]) == ""


def test_format_rules_for_prompt_handles_a_bodyless_rule():
    formatted = format_rules_for_prompt(parse_guild_rules("## Bare\n\n## Next\nBody"))

    assert "1. Bare" in formatted
    assert "2. Next" in formatted
