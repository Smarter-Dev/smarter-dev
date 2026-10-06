"""Wire contract for removing one person from the chat bot (purge v1).

Shared by the three processes involved, so it lives in ``shared`` and imports
nothing heavier than Pydantic:

- the web/agent-worker tier writes the block list, purges guild memory and
  emits one :class:`PurgeCommand` per run on :data:`PURGE_STREAM`;
- the Discord bot (chat agent and embedded proactive agent) and the external
  proactive-agent worker each read the stream in their own consumer group,
  purge the history they hold, and acknowledge per guild through the bot API;
- both runtimes read the block list and report the revision they enforce under
  :func:`enforcing_key`, so a purge never starts before both stop reading the
  person's old messages.

The canonical JSON Schema is ``contracts/privacy/v1/purge_command.schema.json``,
byte-identical in both repositories. The command carries the target's ID and
names, so it is never logged and its stream entry is deleted once acknowledged.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import NonNegativeInt
from pydantic import StrictBool
from pydantic import StringConstraints

PURGE_STREAM = "privacy:v1:purge"
BOT_CONSUMER_GROUP = "smarter-dev-bot"
WORKER_CONSUMER_GROUP = "proactive-agent-workers-v1-privacy"
RUNTIME_COMPONENTS = ("bot", "worker")
# What a blocked author's message becomes wherever it would reach a model.
BLOCKED_PLACEHOLDER = "[BLOCKED BY USER]"
# How long a runtime's "I enforce revision N" report lasts without a refresh.
ENFORCING_TTL_SECONDS = 180

# ASCII names shorter than this match too much ordinary text to be checked
# deterministically: they are reported as unchecked (the run ends in review);
# the agent still sees them in the purge prompt. Other names match at any length.
MIN_CHECKED_NAME_CHARS = 2
# How deep the check descends into JSON stored inside JSON strings.
MAX_JSON_DEPTH = 5

SNOWFLAKE_PATTERN = r"^[0-9]{15,22}$"
Snowflake = Annotated[str, StringConstraints(pattern=SNOWFLAKE_PATTERN)]
PurgeName = Annotated[str, StringConstraints(min_length=1, max_length=100)]

AckComponent = Literal["bot", "worker"]
AckStep = Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")]
AckOutcome = Literal["purged", "unchanged", "failed"]


def enforcing_key(component: str) -> str:
    """Aggregate key a runtime publishes: the MIN over its live processes.

    The web does not trust it; it reads :func:`enforcing_process_pattern`.
    """
    return f"privacy:v1:enforcing:{component}"


def enforcing_process_pattern(component: str) -> str:
    """SCAN pattern for each process's own report, ``...:{component}:{host-pid}`` (EX 180)."""
    return f"{enforcing_key(component)}:*"


def consumer_key(component: str, process_id: str) -> str:
    """Written (EX 180) by a runtime's purge consumer loop on every iteration."""
    return f"privacy:v1:consumer:{component}:{process_id}"


def consumer_pattern(component: str) -> str:
    return f"privacy:v1:consumer:{component}:*"


def history_tombstone_key(guild_id: str) -> str:
    """Set by the worker when a purge left the guild's v1 history half-written.

    Value: JSON ``{"run_id": "...", "request_id": "..."}``; an older plain
    value means "unknown run". Cleared by the next successful v1 write.
    """
    return f"proactive:v1:{{guild:{guild_id}}}:history-invalid"


def purge_epoch_key(guild_id: str) -> str:
    """Bumped after every history purge of a guild; runtimes reload when it moves."""
    return f"proactive:v1:{{guild:{guild_id}}}:purge-epoch"


class PurgeCommand(BaseModel):
    """One purge run, as both runtimes read it from :data:`PURGE_STREAM`."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    request_id: UUID
    run_id: UUID
    user_id: Snowflake
    names: list[PurgeName] = Field(max_length=20)
    guild_ids: list[Snowflake] = Field(min_length=1, max_length=500)
    created_at: AwareDatetime

    def __repr__(self) -> str:
        return f"PurgeCommand(run_id={self.run_id}, guilds={len(self.guild_ids)})"

    __str__ = __repr__


class PurgeAck(BaseModel):
    """One runtime's result for one guild of a run (``POST .../acks``).

    ``detail`` is for operators and must carry no message content and no
    names: counts, store names and error classes only.
    """

    model_config = ConfigDict(extra="forbid")

    component: AckComponent
    guild_id: Snowflake
    outcome: AckOutcome
    stores: list[Annotated[str, StringConstraints(max_length=100)]] = Field(
        default_factory=list, max_length=50
    )
    detail: str = Field(default="", max_length=500)
    # Structured results: the web decides from these alone (the detail is
    # for display). Any of the first four missing flags the ack for review.
    name_hits: dict[AckStep, NonNegativeInt] | None = Field(default=None, max_length=10)
    tombstoned: StrictBool | None = None
    unchecked_names: NonNegativeInt | None = None
    done_record: Literal["written", "replayed", "not_written"] | None = None

    def flags(self) -> list[str]:
        """Why this ack needs review (empty when it does not)."""
        return ack_flags(self.model_dump())


STRUCTURED_ACK_FIELDS = ("name_hits", "tombstoned", "unchecked_names", "done_record")


def ack_flags(ack: dict) -> list[str]:
    """Why a stored ack (a dict of PurgeAck fields) needs review: short reasons,
    no content. Decided from the structured fields only, never the detail."""
    reasons = []
    if ack.get("outcome") == "failed":
        reasons.append("failed")
    missing = [f for f in STRUCTURED_ACK_FIELDS if ack.get(f) is None]
    if missing:
        reasons.append("missing structured fields")
    hits = sum((ack.get("name_hits") or {}).values())
    if hits:
        reasons.append(f"{hits} name hit(s)")
    if ack.get("tombstoned"):
        reasons.append("history tombstoned")
    if ack.get("unchecked_names"):
        reasons.append(f"{ack['unchecked_names']} unchecked name(s)")
    return reasons


class BlockedUsers(BaseModel):
    """The block list as both runtimes read it (``GET /api/privacy/blocked-users``)."""

    revision: int = Field(ge=0)
    user_ids: list[Snowflake]
    # People who opted back in (#92): their messages written before the time
    # stay hidden. Never holds someone in ``user_ids``.
    read_from: dict[Snowflake, AwareDatetime] = Field(default_factory=dict)


_ASCII_NAME = re.compile(r"[a-z0-9_ ]+")


def normalize_for_match(text: str) -> str:
    """Matcher normalisation (privacy:v1): NFC, then casefold."""
    return unicodedata.normalize("NFC", text).casefold()


def _clean_name(name: str) -> str:
    # Strip only: a nickname can hold a tab, a newline, a quote or a
    # backslash, and the matcher must look for exactly that.
    return name.strip()


@dataclass(frozen=True)
class PurgeTarget:
    """The person being removed: their Discord ID and the names they went by.

    Matching is deliberately dumb and deterministic — it is the check that does
    not take the agent's word for it. The matcher (privacy:v1, shared with the
    bot and copied by the worker with ``contracts/privacy/v1/name_matcher_vectors.json``):

    - The ID matches as a whole number (no digit on either side).
    - Names are stripped (nothing inside them changes: a tab, newline, quote
      or backslash in a nickname is matched as itself) and empty ones dropped. Text and names are both NFC-normalised, then casefolded.
    - A name made only of ASCII ``[a-z0-9_]`` and spaces matches where it is
      not preceded or followed by an ASCII letter: ``alice2``, ``alice_dev``
      and ``2alice`` hit, ``malice`` and ``alicea`` do not.
    - Any other name (CJK, emoji, accents, punctuation) matches as a plain
      substring, at any length.
    - An ASCII name shorter than 2 characters is never matched; it is listed
      in :attr:`unchecked_names`, and a run with one ends in review.

    A name can match a different person, which is why name hits are something
    the agent must explain, not an automatic fail.
    """

    user_id: str
    names: tuple[str, ...] = ()

    @classmethod
    def build(cls, user_id: str, names: list[str] | tuple[str, ...]) -> PurgeTarget:
        seen: dict[str, str] = {}
        for name in names:
            cleaned = _clean_name(name)
            key = normalize_for_match(cleaned)
            if cleaned and key not in seen:
                seen[key] = cleaned
        return cls(user_id=user_id.strip(), names=tuple(seen.values()))

    def _patterns(self) -> list[tuple[str, re.Pattern | None]]:
        patterns = []
        for name in self.names:
            norm = normalize_for_match(_clean_name(name))
            if not norm:
                continue
            if _ASCII_NAME.fullmatch(norm):
                if len(norm) < MIN_CHECKED_NAME_CHARS:
                    continue
                patterns.append((norm, re.compile(rf"(?<![a-z]){re.escape(norm)}(?![a-z])")))
            else:
                patterns.append((norm, None))
        return patterns

    @property
    def checked_names(self) -> tuple[str, ...]:
        unchecked = set(self.unchecked_names)
        return tuple(n for n in self.names if n not in unchecked)

    @property
    def unchecked_names(self) -> tuple[str, ...]:
        """ASCII names too short to match deterministically."""
        out = []
        for name in self.names:
            norm = normalize_for_match(_clean_name(name))
            if norm and _ASCII_NAME.fullmatch(norm) and len(norm) < MIN_CHECKED_NAME_CHARS:
                out.append(name)
        return tuple(out)

    def id_hits(self, text: str) -> int:
        if not text or not self.user_id:
            return 0
        return len(re.findall(rf"(?<!\d){re.escape(self.user_id)}(?!\d)", text))

    def name_hits(self, text: str) -> int:
        if not text:
            return 0
        norm = normalize_for_match(text)
        hits = 0
        for name, pattern in self._patterns():
            hits += len(pattern.findall(norm)) if pattern is not None else norm.count(name)
        return hits

    def mentions(self, text: str) -> bool:
        return bool(self.id_hits(text) or self.name_hits(text))

    def value_hits(self, value: object, *, _depth: int = 0) -> tuple[int, int]:
        """(id hits, name hits) over every string in a decoded JSON value.

        Never searches a serialised form: a name holding ``"``, ``\\``, a tab
        or a newline is escaped there and would not match. A string that is
        itself JSON is decoded and searched (up to :data:`MAX_JSON_DEPTH`
        levels); if decoding leaves no hit the raw string is searched too, so
        decoding can never hide one. A string that is not JSON is searched raw.
        """
        if isinstance(value, str):
            raw = (self.id_hits(value), self.name_hits(value))
            inner = _embedded_json(value) if _depth < MAX_JSON_DEPTH else None
            if inner is None or inner == value:
                return raw
            decoded = self.value_hits(inner, _depth=_depth + 1)
            return (max(raw[0], decoded[0]), max(raw[1], decoded[1]))
        if isinstance(value, dict):
            ids = names = 0
            for key, child in value.items():
                for part in (str(key), child):
                    i, n = self.value_hits(part, _depth=_depth)
                    ids, names = ids + i, names + n
            return ids, names
        if isinstance(value, list | tuple):
            ids = names = 0
            for child in value:
                i, n = self.value_hits(child, _depth=_depth)
                ids, names = ids + i, names + n
            return ids, names
        if isinstance(value, int | float) and not isinstance(value, bool):
            return self.id_hits(str(value)), 0
        return 0, 0

    def stored_hits(self, raw: str | bytes) -> tuple[int, int]:
        """(id hits, name hits) in one stored value.

        A JSON value is decoded and every string leaf searched, so a name after
        an escape (``\\nAlice``) or stored ASCII-escaped (``\\u00e9``) is found;
        anything that is not JSON is searched as raw text.
        """
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            return self.id_hits(text), self.name_hits(text)
        if not isinstance(value, dict | list | str):
            # A bare number: the ID itself can be one.
            return self.id_hits(text), self.name_hits(text)
        decoded = self.value_hits(value, _depth=1)
        if decoded != (0, 0):
            return decoded
        # Decoding hid nothing it found; the raw text gets its own look.
        return self.id_hits(text), self.name_hits(text)

    def __repr__(self) -> str:
        # Never print who is being purged, even by accident in a traceback.
        return f"PurgeTarget(names={len(self.names)})"

    __str__ = __repr__


def _embedded_json(text: str):
    stripped = text.strip()
    if not stripped or stripped[0] not in '[{"':
        return None
    try:
        value = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict | list | str) else None


def string_leaves(value: object, *, max_depth: int = MAX_JSON_DEPTH) -> Iterator[str]:
    """Every string in a decoded JSON value: dict keys, values, list items.

    A string that is itself JSON (pydantic-ai stores tool-call ``args`` as a
    JSON string, and tool returns can be JSON text) is decoded and descended
    into instead, up to ``max_depth`` levels. Numbers are yielded as their
    text too, since a Discord ID can be stored as one.
    """
    stack: list[tuple[object, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            inner = _embedded_json(item) if depth < max_depth else None
            if inner is None or inner == item:
                yield item
            else:
                stack.append((inner, depth + 1))
        elif isinstance(item, dict):
            for key, child in item.items():
                stack.append((str(key), depth))
                stack.append((child, depth))
        elif isinstance(item, list | tuple):
            stack.extend((child, depth) for child in item)
        elif isinstance(item, int | float) and not isinstance(item, bool):
            yield str(item)
