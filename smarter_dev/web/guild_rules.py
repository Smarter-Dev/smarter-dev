"""Parsing of a guild's admin-authored rules markdown into addressable rules.

A guild's rules are a single markdown textarea on ``/admin/bot``. The ``/rule``
command needs to cite *one* rule, and an LLM can only point at a rule it can
name, so the free text has to become an ordered list of addressable rules. This
module owns that conversion and nothing else.

Format (documented to admins in the admin page's help text):

- **One markdown heading per rule** — ``## No self-promotion``. Any ATX heading
  level (``#`` through ``######``) starts a new rule, so a rule's body must not
  contain headings; use bold text or a list instead.
- Everything between one heading and the next is that rule's **body**, verbatim.
- Text before the first heading is a **preamble** and is not a rule.
- Rules are numbered automatically from 1 in document order. That 1-based index
  is the identifier the LLM returns and the command validates against, so
  reordering the textarea renumbers the rules.

This module lives under ``smarter_dev.web`` on purpose: the bot imports from the
web package freely, but the web package must never import from the bot.

Everything here is pure — no I/O, no mutation of its arguments — and
:func:`parse_guild_rules` never raises on malformed input. An admin who types
nonsense gets whatever could be parsed (possibly nothing), shown back to them by
the admin page's preview, rather than a broken page or a broken command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: An ATX heading that starts a rule: up to three leading spaces (four would be
#: an indented code block), one to six hashes, then either end-of-line or
#: whitespace and the title. Requiring the whitespace keeps ``#hashtag`` out.
_RULE_HEADING_PATTERN = re.compile(
    r"^ {0,3}(?P<hashes>#{1,6})(?:[ \t]+(?P<title>.*?))?[ \t]*$"
)

#: A fenced code block delimiter. Headings inside a fence are body text, not
#: rule delimiters, so a rule may quote markdown or shell comments safely.
_CODE_FENCE_PATTERN = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")

#: Trailing hashes of a closed ATX heading (``## Title ##``), which are
#: decoration rather than part of the title.
_CLOSING_HASHES_PATTERN = re.compile(r"[ \t]+#+$")


@dataclass(frozen=True)
class GuildRule:
    """One addressable rule parsed out of a guild's rules markdown.

    Attributes:
        index: 1-based position in document order. This is the identifier the
            ``/rule`` LLM returns and the command validates against.
        title: The heading text, with markdown decoration stripped. Falls back
            to ``"Rule <index>"`` when the heading carries no text.
        body: The verbatim rule text between this heading and the next, with
            surrounding blank lines and per-line trailing whitespace removed.
            Empty when the rule is a bare heading.
    """

    index: int
    title: str
    body: str


def normalize_newlines(text: str | None) -> str:
    """Return ``text`` with CRLF and lone CR line endings collapsed to LF.

    Browsers submit textarea content with CRLF endings, so this runs both at
    save time (what gets stored) and inside the parser (what gets parsed), which
    keeps stored markdown and parsed output byte-identical either way.
    ``None`` — an absent form field — normalises to the empty string.
    """
    if not text:
        return ""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_guild_rules(rules_markdown: str | None) -> list[GuildRule]:
    """Parse a guild's rules markdown into an ordered list of rules.

    Pure function; never raises. Malformed input yields whatever could be
    parsed, down to an empty list for markdown with no headings at all.

    Args:
        rules_markdown: The admin-authored textarea contents, or ``None``.

    Returns:
        The rules in document order, numbered from 1. Text before the first
        heading is discarded as a preamble.
    """
    # Each section is one heading's title and the body lines that follow it.
    # Lines before the first heading belong to no section and are discarded.
    sections: list[tuple[str, list[str]]] = []
    open_fence: str | None = None

    for line in normalize_newlines(rules_markdown).split("\n"):
        open_fence = _next_fence_state(line, open_fence)
        heading_title = None if open_fence else _heading_title_of(line)
        if heading_title is not None:
            sections.append((heading_title, []))
        elif sections:
            sections[-1][1].append(line.rstrip())

    return [
        GuildRule(
            index=index,
            title=title or f"Rule {index}",
            body="\n".join(body_lines).strip("\n"),
        )
        for index, (title, body_lines) in enumerate(sections, start=1)
    ]


def find_rule_by_index(rules: list[GuildRule], index: int) -> GuildRule | None:
    """Return the rule with the given 1-based index, or ``None`` if there is none.

    Pure function. Consumers validating an LLM-supplied index should use this
    rather than list indexing, which would happily accept ``0`` and negatives.
    """
    for rule in rules:
        if rule.index == index:
            return rule
    return None


def format_rules_for_prompt(rules: list[GuildRule]) -> str:
    """Render rules as numbered text for an LLM prompt.

    Pure function. Each rule appears as ``<index>. <title>`` followed by its
    body, so the model sees the exact index it is expected to return.
    """
    blocks = [
        f"{rule.index}. {rule.title}\n{rule.body}".rstrip() for rule in rules
    ]
    return "\n\n".join(blocks)


def _next_fence_state(line: str, open_fence: str | None) -> str | None:
    """Return the code-fence marker still open after ``line``.

    Pure helper. ``open_fence`` is the marker that opened the current block, or
    ``None`` outside one. A block closes on a fence of the same character that
    is at least as long as the one that opened it; an unterminated fence simply
    runs to the end of the document.
    """
    match = _CODE_FENCE_PATTERN.match(line)
    if match is None:
        return open_fence
    fence = match.group("fence")
    if open_fence is None:
        return fence
    if fence[0] == open_fence[0] and len(fence) >= len(open_fence):
        return None
    return open_fence


def _heading_title_of(line: str) -> str | None:
    """Return the title of a rule heading, or ``None`` when ``line`` is not one.

    Pure helper. An empty string means "a heading with no title text", which the
    caller replaces with the index-based fallback — distinct from ``None``.
    """
    match = _RULE_HEADING_PATTERN.match(line)
    if match is None:
        return None
    title = (match.group("title") or "").strip()
    title = _CLOSING_HASHES_PATTERN.sub("", title).strip()
    return "" if set(title) <= {"#"} else title
