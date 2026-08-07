"""Split LaTeX out of a chatbot response without changing its source.

Two forms are recognised. The legacy ```latex fenced block is still honoured so
older prompts do not regress. In addition, expressions written with the standard
math delimiters ``$$…$$``, ``\\[…\\]``, and ``\\(…\\)`` are pulled out and rendered
the same way — smaller models emit those far more reliably than the exact fence
convention. A bare single ``$`` is deliberately never treated as math so ordinary
currency text (``$5``) passes straight through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_LATEX_BLOCKS = 5
MAX_LATEX_SOURCE_CHARS = 1_800

_OPENING_FENCE_RE = re.compile(r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$")

# Delimiter openers mapped to the closer that ends them. ``$$`` is tried before
# any single-character match so a display block is never mis-read as two empty
# single-dollar pairs; single ``$`` is absent on purpose (see module docstring).
_DELIMITER_PAIRS: tuple[tuple[str, str], ...] = (
    ("$$", "$$"),
    ("\\[", "\\]"),
    ("\\(", "\\)"),
)


@dataclass(frozen=True)
class TextSection:
    """Ordinary Discord markdown that should be sent as text."""

    text: str


@dataclass(frozen=True)
class LatexSection:
    """The source and lossless fallback for one equation."""

    source: str
    original: str


ResponseSection = TextSection | LatexSection


@dataclass(frozen=True)
class _TextPiece:
    """Literal text that must never be scanned for math delimiters."""

    text: str


@dataclass(frozen=True)
class _LatexCandidate:
    """A recognised equation before block-count and size limits are applied."""

    source: str
    original: str


_MessagePiece = _TextPiece | _LatexCandidate


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


def _match_delimiter(text: str, index: int) -> tuple[str | None, str | None]:
    """Return the (opener, closer) beginning at ``index``, or ``(None, None)``."""
    for opener, closer in _DELIMITER_PAIRS:
        if text.startswith(opener, index):
            return opener, closer
    return None, None


def _scan_delimited_span(text: str) -> list[_MessagePiece]:
    """Break a fence-free span into text and delimited-equation candidates.

    The span is guaranteed to contain no fenced code, so every ``$$``/``\\[``/
    ``\\(`` opener is eligible. Unclosed or empty pairs, and every bare single
    ``$``, stay literal so no reply content is ever dropped.
    """
    pieces: list[_MessagePiece] = []
    literal: list[str] = []
    index = 0
    length = len(text)

    while index < length:
        # An escaped dollar is literal and must not seed a ``$$`` pair.
        if text[index] == "\\" and index + 1 < length and text[index + 1] == "$":
            literal.append(text[index : index + 2])
            index += 2
            continue

        opener, closer = _match_delimiter(text, index)
        if opener is None:
            literal.append(text[index])
            index += 1
            continue

        content_start = index + len(opener)
        close_at = text.find(closer, content_start)
        if close_at == -1:
            # Unclosed opener — the first character is literal; keep scanning
            # after it.
            literal.append(text[index])
            index += 1
            continue

        source = text[content_start:close_at].strip()
        if not source:
            literal.append(text[index])
            index += 1
            continue

        if literal:
            pieces.append(_TextPiece("".join(literal)))
            literal = []
        original = text[index : close_at + len(closer)]
        pieces.append(_LatexCandidate(source=source, original=original))
        index = close_at + len(closer)

    if literal:
        pieces.append(_TextPiece("".join(literal)))
    return pieces


def _message_pieces(message: str) -> list[_MessagePiece]:
    """Carve ``message`` into fenced blocks and scanned plain-text spans.

    ```latex fences become equation candidates; every other fenced block stays
    opaque text; the plain text between fences is scanned for math delimiters.
    """
    pieces: list[_MessagePiece] = []
    plain_span: list[str] = []
    latex_lines: list[str] | None = None
    latex_content: list[str] = []
    latex_marker = ""
    ordinary_lines: list[str] | None = None
    ordinary_marker = ""

    def flush_plain_span() -> None:
        if plain_span:
            pieces.extend(_scan_delimited_span("".join(plain_span)))
            plain_span.clear()

    for line in message.splitlines(keepends=True):
        if latex_lines is not None:
            latex_lines.append(line)
            if _is_closing_fence(line, latex_marker):
                pieces.append(
                    _LatexCandidate(
                        source="".join(latex_content).strip(),
                        original="".join(latex_lines),
                    )
                )
                latex_lines = None
                latex_content = []
                latex_marker = ""
            else:
                latex_content.append(line)
            continue

        if ordinary_lines is not None:
            ordinary_lines.append(line)
            if _is_closing_fence(line, ordinary_marker):
                pieces.append(_TextPiece("".join(ordinary_lines)))
                ordinary_lines = None
                ordinary_marker = ""
            continue

        opening = _OPENING_FENCE_RE.match(line.rstrip("\r\n"))
        if opening is None:
            plain_span.append(line)
            continue

        # A fence starts here — flush any pending plain text before it so order
        # is preserved.
        flush_plain_span()
        marker = opening.group("fence")
        info = opening.group("info").strip()
        if marker[0] == "`" and info.casefold() == "latex":
            latex_lines = [line]
            latex_content = []
            latex_marker = marker
        else:
            ordinary_lines = [line]
            ordinary_marker = marker

    # An unterminated fence at end of message is literal text, never dropped.
    if latex_lines is not None:
        pieces.append(_TextPiece("".join(latex_lines)))
    elif ordinary_lines is not None:
        pieces.append(_TextPiece("".join(ordinary_lines)))
    flush_plain_span()
    return pieces


def split_latex_sections(message: str) -> list[ResponseSection]:
    """Split renderable LaTeX from ``message`` in source order.

    ```latex fences and ``$$…$$``/``\\[…\\]``/``\\(…\\)`` delimiters become
    ``LatexSection``s; everything else is text. Empty, oversized, and excess
    expressions fall back to text so malformed model output is never dropped from
    the Discord reply. The block-count and size limits apply across both forms
    combined, in source order.
    """
    if not message:
        return []

    sections: list[ResponseSection] = []
    rendered_blocks = 0
    for piece in _message_pieces(message):
        if isinstance(piece, _TextPiece):
            _append_text(sections, piece.text)
            continue
        if (
            piece.source
            and len(piece.source) <= MAX_LATEX_SOURCE_CHARS
            and rendered_blocks < MAX_LATEX_BLOCKS
        ):
            sections.append(
                LatexSection(source=piece.source, original=piece.original)
            )
            rendered_blocks += 1
        else:
            _append_text(sections, piece.original)
    return sections


def has_latex_section(message: str) -> bool:
    """Return whether ``message`` contains at least one renderable equation."""
    return any(
        isinstance(section, LatexSection)
        for section in split_latex_sections(message)
    )
