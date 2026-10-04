"""Edit-based rewriting for a privacy purge (privacy:v1), shared by every store.

A purge never lets a model return a whole block of text. The text is cut into
segments; the model returns one decision per segment that mentions the person
(keep, remove, or rewrite with new text), and :func:`apply_edits` rebuilds the
text from the original bytes. Everything that does not mention the person
comes back byte for byte, in order, with its duplicates, and nothing can be
added. Used by the web's guild-memory purge, the bot's topic, notes and watch
instructions, and (copied verbatim) the external worker's watch instructions.

Segments: lines; sentences within a line (split after ``.``, ``!`` or ``?``
plus whitespace); inside a sentence that mentions the person, its parts
between ``;``; inside such a part that also has two or more commas (a list),
its items between ``,``. A cut that would split a name is not made.

Edits: exactly one per mentioning segment, by id ``{location}:{n}``.

- ``remove``;
- ``rewrite`` with ``text``: one non-empty line (``str.splitlines`` gives
  one, no control characters); the segment's indentation and list marker are
  kept;
- ``keep``.

Removing a segment takes the separator before it (or after it, when it was
first); the first segment's prefix moves to the first survivor; each line's
trailing whitespace and line ending are kept. A line whose segments are all
removed goes with its line break.

Refused (:class:`SegmentEditError`): edits that do not match the listed
segments one to one, a rewrite that is not one line, the person's ID left
anywhere in a text. A text may come back empty (a note about only the person
is deleted); the caller refuses that for blocks, which are never reset. A
surviving name is not refused here; the caller retries, then stores the text
and reports the name hit.

Imports only the stdlib, pydantic and the shared matcher.
Test vectors: ``contracts/privacy/v1/segment_edit_vectors.json``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from smarter_dev.shared.privacy_purge import PurgeTarget

_SENTENCE_SPLIT = re.compile(r"((?<=[.!?])\s+)")
_SEMICOLON_SPLIT = re.compile(r"(;\s*)")
_COMMA_SPLIT = re.compile(r"(,\s*)")
# What a rewrite never touches: indentation and a list marker.
_PREFIX = re.compile(r"\s*(?:[-*+•]\s+|\d+[.)]\s+)?")


class SegmentEditError(ValueError):
    """The edits cannot be applied. The message is for the model; it never
    quotes stored text, the ID or a name."""


class SegmentEdit(BaseModel):
    """The decision for one segment that mentions the person."""

    id: str
    action: Literal["keep", "remove", "rewrite"]
    text: str | None = None


def _hits(target: PurgeTarget, text: str) -> int:
    return target.id_hits(text) + target.name_hits(text)


def _join_cut_names(pieces: list[str], target: PurgeTarget) -> list[str]:
    """Re-join neighbouring pieces (segment, separator, segment, ...) where the
    cut between them split a name ("Dr. Kai" must not become "Dr." and "Kai")."""
    out = [pieces[0]]
    for i in range(1, len(pieces), 2):
        sep, nxt = pieces[i], pieces[i + 1]
        joined = out[-1] + sep + nxt
        if _hits(target, joined) > _hits(target, out[-1]) + _hits(target, nxt):
            out[-1] = joined
        else:
            out.extend((sep, nxt))
    return out


def _split_mentioning(sentence: str, target: PurgeTarget) -> list[str]:
    """Pieces (segment, separator, segment, ...) of one sentence."""
    if not target.mentions(sentence):
        return [sentence]
    out: list[str] = []
    for i, part in enumerate(_SEMICOLON_SPLIT.split(sentence)):
        if i % 2 or not target.mentions(part) or part.count(",") < 2:
            out.append(part)
        else:
            out.extend(_COMMA_SPLIT.split(part))
    # A name that itself contains a separator must not be cut in two.
    if sum(_hits(target, seg) for seg in out[0::2]) != _hits(target, sentence):
        return [sentence]
    return out


_TAIL = re.compile(r"\s*\Z")


def _split_tail(line: str) -> tuple[str, str]:
    """A line's text and its trailing whitespace (``\r`` of a CRLF included),
    which no edit ever touches."""
    tail = _TAIL.search(line).group(0)
    return line[: len(line) - len(tail)], tail


def _line_pieces(line: str, target: PurgeTarget) -> list[str]:
    """Pieces (segment, separator, segment, ...) of a line without its tail.

    The list marker is set aside first, so "1. " is never taken for the end
    of a sentence; it stays at the front of the first segment.
    """
    prefix = _PREFIX.match(line).group(0)
    sentences = _join_cut_names(_SENTENCE_SPLIT.split(line[len(prefix) :]), target)
    sentences[0] = prefix + sentences[0]
    out: list[str] = []
    for i, piece in enumerate(sentences):
        if i % 2:
            out.append(piece)
        else:
            out.extend(_split_mentioning(piece, target))
    return out


@dataclass(frozen=True)
class Segment:
    id: str
    index: int
    text: str

    @property
    def prefix(self) -> str:
        return _PREFIX.match(self.text).group(0)

    @property
    def body(self) -> str:
        return self.text[len(self.prefix) :]


def segments(location: str, text: str, target: PurgeTarget) -> list[Segment]:
    """Every segment of ``text``, numbered in order; ids are ``{location}:{n}``."""
    out: list[Segment] = []
    for line in text.split("\n"):
        body, _tail = _split_tail(line)
        for piece in _line_pieces(body, target)[0::2]:
            out.append(Segment(id=f"{location}:{len(out)}", index=len(out), text=piece))
    return out


def editable_segments(location: str, text: str, target: PurgeTarget) -> list[Segment]:
    """The segments that mention the person: the only ones the model decides."""
    return [seg for seg in segments(location, text, target) if target.mentions(seg.text)]


def rebuild(text: str, target: PurgeTarget, decisions: Mapping[int, str | None]) -> str:
    """``text`` with segment ``i`` removed (``None``) or replaced, all else byte for byte.

    Removing a segment takes the separator before it, or the one after it
    when nothing is left before it on the line; so between two survivors
    stands the separator that stood right before the second. The first
    segment's indentation and list marker move to the first survivor. A
    line's trailing whitespace and line ending are kept, except that when the
    last line goes, the line left last loses its whole terminator (``\r\n``
    as well as ``\n``). A line whose segments are all removed goes with its
    line break.
    """
    if not decisions:
        return text
    out_lines: list[str] = []
    lines = text.split("\n")
    last_kept = -1
    index = 0
    for number, line in enumerate(lines):
        body, tail = _split_tail(line)
        pieces = _line_pieces(body, target)
        segs, seps = pieces[0::2], pieces[1::2]
        positions = range(index, index + len(segs))
        index += len(segs)
        if not any(i in decisions for i in positions):
            out_lines.append(line)
            last_kept = number
            continue
        survivors: list[tuple[str, int]] = []
        for j, i in enumerate(positions):
            if i not in decisions:
                survivors.append((segs[j], j))
            elif decisions[i] is not None:
                survivors.append((decisions[i], j))
        if not any(text_.strip() for text_, _ in survivors):
            continue
        parts: list[str] = []
        for n, (text_, j) in enumerate(survivors):
            if n == 0:
                if j > 0:
                    # The line's first segment went: the line's own prefix
                    # (indentation, list marker) moves to the first survivor,
                    # whose text is kept whole (a "1) " inside it is its own).
                    text_ = _PREFIX.match(segs[0]).group(0) + text_
            else:
                parts.append(seps[j - 1])
            parts.append(text_)
        out_lines.append("".join(parts) + tail)
        last_kept = number
    if out_lines and last_kept < len(lines) - 1 and out_lines[-1].endswith("\r"):
        # The old last line went, so the line now last loses its terminator
        # whole: the "\r" of its "\r\n" goes with the "\n".
        out_lines[-1] = out_lines[-1][:-1]
    return "\n".join(out_lines)


def _single_line(text: str) -> bool:
    return len(text.splitlines()) == 1 and not any(
        unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in text
    )


def editable_by_location(
    texts: Mapping[str, str], target: PurgeTarget
) -> dict[str, list[Segment]]:
    """The segments to decide, per location, for every text that mentions the person."""
    return {
        location: editable_segments(location, text, target)
        for location, text in texts.items()
        if target.mentions(text)
    }


def apply_edits(
    texts: Mapping[str, str],
    edits: Sequence[SegmentEdit],
    target: PurgeTarget,
) -> dict[str, str]:
    """Validate ``edits`` against ``texts`` and rebuild every text from them.

    ``texts`` maps a location (``memory``, ``note:<id>``, ``topic``...) to its
    current text. Returns every location's new text (unchanged ones byte for
    byte). Raises :class:`SegmentEditError` when the edits do not fit the
    segments, a rewrite is not one line, or the person's ID survives. A
    surviving *name* is not an error here: the caller reports it.
    """
    by_location = editable_by_location(texts, target)
    by_id = {seg.id: (location, seg) for location, segs in by_location.items() for seg in segs}

    decisions: dict[str, dict[int, str | None]] = {location: {} for location in by_location}
    seen: set[str] = set()
    for edit in edits:
        if edit.id not in by_id:
            raise SegmentEditError(
                f"{edit.id!r} is not a segment you were asked to decide; only listed "
                "segments can change."
            )
        if edit.id in seen:
            raise SegmentEditError(f"Segment {edit.id!r} has two entries; give it one.")
        seen.add(edit.id)
        location, seg = by_id[edit.id]
        if edit.action == "remove":
            decisions[location][seg.index] = None
        elif edit.action == "rewrite":
            text = (edit.text or "").strip()
            # One line only: a line break (in any form) could add a heading.
            if not text or not _single_line(text):
                raise SegmentEditError(
                    f"Rewritten segment {edit.id!r} must be one line of text with no "
                    "control characters; remove it if nothing is left."
                )
            body = _PREFIX.sub("", text, count=1) if seg.prefix.strip() else text
            decisions[location][seg.index] = seg.prefix + body
    missing = [seg_id for seg_id in by_id if seg_id not in seen]
    if missing:
        raise SegmentEditError(
            f"Every listed segment needs one entry. Missing: {', '.join(missing)}."
        )

    result: dict[str, str] = {}
    for location, text in texts.items():
        new = rebuild(text, target, decisions.get(location, {}))
        if location in by_location:
            if target.id_hits(new):
                raise SegmentEditError(f"`{location}` still carries this person's ID.")
        result[location] = new
    return result
