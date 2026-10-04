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
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import StringConstraints

PURGE_STREAM = "privacy:v1:purge"
BOT_CONSUMER_GROUP = "smarter-dev-bot"
WORKER_CONSUMER_GROUP = "proactive-agent-workers-v1-privacy"
RUNTIME_COMPONENTS = ("bot", "worker")
# What a blocked author's message becomes wherever it would reach a model.
BLOCKED_PLACEHOLDER = "[BLOCKED BY USER]"
# How long a runtime's "I enforce revision N" report lasts without a refresh.
ENFORCING_TTL_SECONDS = 180

# Names shorter than this match too much ordinary text to be checked
# deterministically; the agent still sees them in the purge prompt.
MIN_CHECKED_NAME_CHARS = 2

SNOWFLAKE_PATTERN = r"^[0-9]{15,22}$"
Snowflake = Annotated[str, StringConstraints(pattern=SNOWFLAKE_PATTERN)]
PurgeName = Annotated[str, StringConstraints(min_length=1, max_length=100)]

AckComponent = Literal["bot", "worker"]
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


class BlockedUsers(BaseModel):
    """The block list as both runtimes read it (``GET /api/privacy/blocked-users``)."""

    revision: int = Field(ge=0)
    user_ids: list[Snowflake]


@dataclass(frozen=True)
class PurgeTarget:
    """The person being removed: their Discord ID and the names they went by.

    Matching is deliberately dumb and deterministic — it is the check that does
    not take the agent's word for it. The ID matches as a whole number, a name
    as a whole word in any case. A name can match a different person, which is
    why name hits are something the agent must explain, not an automatic fail.
    """

    user_id: str
    names: tuple[str, ...] = ()

    @classmethod
    def build(cls, user_id: str, names: list[str] | tuple[str, ...]) -> PurgeTarget:
        seen: dict[str, str] = {}
        for name in names:
            cleaned = " ".join(name.split())
            if cleaned and cleaned.casefold() not in seen:
                seen[cleaned.casefold()] = cleaned
        return cls(user_id=user_id.strip(), names=tuple(seen.values()))

    @property
    def checked_names(self) -> tuple[str, ...]:
        return tuple(n for n in self.names if len(n) >= MIN_CHECKED_NAME_CHARS)

    def id_hits(self, text: str) -> int:
        if not text or not self.user_id:
            return 0
        return len(re.findall(rf"(?<!\d){re.escape(self.user_id)}(?!\d)", text))

    def name_hits(self, text: str) -> int:
        if not text:
            return 0
        return sum(
            len(re.findall(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE))
            for name in self.checked_names
        )

    def mentions(self, text: str) -> bool:
        return bool(self.id_hits(text) or self.name_hits(text))

    def value_hits(self, value: object) -> tuple[int, int]:
        """(id hits, name hits) over every string leaf of a decoded JSON value."""
        ids = names = 0
        for leaf in string_leaves(value):
            ids += self.id_hits(leaf)
            names += self.name_hits(leaf)
        return ids, names

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
        return self.value_hits(value)

    def __repr__(self) -> str:
        # Never print who is being purged, even by accident in a traceback.
        return f"PurgeTarget(names={len(self.names)})"

    __str__ = __repr__


def string_leaves(value: object) -> Iterator[str]:
    """Every string in a decoded JSON value: dict keys, values, list items.

    Numbers are yielded as their text too, since a Discord ID can be stored
    as one.
    """
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            for key, child in item.items():
                stack.append(str(key))
                stack.append(child)
        elif isinstance(item, list | tuple):
            stack.extend(item)
        elif isinstance(item, int | float) and not isinstance(item, bool):
            yield str(item)
