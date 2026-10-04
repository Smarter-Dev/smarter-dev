"""FOLD PLAUSIBILITY RULE (privacy:v1), identical to the proactive-agent
worker's ``fold_plausibility_problem`` / ``fold_input_texts``.

A privacy fold must not "succeed" by emptying memory. Every field of a fold's
output is checked against the store it replaces; a rejection is a model retry
(2 of them), and when they run out the step fails and nothing is written.

1. A non-empty input requires an output (the bot: a field the model left out
   is rejected); an empty input requires an empty output.
2. A refusal (any ``FOLD_REFUSALS`` phrase, case-insensitive) is rejected
   anywhere; an output under 40 chars after stripping, or without letters,
   is rejected unless the bystander text B is empty (input wholly about the
   target).
3. B = "\\n".join of the input lines (every part split into lines) holding
   neither the target id (whole number) nor a name hit. If len(B) >= 200 the
   output needs max(40, min(ceil(0.05 * len(B)), 400)) chars.
4. U = distinct ``uid=N`` values in the input other than the target's. If U
   is not empty, ceil(0.5 * |U|) of them must appear in the output as
   ``uid=N`` or as the display name rendered beside that uid in the input
   (``·<name> (uid=N)``, as a name hit).
5. Input parts are user prompts, tool returns and the agent's own text;
   system prompts and tool-call arguments are left out; structured content
   is read as JSON.

Reasons are content-free (they go to the model and to logs). All id and
name matching goes through ``PurgeTarget``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic_ai.messages import ModelMessage

from smarter_dev.shared.privacy_purge import PurgeTarget

FOLD_MIN_CHARS = 40
FOLD_REFUSALS = (
    "i can't",
    "i cannot",
    "i'm sorry",
    "i am sorry",
    "i'm unable",
    "i am unable",
    "as an ai",
)
FOLD_BYSTANDER_MIN = 200
FOLD_LENGTH_RATIO = 0.05
FOLD_LENGTH_CAP = 400
FOLD_RETENTION_SHARE = 0.5

_UID_VALUE = re.compile(r"uid=([0-9]{1,22})")
_NAME_BESIDE_UID = re.compile(r"·([^\n·]*?) \(uid=([0-9]{1,22})\)")


@dataclass(frozen=True)
class FoldField:
    """One output field and the input parts it replaces; ``output`` is None
    when the model left the field out."""

    name: str
    inputs: tuple[str, ...]
    output: str | None


def fold_input_texts(messages: list[ModelMessage]) -> list[str]:
    """The input parts the rule reads (see 5. above)."""
    texts = []
    for message in messages:
        for part in message.parts:
            kind = getattr(part, "part_kind", "")
            if kind not in ("user-prompt", "tool-return", "text"):
                continue
            content = getattr(part, "content", None)
            if isinstance(content, str):
                texts.append(content)
            elif content is not None:
                texts.append(json.dumps(content, ensure_ascii=False, default=str))
    return texts


def fold_plausibility_problem(
    input_texts: Iterable[str], target: PurgeTarget, output: str
) -> str | None:
    """Why ``output`` cannot replace its input store, or None."""
    input_texts = list(input_texts)
    text = output.strip()
    if not any(part.strip() for part in input_texts):
        return "the input was empty but the output is not" if text else None
    if target.id_hits(output):
        return "it still contains the user id"
    lines = [line for part in input_texts for line in part.splitlines()]
    bystander = "\n".join(line for line in lines if not target.mentions(line))
    folded = text.casefold()
    if any(phrase in folded for phrase in FOLD_REFUSALS):
        return "it reads as a refusal"
    if bystander:
        if len(text) < FOLD_MIN_CHARS or not any(c.isalpha() for c in text):
            return "it is too short to carry any memory"
        if len(bystander) >= FOLD_BYSTANDER_MIN:
            floor = max(
                FOLD_MIN_CHARS,
                min(math.ceil(FOLD_LENGTH_RATIO * len(bystander)), FOLD_LENGTH_CAP),
            )
            if len(text) < floor:
                return f"it is far shorter than what it replaces (needs {floor} chars)"
    joined = "\n".join(input_texts)
    uids = {uid for uid in _UID_VALUE.findall(joined) if uid != target.user_id}
    if uids:
        displays: dict[str, set[str]] = {}
        for name, uid in _NAME_BESIDE_UID.findall(joined):
            if name.strip():
                displays.setdefault(uid, set()).add(name.strip())
        kept = sum(
            1
            for uid in uids
            if f"uid={uid}" in text
            or any(
                PurgeTarget.build("", [name]).name_hits(text)
                for name in displays.get(uid, ())
            )
        )
        required = math.ceil(FOLD_RETENTION_SHARE * len(uids))
        if kept < required:
            return f"it keeps {kept} of the {len(uids)} other members (needs {required})"
    return None


def plausibility_rejection(
    fields: Iterable[FoldField], target: PurgeTarget
) -> str | None:
    """The first field that breaks the rule, as a content-free reason."""
    for field in fields:
        has_input = any(part.strip() for part in field.inputs)
        if field.output is None:
            if has_input:
                return f"the {field.name} field is missing"
            continue
        problem = fold_plausibility_problem(field.inputs, target, field.output)
        if problem is not None:
            return f"{field.name}: {problem}"
    return None
