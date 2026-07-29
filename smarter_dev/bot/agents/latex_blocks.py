"""Split fenced LaTeX out of a chatbot response without changing its source."""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_LATEX_BLOCKS = 5
MAX_LATEX_SOURCE_CHARS = 1_800

_OPENING_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")


@dataclass(frozen=True)
class TextSection:
    """Ordinary Discord markdown that should be sent as text."""

    text: str


@dataclass(frozen=True)
class LatexSection:
    """The source and lossless fallback for one fenced equation."""

    source: str
    original: str


ResponseSection = TextSection | LatexSection


def _is_closing_fence(line: str, marker: str) -> bool:
    """Return whether ``line`` closes a fence opened by ``marker``."""
    candidate = line.rstrip("\r\n")
    match = re.fullmatch(r" {0,3}([`~]+)[ \t]*", candidate)
    if match is None:
        return False
    closing = match.group(1)
    return closing[0] == marker[0] and len(closing) >= len(marker)


def _append_text(sections: list[ResponseSection], text: str) -> None:
    """Append text, coalescing adjacent text sections."""
    if not text:
        return
    if sections and isinstance(sections[-1], TextSection):
        sections[-1] = TextSection(sections[-1].text + text)
    else:
        sections.append(TextSection(text))


def split_latex_sections(message: str) -> list[ResponseSection]:
    """Split exact ``latex`` fences from ``message`` in source order.

    Other fenced blocks remain ordinary text. Empty, oversized, excess, and
    unclosed LaTeX fences also remain text so malformed model output is never
    dropped from the Discord reply.
    """
    if not message:
        return []

    sections: list[ResponseSection] = []
    text_buffer: list[str] = []
    latex_original: list[str] | None = None
    latex_source: list[str] = []
    latex_marker = ""
    ordinary_marker: str | None = None
    rendered_blocks = 0

    for line in message.splitlines(keepends=True):
        if latex_original is not None:
            latex_original.append(line)
            if _is_closing_fence(line, latex_marker):
                source = "".join(latex_source).strip()
                original = "".join(latex_original)
                if (
                    source
                    and len(source) <= MAX_LATEX_SOURCE_CHARS
                    and rendered_blocks < MAX_LATEX_BLOCKS
                ):
                    _append_text(sections, "".join(text_buffer))
                    text_buffer.clear()
                    sections.append(LatexSection(source=source, original=original))
                    rendered_blocks += 1
                else:
                    text_buffer.append(original)
                latex_original = None
                latex_source = []
                latex_marker = ""
            else:
                latex_source.append(line)
            continue

        if ordinary_marker is not None:
            text_buffer.append(line)
            if _is_closing_fence(line, ordinary_marker):
                ordinary_marker = None
            continue

        opening = _OPENING_FENCE_RE.match(line.rstrip("\r\n"))
        if opening is None:
            text_buffer.append(line)
            continue

        marker = opening.group("fence")
        info = opening.group("info").strip()
        if marker[0] == "`" and info.casefold() == "latex":
            latex_original = [line]
            latex_source = []
            latex_marker = marker
        else:
            text_buffer.append(line)
            ordinary_marker = marker

    if latex_original is not None:
        text_buffer.extend(latex_original)

    _append_text(sections, "".join(text_buffer))
    return sections


def has_latex_section(message: str) -> bool:
    """Return whether ``message`` contains at least one renderable fence."""
    return any(
        isinstance(section, LatexSection)
        for section in split_latex_sections(message)
    )
