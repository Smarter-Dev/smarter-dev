"""Match a moderator's description of an incident to a message and rules.

A moderator runs ``/rule <what happened> [user]``. This agent is handed the
guild's parsed rules, the tail of the channel, and that description, and answers
two questions: WHICH message the moderator means, and WHICH rules it breaks. It
never writes rule text — the command renders the admin's wording verbatim from
config — so the model only ever selects rule *indices* and writes
the sentence explaining why they apply. A paraphrased rule posted publicly under
the bot's name is worse than no citation at all.

Everything the command needs before it posts is pure and testable here:
:func:`candidate_target_ids` decides what may be targeted,
:func:`build_rule_match_prompt` renders the prompt, and
:func:`validate_rule_citation` is the gate an LLM answer must pass — an invented
message id or a rule index that does not exist must never reach a public post.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic import Field
from pydantic_ai import Agent

from smarter_dev.bot.agents.model_router import build_model_for
from smarter_dev.bot.agents.model_router import model_settings_for
from smarter_dev.shared.model_catalog import ReasoningLevel
from smarter_dev.shared.model_catalog import get_model
from smarter_dev.web.guild_rules import GuildRule
from smarter_dev.web.guild_rules import format_rules_for_prompt

logger = logging.getLogger(__name__)

RULE_MATCHER_MODEL_KEY = "gemini-3-5-flash-lite"

SYSTEM_PROMPT = """\
You help a Discord moderator cite the server's own rules. A moderator has \
described an incident in their own words; you decide WHICH message they are \
describing and WHICH of the server's rules that message breaks.

You are given:
- RULES: the server's rules, each with a number. That number is the rule's \
index and is the only way you may refer to a rule.
- CONVERSATION: recent channel messages, oldest first, each prefixed with its \
message id in square brackets. All of them are context — a violation is often \
only legible in the surrounding conversation.
- CANDIDATE MESSAGES: the subset of the conversation you are allowed to pick \
as the target. When the moderator named a user, only that user's messages are \
candidates.
- MODERATOR DESCRIPTION: what the moderator says happened.

Answer with:
- ``matched_rule_indices``: the index numbers of every rule the target message \
breaks, most relevant first. Cite a rule only when the message plainly breaks \
it; two or three is already a lot. Return an EMPTY list when no rule really \
applies — that is the correct, expected answer for a message that is merely \
rude, annoying, or off-topic without breaking a written rule. Never stretch a \
rule to fit, and never invent an index that is not in RULES.
- ``target_message_id``: the id of the single CANDIDATE message the moderator \
is describing, copied exactly as given in square brackets. Never return an id \
that is not in CANDIDATE MESSAGES, and never invent one. If the description \
fits no candidate, still return the closest candidate id and an empty \
``matched_rule_indices``.
- ``explanation``: one or two plain sentences saying what the message did that \
breaks the cited rules. This is the ONLY prose you write.

The rules' own wording is displayed to the channel verbatim from the server's \
configuration, alongside your explanation. So do NOT quote, restate, or \
paraphrase rule text in your explanation — a reworded rule is a subtly wrong \
rule posted under the bot's name. Refer to the behaviour in the message \
instead ("advertised a paid product without asking first"), not to what the \
rule says. When ``matched_rule_indices`` is empty, use the explanation to say \
briefly why nothing in the rules covers this."""


@dataclass(frozen=True)
class RuleMatchMessage:
    """One Discord message the matcher reasons about."""

    message_id: str
    author_id: str
    author_display: str
    content: str


class RuleCitation(BaseModel):
    """The model's raw answer: which rules, on which message, and why.

    An empty ``matched_rule_indices`` IS the "no rule applies" signal; there is
    deliberately no separate boolean that could contradict the list.
    """

    matched_rule_indices: list[int] = Field(
        description=(
            "Index numbers of the rules the target message breaks, most "
            "relevant first. Empty when no rule applies."
        ),
    )
    target_message_id: str = Field(
        description="Id of the CANDIDATE message the moderator is describing.",
    )
    explanation: str = Field(
        description=(
            "One or two sentences on what the message did. Never quotes or "
            "paraphrases rule text."
        ),
    )


@dataclass(frozen=True)
class ValidatedCitation:
    """A citation that passed :func:`validate_rule_citation` and is safe to post.

    Attributes:
        matched_rule_indices: Deduped rule indices, all known to the guild, in
            the order the model gave them. Empty means no rule applies.
        target_message_id: A message id that really was offered as a candidate.
        explanation: The model's prose, whitespace-stripped.
    """

    matched_rule_indices: tuple[int, ...]
    target_message_id: str
    explanation: str


@dataclass(frozen=True)
class RejectedCitation:
    """A citation that failed validation, with a reason fit for a mod-only reply."""

    reason: str


_rule_matcher_agent: Agent[None, RuleCitation] | None = None


def get_rule_matcher_agent() -> Agent[None, RuleCitation]:
    """Return the singleton rule-matcher agent, building it on first use."""
    global _rule_matcher_agent
    if _rule_matcher_agent is None:
        catalog_model = get_model(RULE_MATCHER_MODEL_KEY)
        if catalog_model is None:
            raise ValueError(
                f"Rule matcher model {RULE_MATCHER_MODEL_KEY!r} is not in the catalog"
            )
        _rule_matcher_agent = Agent(
            build_model_for(catalog_model),
            output_type=RuleCitation,
            system_prompt=SYSTEM_PROMPT,
            model_settings=model_settings_for(catalog_model, ReasoningLevel.LOW),
        )
    return _rule_matcher_agent


def candidate_target_ids(
    messages: Sequence[RuleMatchMessage],
    target_author_id: str | None = None,
    excluded_author_id: str | None = None,
) -> tuple[str, ...]:
    """Return the ids of the messages that may be cited as the target.

    Pure function. With no ``target_author_id`` every fetched message is a
    candidate; with one, only that user's messages are — the rest stay in the
    prompt as context. ``excluded_author_id`` then removes an author who may
    never be a target however the moderator narrowed things: in practice the bot
    itself, whose messages are context but are never told off for breaking the
    server's rules. The result is in conversation order and is exactly the set
    to hand to :func:`validate_rule_citation`, so the prompt can never offer an
    id the caller has already decided to refuse.
    """
    return tuple(
        message.message_id
        for message in messages
        if (target_author_id is None or message.author_id == target_author_id)
        and message.author_id != excluded_author_id
    )


def build_rule_match_prompt(
    moderator_description: str,
    rules: Sequence[GuildRule],
    messages: Sequence[RuleMatchMessage],
    target_author_id: str | None = None,
    excluded_author_id: str | None = None,
) -> str:
    """Render the user prompt for one ``/rule`` invocation.

    Pure function. Every message appears under CONVERSATION regardless of
    ``target_author_id`` or ``excluded_author_id``, because a violation is often
    only legible in the surrounding conversation; CANDIDATE MESSAGES then
    narrows what may be targeted, using exactly the same
    :func:`candidate_target_ids` the caller validates the answer against.

    Raises:
        ValueError: If ``rules`` or ``messages`` is empty — there is nothing to
            cite or nothing to cite it against, and the caller should say so
            rather than spend a model call.
    """
    if not rules:
        raise ValueError("Cannot build a rule-match prompt with no rules")
    if not messages:
        raise ValueError("Cannot build a rule-match prompt with no messages")

    candidate_ids = set(
        candidate_target_ids(messages, target_author_id, excluded_author_id)
    )
    candidates = [
        message for message in messages if message.message_id in candidate_ids
    ]
    candidate_block = (
        "\n".join(_render_message(message) for message in candidates)
        if candidates
        else "(none — no message in this conversation may be cited as the target)"
    )
    return "\n\n".join(
        [
            "RULES:\n" + format_rules_for_prompt(list(rules)),
            "CONVERSATION (oldest first, all of it context):\n"
            + "\n".join(_render_message(message) for message in messages),
            "CANDIDATE MESSAGES (pick the target from these ids only):\n"
            + candidate_block,
            "MODERATOR DESCRIPTION:\n" + moderator_description.strip(),
        ]
    )


def validate_rule_citation(
    citation: RuleCitation,
    valid_rule_indices: Collection[int],
    candidate_message_ids: Collection[str],
) -> ValidatedCitation | RejectedCitation:
    """Check an LLM citation against reality before anything is posted publicly.

    Pure function; mutates nothing. A citation is accepted only when its target
    is a message that really was offered as a candidate and every rule index it
    names really exists (which rejects ``0`` and negatives, since guild rules are
    numbered from 1). Repeated indices are deduped, keeping the model's ordering.
    An empty ``matched_rule_indices`` is a valid answer — "no rule applies" — and
    still requires a real target message, since the reply names the message.

    Returns:
        A :class:`ValidatedCitation` safe to render, or a
        :class:`RejectedCitation` whose ``reason`` explains what was wrong.
    """
    target_message_id = citation.target_message_id.strip()
    if target_message_id not in set(candidate_message_ids):
        return RejectedCitation(
            reason=(
                f"the model targeted message {target_message_id!r}, which is not "
                "one of the messages it was offered"
            )
        )

    unknown_indices = [
        index
        for index in citation.matched_rule_indices
        if index not in set(valid_rule_indices)
    ]
    if unknown_indices:
        listed = ", ".join(str(index) for index in unknown_indices)
        return RejectedCitation(
            reason=f"the model cited rule number(s) {listed}, which do not exist"
        )

    explanation = citation.explanation.strip()
    if citation.matched_rule_indices and not explanation:
        return RejectedCitation(
            reason="the model cited rules but gave no explanation"
        )

    return ValidatedCitation(
        matched_rule_indices=_deduped(citation.matched_rule_indices),
        target_message_id=target_message_id,
        explanation=explanation,
    )


async def match_rule_violation(
    moderator_description: str,
    rules: Sequence[GuildRule],
    messages: Sequence[RuleMatchMessage],
    target_author_id: str | None = None,
    excluded_author_id: str | None = None,
) -> RuleCitation:
    """Ask the model which message the moderator means and which rules it breaks.

    Returns the model's RAW structured output, unvalidated on purpose: the
    caller pairs it with :func:`validate_rule_citation` (against
    :func:`candidate_target_ids` and the guild's rule indices) before rendering
    anything publicly. Pass the same ``target_author_id`` and
    ``excluded_author_id`` to both, so the model is never offered an id its
    answer would be rejected for using. Model failures propagate — a moderator
    gets an error, not an invented citation.

    Raises:
        ValueError: If ``rules`` or ``messages`` is empty.
    """
    prompt = build_rule_match_prompt(
        moderator_description, rules, messages, target_author_id, excluded_author_id
    )
    result = await get_rule_matcher_agent().run(prompt)
    logger.info(
        "match_rule_violation: rules=%s target_message_id=%r description=%r",
        result.output.matched_rule_indices,
        result.output.target_message_id,
        moderator_description,
    )
    return result.output


def _render_message(message: RuleMatchMessage) -> str:
    """Render one message as ``[id] author: content`` for the prompt."""
    return f"[{message.message_id}] {message.author_display}: {message.content}"


def _deduped(indices: Sequence[int]) -> tuple[int, ...]:
    """Return ``indices`` without repeats, keeping first-appearance order."""
    seen: set[int] = set()
    unique: list[int] = []
    for index in indices:
        if index not in seen:
            seen.add(index)
            unique.append(index)
    return tuple(unique)
