"""How the chat agent names a person in what it keeps (#104).

Memory, behavior, personality and notes name a member only as a tag,
``<userid:username>`` (``<123456789012345678:zech>``), so the public
``/chat-agent`` page can mask every person without a model call. Someone the
agent has only heard about, with no id to give, is ``<:username>``. The form
before #104, ``username (id N)``, is still read everywhere until the stored
blocks are rewritten (``scripts/retag_chat_agent_memory.py``), and a Discord
mention ``<@N>`` names a person too.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import dataclass

# ``<id:username>``, or ``<:username>`` with no id. The username runs to the
# closing bracket, so a display name with spaces or punctuation stays one tag;
# an id-less one has no colon in it, so a custom emoji ``<:name:123>`` is not
# a tag.
MEMBER_TAG = re.compile(
    r"<(?:(?P<tag_id>[0-9]{1,22}):(?P<tag_name>[^<>\n]{1,64})"
    r"|:(?P<heard_name>[^<>:\n]{1,64}))>"
)
# ``username (id N)``, the dream's form before #104.
LEGACY_MEMBER = re.compile(
    r"(?P<legacy_name>[\w.\-]{2,32})\s*\(\s*id\s*(?P<legacy_id>[0-9]{1,22})\s*\)",
    re.IGNORECASE,
)
# ``<@N>`` / ``<@!N>``: a user mention, which carries no name.
USER_MENTION = re.compile(r"<@!?(?P<mention_id>[0-9]{1,22})>")
# Any of the three, in one pass so a match is never read twice.
_ANY_MEMBER = re.compile(
    "|".join(
        f"(?:{pattern.pattern})"
        for pattern in (MEMBER_TAG, LEGACY_MEMBER, USER_MENTION)
    ),
    re.IGNORECASE,
)
_UNSAFE_NAME = re.compile(r"[<>\n\r]+")
MAX_TAG_NAME_CHARS = 64


@dataclass(frozen=True)
class MemberRef:
    """One reference to a person: their id, if it gave one, and their name,
    if it gave one. A tag without an id is someone the agent only heard of."""

    user_id: str | None
    username: str | None


def member_tag(user_id: object, username: str) -> str:
    """``<user_id:username>``, with the name made safe to sit in a tag."""
    name = _UNSAFE_NAME.sub(" ", username or "").strip()[:MAX_TAG_NAME_CHARS].strip()
    return f"<{user_id}:{name or user_id}>"


def _ref(match: re.Match[str]) -> MemberRef:
    found = match.groupdict()
    if found["tag_id"] is not None:
        return MemberRef(found["tag_id"], found["tag_name"].strip() or None)
    if found["heard_name"] is not None:
        return MemberRef(None, found["heard_name"].strip() or None)
    if found["legacy_id"] is not None:
        return MemberRef(found["legacy_id"], found["legacy_name"].strip(".-") or None)
    return MemberRef(found["mention_id"], None)


def is_tag(match: re.Match[str]) -> bool:
    found = match.groupdict()
    return found["tag_id"] is not None or found["heard_name"] is not None


def split_members(text: str) -> tuple[str | MemberRef, ...]:
    """``text`` as runs of plain text and the people it references, in order."""
    parts: list[str | MemberRef] = []
    last = 0
    for match in _ANY_MEMBER.finditer(text or ""):
        if match.start() > last:
            parts.append(text[last : match.start()])
        parts.append(_ref(match))
        last = match.end()
    if last < len(text or ""):
        parts.append(text[last:])
    return tuple(parts)


def replace_members(text: str, render: Callable[[MemberRef], str]) -> str:
    """``text`` with every tag, old-form name and mention replaced by ``render``."""
    return _ANY_MEMBER.sub(lambda match: render(_ref(match)), text or "")


def referenced_members(*texts: str) -> list[MemberRef]:
    """Every reference in ``texts``, in order."""
    return [_ref(match) for text in texts for match in _ANY_MEMBER.finditer(text or "")]


def tagged_names(*texts: str) -> set[str]:
    """Every name ``texts`` give a person in a tag or the old form, casefolded."""
    return {
        ref.username.casefold()
        for ref in referenced_members(*texts)
        if ref.username is not None
    }


def names_outside_tags(text: str, names: Iterable[str]) -> list[str]:
    """Which of ``names`` (casefolded) ``text`` carries outside a tag, matched
    as whole words. A name in the old ``username (id N)`` form is outside one."""
    rest = MEMBER_TAG.sub(" ", text or "").casefold()
    return sorted(
        name
        for name in set(names)
        if name and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", rest)
    )


def retag(text: str, names_by_id: Mapping[str, str] | None = None) -> tuple[str, int]:
    """``text`` with the old form, and every mention whose name is known, as tags.

    ``names_by_id`` resolves a mention; a mention it cannot resolve is left as
    it is. Tags, id-less ones included, are left exactly as they are, and none
    is invented, so a second pass changes nothing. Returns the new text and how
    many references it converted.
    """
    names_by_id = names_by_id or {}
    converted = 0

    def convert(match: re.Match[str]) -> str:
        nonlocal converted
        if is_tag(match):
            return match[0]
        ref = _ref(match)
        name = ref.username or names_by_id.get(ref.user_id)
        if not name:
            return match[0]
        converted += 1
        return member_tag(ref.user_id, name)

    return _ANY_MEMBER.sub(convert, text or ""), converted


def heard_tag(username: str) -> str:
    """``<:username>``, for someone with no id; the name must not hold a colon."""
    return f"<:{username}>"


def tag_name(text: str, username: str, user_id: str | None = None) -> tuple[str, int]:
    """``text`` with every whole-word ``username`` outside a reference as a tag.

    Case-insensitive, like the page's name check. A tag, an old-form name or a
    mention is left exactly as it is, so a second pass changes nothing. With
    ``user_id`` the tag is ``<user_id:username>``, without it ``<:username>``.
    Returns the new text and how many it tagged.
    """
    tag = member_tag(user_id, username) if user_id else heard_tag(username)
    bare = re.compile(rf"(?<!\w){re.escape(username)}(?!\w)", re.IGNORECASE)
    either = re.compile(
        rf"(?P<reference>{_ANY_MEMBER.pattern})|{bare.pattern}", re.IGNORECASE
    )
    tagged = 0

    def convert(match: re.Match[str]) -> str:
        nonlocal tagged
        if match["reference"] is not None:
            return match[0]
        tagged += 1
        return tag

    return either.sub(convert, text or ""), tagged
