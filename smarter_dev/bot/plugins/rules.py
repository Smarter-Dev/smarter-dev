"""`/rule` — cite the server's own rules against a recent message.

Anyone may run ``/rule <what happened> [user]``. The command loads the guild's
admin-authored rules, hands the tail of the channel plus the invoker's
description to the rule-matcher agent, validates whatever the model says against
reality, and — only then — posts a public reply to the offending message.

Two rails hold this together and are the reason most of this module is pure
functions:

**The validation rail.** The model picks a rule index and a message id; both are
checked against the guild's real rules and the real candidate messages before
anything is posted. A hallucinated id, an index that does not exist, or a target
that is the bot's own message is an ephemeral note to the invoker and nothing
public. "No rule applies" is a normal outcome, not an error.

**The rendering rail.** A rule's title and body are reproduced verbatim from
config (mass mentions defanged); the only model-written prose in the public post
is the explanation. When the post will not fit Discord's 2000-character cap it is
the *explanation* that gets truncated, never a rule — a half-quoted rule posted
under the bot's name is worse than no explanation at all. If not even one rule
fits, or no rule was matched, :func:`render_rule_citation` returns ``None`` and
nothing public is posted at all: a public reply that pings a member while quoting
no rule and giving no explanation is worse still.

Every path out of the command answers the invoker's deferred ephemeral, including
the Discord failures — a deleted target message, a channel the bot cannot read —
because there is no global error handler behind this command to do it instead.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection
from collections.abc import Sequence
from datetime import datetime

import hikari
import lightbulb

from smarter_dev.bot.agents.rule_matcher import SYSTEM_PROMPT
from smarter_dev.bot.agents.rule_matcher import RejectedCitation
from smarter_dev.bot.agents.rule_matcher import RuleMatchMessage
from smarter_dev.bot.agents.rule_matcher import candidate_target_ids
from smarter_dev.bot.agents.rule_matcher import match_rule_violation
from smarter_dev.bot.agents.rule_matcher import validate_rule_citation
from smarter_dev.bot.plugins.history import neutralize_mass_mentions
from smarter_dev.bot.services.rate_limiter import rate_limiter
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import GuildRulesConfigOperations
from smarter_dev.web.guild_rules import GuildRule
from smarter_dev.web.guild_rules import find_rule_by_index
from smarter_dev.web.guild_rules import parse_guild_rules

logger = logging.getLogger(__name__)

guild_rules_ops = GuildRulesConfigOperations()

#: The key this command uses in ``RateLimiter.COMMAND_LIMITS``.
RULE_COMMAND_TYPE = "rule"

#: How much channel history the matcher gets. Ten messages is enough for the
#: surrounding conversation a violation is usually only legible in, and small
#: enough that the prompt stays cheap.
RULE_CONTEXT_MESSAGE_COUNT = 10

DISCORD_MESSAGE_CHAR_LIMIT = 2000

#: Below this many characters an explanation is an unreadable stub, so it is
#: dropped whole rather than rendered as a couple of words and an ellipsis.
MINIMUM_EXPLANATION_CHARS = 40

#: Blank line between the rendered sections of the public post.
SECTION_SEPARATOR = "\n\n"

#: Characters per token, the usual rough English ratio. Only used to charge the
#: shared token budget for a call whose real usage the agent does not report.
CHARS_PER_TOKEN = 4

#: Headroom charged for the model's own output on top of the prompt estimate.
RULE_MATCH_OUTPUT_TOKEN_ALLOWANCE = 300

#: How much of a guild's rules document may be sent to the model, in characters.
#: Roughly 5,000 tokens — enough for some 40 generously written rules, which is
#: already far more than a server anyone reads the rules of has. Nothing else
#: bounds the admin textarea, and a runaway document would otherwise cost tens
#: of thousands of tokens per call out of the budget shared with every other AI
#: command. Rules past the cap are dropped whole and logged.
MAX_RULES_PROMPT_CHARS = 20_000

NO_RULES_CONFIGURED_MESSAGE = (
    "This server has no rules configured, so there is nothing for me to cite. "
    "An admin can write them on the bot admin page."
)
NO_RECENT_MESSAGES_MESSAGE = (
    "There are no recent messages in this channel for me to look at."
)
LLM_AT_CAPACITY_MESSAGE = (
    "The AI system has reached its usage limits for this time period. "
    "Please try again in a few minutes."
)
MISSING_POST_PERMISSION_MESSAGE = (
    "I don't have permission to post in this channel, so I couldn't share the "
    "citation."
)
MISSING_HISTORY_PERMISSION_MESSAGE = (
    "I can't read this channel's recent messages, so there's nothing for me to "
    "check. I need the Read Message History permission here."
)
CHANNEL_HISTORY_UNAVAILABLE_MESSAGE = (
    "I couldn't read this channel's recent messages, so I haven't posted "
    "anything."
)
TARGET_MESSAGE_GONE_MESSAGE = (
    "The message I was going to reply to is no longer there, so I haven't "
    "posted anything."
)
RULE_TOO_LONG_TO_QUOTE_MESSAGE = (
    "The rule that applies here is too long to quote in the channel, so I "
    "haven't posted anything. An admin can shorten it on the bot admin page."
)
RULES_TOO_LONG_MESSAGE = (
    "This server's rules are too long for me to work with. An admin can shorten "
    "them on the bot admin page."
)
CITATION_POSTED_MESSAGE = "Posted the rule citation."
MODEL_FAILURE_MESSAGE = (
    "The rule matcher failed to answer. Please try again in a moment."
)
DISCORD_UNAVAILABLE_MESSAGE = (
    "Discord didn't respond properly, so I'm not sure whether anything was "
    "posted. Please check the channel before trying again."
)

plugin = lightbulb.Plugin("rules")


# --------------------------------------------------------------------------- #
# Pure rendering
# --------------------------------------------------------------------------- #


def format_rule_block(rule: GuildRule) -> str:
    """Render one rule for the public post: numbered bold title, verbatim body.

    Pure function. The title and body are reproduced as the admin wrote them,
    save for ``@everyone``/``@here`` being zero-width-broken: the additions are
    the rule's number, the bold markers around the title, and nothing else, so a
    reader can tell the quoted rule apart from the bot's prose. Rule text is
    defanged here rather than only at the call site because
    :func:`post_rule_citation`'s ``allowed_mentions`` kwargs are a single guard,
    and a rule about not pinging the server must not ping the server.
    """
    heading = f"**{rule.index}. {neutralize_mass_mentions(rule.title)}**"
    body = neutralize_mass_mentions(rule.body)
    return f"{heading}\n{body}" if body else heading


def omitted_rules_note(omitted_count: int) -> str:
    """The note standing in for rules that would not fit in one message."""
    if omitted_count == 1:
        return "…and 1 more rule was omitted to fit Discord's message limit."
    return (
        f"…and {omitted_count} more rules were omitted to fit Discord's "
        "message limit."
    )


def fit_rule_blocks(
    rule_blocks: Sequence[str], char_limit: int
) -> tuple[tuple[str, ...], int]:
    """Split rendered rules into the ones that fit whole and a dropped count.

    Pure function. A rule is either rendered complete or not at all, and the
    budget already accounts for the :func:`omitted_rules_note` that a partial
    render would need, so the joined result can never exceed ``char_limit``.
    """
    kept: list[str] = []
    used_chars = 0
    for position, block in enumerate(rule_blocks):
        remaining_after_block = len(rule_blocks) - position - 1
        note_cost = (
            len(SECTION_SEPARATOR + omitted_rules_note(remaining_after_block))
            if remaining_after_block
            else 0
        )
        block_cost = len(block) + (len(SECTION_SEPARATOR) if kept else 0)
        if used_chars + block_cost + note_cost > char_limit:
            break
        used_chars += block_cost
        kept.append(block)
    return tuple(kept), len(rule_blocks) - len(kept)


def truncate_with_ellipsis(text: str, char_limit: int) -> str:
    """Return ``text`` cut to ``char_limit`` characters, ellipsis included.

    Pure function. A budget of zero or less leaves room for nothing at all, not
    even the ellipsis, so the result is empty — the return is never longer than
    ``char_limit``.
    """
    if char_limit <= 0:
        return ""
    if len(text) <= char_limit:
        return text
    return text[: char_limit - 1].rstrip() + "…"


def render_rule_citation(
    matched_rules: Sequence[GuildRule],
    explanation: str,
    char_limit: int = DISCORD_MESSAGE_CHAR_LIMIT,
) -> str | None:
    """Render the public post: the cited rules verbatim, then the explanation.

    Pure function. The rules own the character budget — whatever is left over
    goes to the explanation, which is truncated or dropped entirely rather than
    ever costing a rule its text. When only some rules fit, the remainder is
    summarized by :func:`omitted_rules_note` and the explanation is dropped,
    because at that point there is no budget for it.

    Returns:
        The message to post, or ``None`` when there is nothing worth posting:
        no rules were matched at all, or not one of them fits in ``char_limit``.
        A public reply that pings a member while quoting no rule and giving no
        explanation is worse than staying silent, so the caller answers the
        invoker privately instead.
    """
    if not matched_rules:
        return None

    kept_blocks, omitted_count = fit_rule_blocks(
        [format_rule_block(rule) for rule in matched_rules], char_limit
    )
    if not kept_blocks:
        return None

    sections = list(kept_blocks)
    if omitted_count:
        sections.append(omitted_rules_note(omitted_count))
    rules_text = SECTION_SEPARATOR.join(sections)
    if omitted_count:
        return rules_text

    safe_explanation = neutralize_mass_mentions(explanation.strip())
    if not safe_explanation:
        return rules_text
    explanation_budget = char_limit - len(rules_text) - len(SECTION_SEPARATOR)
    if explanation_budget < MINIMUM_EXPLANATION_CHARS:
        return rules_text
    fitted = truncate_with_ellipsis(safe_explanation, explanation_budget)
    return f"{rules_text}{SECTION_SEPARATOR}{fitted}"


# --------------------------------------------------------------------------- #
# Pure selection and messaging
# --------------------------------------------------------------------------- #


def to_rule_match_message(message) -> RuleMatchMessage:
    """Project a Discord message onto what the matcher reasons about."""
    return RuleMatchMessage(
        message_id=str(message.id),
        author_id=str(message.author.id),
        author_display=message.author.username,
        content=message.content or "",
    )


def select_candidate_message_ids(
    messages: Sequence[RuleMatchMessage],
    bot_user_id: str | None,
    target_author_id: str | None,
) -> tuple[str, ...]:
    """The message ids that may legitimately be cited as the target.

    Pure function naming the bot as the agent's ``excluded_author_id``: its
    messages stay in the prompt as context, but the bot is never told off for
    breaking the server's rules. The command hands the same exclusion to
    :func:`match_rule_violation`, so the ids offered to the model and the ids
    accepted from it are one set.
    """
    return candidate_target_ids(messages, target_author_id, bot_user_id)


def bot_message_ids_of(
    messages: Sequence[RuleMatchMessage], bot_user_id: str | None
) -> frozenset[str]:
    """The ids of the messages the bot itself sent, empty if its id is unknown."""
    if bot_user_id is None:
        return frozenset()
    return frozenset(
        message.message_id
        for message in messages
        if message.author_id == bot_user_id
    )


def bot_target_rejection(
    target_message_id: str, bot_message_ids: Collection[str]
) -> str | None:
    """Why a target cannot be used, when the model picked the bot's own message.

    Pure function returning None when the target is fine. The bot's messages are
    given to the model as context, so it can legitimately point at one; that is
    a refusal to explain rather than the generic "id I never offered you" error.
    """
    if target_message_id.strip() in set(bot_message_ids):
        return (
            "I won't cite a rule against my own message. Try naming the member "
            "you mean with the `user` option."
        )
    return None


def rate_limit_message(reset_time: datetime | None, now: datetime) -> str:
    """Tell the invoker they are rate limited and, if known, for how long."""
    limit = rate_limiter.COMMAND_LIMITS[RULE_COMMAND_TYPE]["limit"]
    window_minutes = int(
        rate_limiter.COMMAND_LIMITS[RULE_COMMAND_TYPE]["window"].total_seconds() // 60
    )
    preamble = (
        f"🕒 You've reached the rate limit of {limit} `/rule` requests per "
        f"{window_minutes} minutes."
    )
    if reset_time is None:
        return f"{preamble} Please try again later."
    minutes_left = max(1, math.ceil((reset_time - now).total_seconds() / 60))
    plural = "s" if minutes_left != 1 else ""
    return f"{preamble} Please try again in {minutes_left} minute{plural}."


def no_candidates_message(target_display: str | None, message_count: int) -> str:
    """Explain that there is nothing here the command is allowed to cite."""
    if target_display is None:
        return (
            f"There are no messages in the last {message_count} here that I "
            "could cite a rule against."
        )
    return (
        f"{neutralize_mass_mentions(target_display)} has no messages in the "
        f"last {message_count} messages here, so there is nothing to cite."
    )


def no_match_message(explanation: str) -> str:
    """The ephemeral reply for the normal "nothing in the rules covers this"."""
    safe_explanation = neutralize_mass_mentions(explanation.strip())
    opening = "No rule seemed to apply to that, so I haven't posted anything."
    return f"{opening} {safe_explanation}" if safe_explanation else opening


def rejected_citation_message(reason: str) -> str:
    """The ephemeral reply when the model's answer failed validation."""
    return f"I couldn't cite a rule for that: {neutralize_mass_mentions(reason)}."


def estimate_rule_match_tokens(
    rules: Sequence[GuildRule],
    messages: Sequence[RuleMatchMessage],
    moderator_description: str,
) -> int:
    """Estimate the tokens one ``/rule`` model call will burn.

    Pure function summing every part of the prompt the agent actually builds —
    system prompt, rules, conversation and the moderator's own description. The
    agent returns only its structured answer, not a usage record, so the shared
    hourly token budget is charged from that size plus a fixed allowance for the
    model's reply. An estimate that is roughly right is the difference between
    this command counting against the budget and being invisible to it.
    """
    prompt_chars = len(SYSTEM_PROMPT)
    prompt_chars += sum(len(rule.title) + len(rule.body) for rule in rules)
    prompt_chars += sum(
        len(message.author_display) + len(message.content) + len(message.message_id)
        for message in messages
    )
    prompt_chars += len(moderator_description)
    return prompt_chars // CHARS_PER_TOKEN + RULE_MATCH_OUTPUT_TOKEN_ALLOWANCE


def rules_within_prompt_budget(
    rules: Sequence[GuildRule], char_budget: int
) -> tuple[tuple[GuildRule, ...], int]:
    """Split a guild's rules into the leading run that fits, and a dropped count.

    Pure function. Nothing bounds how much an admin can type into the rules
    textarea, and every ``/rule`` call pays for the whole document in prompt
    tokens out of a budget shared with every other AI command, so the document
    is capped before it is sent. Rules are kept whole and in document order, so
    the indices the model may cite stay contiguous from 1 and mean the same
    thing they do in the admin's document.
    """
    kept: list[GuildRule] = []
    used_chars = 0
    for rule in rules:
        rule_chars = len(rule.title) + len(rule.body)
        if used_chars + rule_chars > char_budget:
            break
        used_chars += rule_chars
        kept.append(rule)
    return tuple(kept), len(rules) - len(kept)


# --------------------------------------------------------------------------- #
# Discord I/O
# --------------------------------------------------------------------------- #


async def collect_recent_messages(rest, channel_id: int, limit: int) -> list:
    """Fetch the tail of a channel, returned oldest first.

    Discord serves channel history newest-first; the matcher's prompt reads
    oldest-first, which is also how a human reads the conversation.

    Raises:
        hikari.ForbiddenError: The bot cannot read this channel's history.
        hikari.NotFoundError: The channel is gone.
    """
    newest_first = [
        message async for message in rest.fetch_messages(channel_id).limit(limit)
    ]
    return list(reversed(newest_first))


async def load_guild_rules(guild_id: str) -> list[GuildRule]:
    """Read and parse the guild's rules document, empty when none is configured."""
    async with get_db_session_context() as session:
        config = await guild_rules_ops.get_config(session, guild_id)
    return parse_guild_rules(config.rules_markdown if config is not None else None)


async def respond_ephemeral(ctx: lightbulb.Context, message: str) -> None:
    """Reply to the invoker only, with every mention pinned off.

    Ephemeral text echoes usernames and model prose, so mentions are disabled
    explicitly rather than relying on the content having been defanged.
    """
    await ctx.respond(
        message,
        flags=hikari.MessageFlag.EPHEMERAL,
        mentions_everyone=False,
        user_mentions=False,
        role_mentions=False,
    )


async def post_rule_citation(rest, channel_id: int, target_message, content: str) -> None:
    """Post the citation publicly as a reply to the offending message.

    ``mentions_reply`` is the one ping this message is allowed to make: the
    member being cited is notified, while ``@everyone``/``@here``, roles and any
    user id interpolated into the text cannot ping anyone.
    """
    await rest.create_message(
        channel_id,
        content,
        reply=target_message,
        mentions_reply=True,
        mentions_everyone=False,
        user_mentions=False,
        role_mentions=False,
    )


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #


@plugin.command
@lightbulb.option(
    "user",
    "Only consider this member's messages",
    type=hikari.OptionType.USER,
    required=False,
    default=None,
)
@lightbulb.option(
    "rule",
    "What happened, in your own words",
    type=str,
    required=True,
)
@lightbulb.command(
    "rule",
    "Cite the server rule a recent message breaks",
    auto_defer=True,
    ephemeral=True,
)
@lightbulb.implements(lightbulb.SlashCommand)
async def rule(ctx: lightbulb.Context) -> None:
    """Match a described incident to a recent message and cite the rules it breaks."""

    guild = ctx.get_guild()
    if not guild:
        await respond_ephemeral(ctx, "This command only works in a server.")
        return

    invoker_id = str(ctx.author.id)
    if not rate_limiter.check_user_limit(invoker_id, RULE_COMMAND_TYPE):
        await respond_ephemeral(
            ctx,
            rate_limit_message(
                rate_limiter.get_user_reset_time(invoker_id, RULE_COMMAND_TYPE),
                datetime.now(),
            ),
        )
        return

    configured_rules = await load_guild_rules(str(guild.id))
    if not configured_rules:
        await respond_ephemeral(ctx, NO_RULES_CONFIGURED_MESSAGE)
        return

    guild_rules, dropped_rule_count = rules_within_prompt_budget(
        configured_rules, MAX_RULES_PROMPT_CHARS
    )
    if dropped_rule_count:
        logger.warning(
            "Guild %s has %s rule(s) past the %s-character prompt cap; they were "
            "not sent to the matcher",
            guild.id,
            dropped_rule_count,
            MAX_RULES_PROMPT_CHARS,
        )
    if not guild_rules:
        await respond_ephemeral(ctx, RULES_TOO_LONG_MESSAGE)
        return

    try:
        fetched_messages = await collect_recent_messages(
            ctx.bot.rest, ctx.channel_id, RULE_CONTEXT_MESSAGE_COUNT
        )
    except hikari.ForbiddenError:
        await respond_ephemeral(ctx, MISSING_HISTORY_PERMISSION_MESSAGE)
        return
    except hikari.NotFoundError:
        await respond_ephemeral(ctx, CHANNEL_HISTORY_UNAVAILABLE_MESSAGE)
        return
    except hikari.HTTPError:
        # A 5xx during a Discord outage, or a 401 on a revoked token. Answer the
        # invoker so the deferred ephemeral never hangs, then re-raise so the
        # failure stays loud — there is no global error handler behind this
        # command to surface it otherwise.
        await respond_ephemeral(ctx, DISCORD_UNAVAILABLE_MESSAGE)
        raise
    if not fetched_messages:
        await respond_ephemeral(ctx, NO_RECENT_MESSAGES_MESSAGE)
        return

    bot_user = ctx.bot.get_me()
    bot_user_id = str(bot_user.id) if bot_user is not None else None
    match_messages = [to_rule_match_message(message) for message in fetched_messages]

    target_user = ctx.options.user
    target_author_id = str(target_user.id) if target_user is not None else None
    candidate_ids = select_candidate_message_ids(
        match_messages, bot_user_id, target_author_id
    )
    if not candidate_ids:
        await respond_ephemeral(
            ctx,
            no_candidates_message(
                target_user.username if target_user is not None else None,
                RULE_CONTEXT_MESSAGE_COUNT,
            ),
        )
        return

    estimated_tokens = estimate_rule_match_tokens(
        guild_rules, match_messages, ctx.options.rule
    )
    if not rate_limiter.check_token_limit(estimated_tokens):
        await respond_ephemeral(ctx, LLM_AT_CAPACITY_MESSAGE)
        return

    # Charged before the call, not after: the limit exists to cap model calls and
    # public posts, so a request that fails or gets rejected still spent one.
    rate_limiter.record_request(invoker_id, estimated_tokens, RULE_COMMAND_TYPE)

    try:
        citation = await match_rule_violation(
            ctx.options.rule,
            guild_rules,
            match_messages,
            target_author_id=target_author_id,
            excluded_author_id=bot_user_id,
        )
    except Exception:
        # Tell the invoker something went wrong, then re-raise so the failure is
        # still loud. Failing open here would mean inventing a public citation.
        await respond_ephemeral(ctx, MODEL_FAILURE_MESSAGE)
        raise

    bot_rejection = bot_target_rejection(
        citation.target_message_id, bot_message_ids_of(match_messages, bot_user_id)
    )
    if bot_rejection is not None:
        await respond_ephemeral(ctx, bot_rejection)
        return

    validated = validate_rule_citation(
        citation, {rule_.index for rule_ in guild_rules}, candidate_ids
    )
    if isinstance(validated, RejectedCitation):
        logger.warning(
            "Rejected /rule citation in guild %s: %s", guild.id, validated.reason
        )
        await respond_ephemeral(ctx, rejected_citation_message(validated.reason))
        return

    if not validated.matched_rule_indices:
        await respond_ephemeral(ctx, no_match_message(validated.explanation))
        return

    matched_rules = [
        matched
        for matched in (
            find_rule_by_index(guild_rules, index)
            for index in validated.matched_rule_indices
        )
        if matched is not None
    ]
    target_message = next(
        message
        for message in fetched_messages
        if str(message.id) == validated.target_message_id
    )

    citation_content = render_rule_citation(matched_rules, validated.explanation)
    if citation_content is None:
        logger.warning(
            "Rule(s) %s in guild %s are too long to quote in one message",
            list(validated.matched_rule_indices),
            guild.id,
        )
        await respond_ephemeral(ctx, RULE_TOO_LONG_TO_QUOTE_MESSAGE)
        return

    try:
        await post_rule_citation(
            ctx.bot.rest, ctx.channel_id, target_message, citation_content
        )
    except hikari.ForbiddenError:
        await respond_ephemeral(ctx, MISSING_POST_PERMISSION_MESSAGE)
        return
    except (hikari.NotFoundError, hikari.BadRequestError):
        # The offending message was deleted during the model call, so the reply
        # target Discord was given no longer resolves.
        await respond_ephemeral(ctx, TARGET_MESSAGE_GONE_MESSAGE)
        return
    except hikari.HTTPError:
        # A 5xx or a 401: unlike the cases above we cannot tell whether the post
        # landed, so the message says so rather than claiming nothing happened.
        await respond_ephemeral(ctx, DISCORD_UNAVAILABLE_MESSAGE)
        raise

    await respond_ephemeral(ctx, CITATION_POSTED_MESSAGE)

    logger.info(
        "%s (%s) cited rule(s) %s on message %s in guild %s",
        ctx.author.username,
        ctx.author.id,
        list(validated.matched_rule_indices),
        validated.target_message_id,
        guild.id,
    )


def load(bot: lightbulb.BotApp) -> None:
    """Load the rules plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the rules plugin."""
    bot.remove_plugin(plugin)
