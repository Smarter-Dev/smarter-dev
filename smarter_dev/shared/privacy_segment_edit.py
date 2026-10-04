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
- ``rewrite`` with ``text``: one line, no ID or name (the shared matcher), not
  a placeholder, not longer and with no more sentences than the original; the
  segment's indentation and list marker are kept;
- ``keep``: only for a name hit (never the ID), and only when the location is
  in ``unresolved`` (a different person who shares the name).

A line whose segments are all removed goes with its line break. A text that
still mentions the person after the rebuild (a name no segment holds, such as
one spanning a line break) is refused unless its location is unresolved.

Imports only the stdlib, pydantic and the shared matcher.
Test vectors: ``contracts/privacy/v1/segment_edit_vectors.json``.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

from smarter_dev.shared.privacy_purge import PurgeTarget

# Text a prompt might show for "nothing here"; never written as a rewrite.
PLACEHOLDERS = frozenset({"(empty)", "(none)"})

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


def _line_pieces(line: str, target: PurgeTarget) -> list[str]:
    sentences = _join_cut_names(_SENTENCE_SPLIT.split(line), target)
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
        for piece in _line_pieces(line, target)[0::2]:
            out.append(Segment(id=f"{location}:{len(out)}", index=len(out), text=piece))
    return out


def editable_segments(location: str, text: str, target: PurgeTarget) -> list[Segment]:
    """The segments that mention the person: the only ones the model decides."""
    return [seg for seg in segments(location, text, target) if target.mentions(seg.text)]


def rebuild(text: str, target: PurgeTarget, decisions: Mapping[int, str | None]) -> str:
    """``text`` with segment ``i`` removed (``None``) or replaced, all else byte for byte.

    A line whose segments are all removed goes with its line break. Removing
    a segment takes its own following separator, or the one before it when
    it was the last left on its line.
    """
    if not decisions:
        return text
    out_lines: list[str] = []
    index = 0
    for line in text.split("\n"):
        pieces = _line_pieces(line, target)
        segs, seps = pieces[0::2], pieces[1::2]
        positions = range(index, index + len(segs))
        index += len(segs)
        if not any(i in decisions for i in positions):
            out_lines.append(line)
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
            parts.append(text_)
            if n < len(survivors) - 1:
                parts.append(seps[j] if j < len(seps) else " ")
        out_lines.append("".join(parts))
    return "\n".join(out_lines)


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
    *,
    unresolved: Collection[str] = (),
) -> dict[str, str]:
    """Validate ``edits`` against ``texts`` and rebuild every text from them.

    ``texts`` maps a location (``memory``, ``note:<id>``, ``topic``...) to its
    current text. Returns every location's new text (unchanged ones byte for
    byte). Raises :class:`SegmentEditError` on any problem.
    """
    by_location = editable_by_location(texts, target)
    by_id = {seg.id: (location, seg) for location, segs in by_location.items() for seg in segs}
    unresolved = {location.strip() for location in unresolved}

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
            if not text or text in PLACEHOLDERS or "\n" in text:
                raise SegmentEditError(
                    f"Rewritten segment {edit.id!r} must be one line of real text; "
                    "remove it if nothing is left."
                )
            if target.mentions(text):
                raise SegmentEditError(
                    f"Rewritten segment {edit.id!r} still names this person or carries "
                    "their ID."
                )
            body = _PREFIX.sub("", text, count=1) if seg.prefix.strip() else text
            # A rewrite takes something out; it never adds a sentence or grows.
            if len(body) > len(seg.body.strip()) or len(_SENTENCE_SPLIT.split(body)) > len(
                _SENTENCE_SPLIT.split(seg.body.strip())
            ):
                raise SegmentEditError(
                    f"Rewritten segment {edit.id!r} is longer or has more sentences than the "
                    "original; a rewrite only takes this person out."
                )
            decisions[location][seg.index] = seg.prefix + body
        else:  # keep
            if target.id_hits(seg.text):
                raise SegmentEditError(
                    f"Segment {edit.id!r} carries this person's ID; it cannot stay."
                )
            if location not in unresolved:
                raise SegmentEditError(
                    f"Segment {edit.id!r} names this person. Remove or rewrite it, or if "
                    f"it is a different person who shares the name, list `{location}` "
                    "in unresolved and say why."
                )
    missing = [seg_id for seg_id in by_id if seg_id not in seen]
    if missing:
        raise SegmentEditError(
            f"Every listed segment needs one entry. Missing: {', '.join(missing)}."
        )

    result: dict[str, str] = {}
    for location, text in texts.items():
        new = rebuild(text, target, decisions.get(location, {}))
        if location in by_location:
            # A hit no segment holds (a name spanning a line break) cannot be
            # decided segment by segment: refuse rather than call it clean.
            if target.id_hits(new):
                raise SegmentEditError(f"`{location}` still carries this person's ID.")
            if target.name_hits(new) and location not in unresolved:
                raise SegmentEditError(
                    f"`{location}` still names this person in a place no segment covers; "
                    f"list `{location}` in unresolved if it is a different person."
                )
        result[location] = new
    return result
