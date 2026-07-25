"""`/history` — the moderator mod-history lookup, as a bot-core slash command.

Replaces the legacy `!history` mod-lookup command. Renders a profile card for the
target (working for departed users too) followed by their `ModerationAction`
history, newest-first, each row carrying a "Jump To Action" link back to the
message that triggered it. See
docs/v2/feature-parity/automated-and-command-moderation.md §4.2 "Handler D" — the
2026 decision is a bot-core slash command instead of a mod-channel handler, and
the sibling `!lookup`/`!whois` subcommands are dropped.

Divergence from legacy: legacy packed up to 50 rows into a ~6000-char embed; this
renders a plain ephemeral message capped at Discord's 2000 chars, stopping before
the cap and appending an "…and N older actions" tail for the remainder.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

import hikari
import lightbulb

from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import ModerationActionOperations

logger = logging.getLogger(__name__)

mod_action_ops = ModerationActionOperations()

# Discord hard-caps message content at 2000 characters; the legacy command read
# 50 rows deep, which we keep even though the cap may render fewer.
DISCORD_MESSAGE_CHAR_LIMIT = 2000
MODERATION_HISTORY_DEPTH = 50

MASS_MENTION_TOKENS = ("@everyone", "@here")
ZERO_WIDTH_SPACE = "​"
_MASS_MENTION_PATTERN = re.compile(
    "|".join(re.escape(token) for token in MASS_MENTION_TOKENS), re.IGNORECASE
)

_SECONDS_PER_UNIT: tuple[tuple[str, int], ...] = (
    ("year", 365 * 86_400),
    ("month", 30 * 86_400),
    ("week", 7 * 86_400),
    ("day", 86_400),
    ("hour", 3_600),
    ("minute", 60),
)

plugin = lightbulb.Plugin("history")


@dataclass(frozen=True)
class TargetProfile:
    """The profile-card facts about the looked-up user.

    ``is_guild_member`` is False for a user who has left (or never joined) the
    guild; such a profile has no ``joined_at`` and no known rules acceptance.
    ``has_accepted_rules`` is None when Discord did not tell us (membership
    screening disabled, or the gateway omitted the field).
    """

    user_id: str
    username: str
    joined_at: datetime | None
    is_guild_member: bool
    has_accepted_rules: bool | None


@dataclass(frozen=True)
class HistoryRow:
    """One renderable moderation-history entry, decoupled from the ORM row."""

    action_type: str
    occurred_at: datetime
    reason: str | None
    source: str
    channel_id: str | None
    trigger_message_id: str | None


def humanize_time_since(moment: datetime, now: datetime) -> str:
    """Render the gap between ``moment`` and ``now`` as e.g. "3 months ago"."""
    elapsed_seconds = int((now - moment).total_seconds())
    for unit_name, unit_seconds in _SECONDS_PER_UNIT:
        if elapsed_seconds >= unit_seconds:
            unit_count = elapsed_seconds // unit_seconds
            plural = "s" if unit_count != 1 else ""
            return f"{unit_count} {unit_name}{plural} ago"
    return "just now"


def build_jump_link(
    guild_id: str, channel_id: str | None, trigger_message_id: str | None
) -> str | None:
    """The message URL for an action, or None when either id is missing.

    Rows recorded without a triggering message (audit-log ingestion, manual
    commands outside a message context) render linkless — legacy behaved the
    same when its pickled link was absent.
    """
    if not channel_id or not trigger_message_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{trigger_message_id}"


def neutralize_mass_mentions(text: str) -> str:
    """Zero-width-break ``@everyone``/``@here`` so echoed text cannot ping.

    Mirrors ``spam_engine.sanitize_content``'s mass-mention defusing. The
    response also pins ``allowed_mentions`` off, but usernames are attacker-
    chosen and reasons are free text, so the rendered characters are defanged
    too rather than relying on a single guard.
    """
    return _MASS_MENTION_PATTERN.sub(
        lambda match: f"@{ZERO_WIDTH_SPACE}{match.group(0)[1:]}", text
    )


def format_profile_card(profile: TargetProfile, now: datetime) -> str:
    """Render the header card: who the user is and their standing in the guild."""
    lines = [f"**{neutralize_mass_mentions(profile.username)}** (`{profile.user_id}`)"]
    if not profile.is_guild_member:
        lines.append("NO LONGER A MEMBER")
    elif profile.joined_at is not None:
        joined_on = profile.joined_at.strftime("%Y-%m-%d")
        lines.append(f"Joined: {humanize_time_since(profile.joined_at, now)} ({joined_on})")
    if profile.is_guild_member and profile.has_accepted_rules is not None:
        lines.append(
            f"Accepted rules: {'Yes' if profile.has_accepted_rules else 'No'}"
        )
    return "\n".join(lines)


def format_history_row(row: HistoryRow, guild_id: str) -> str:
    """Render one history entry: date, action, source, reason, optional link."""
    occurred_on = row.occurred_at.strftime("%Y-%m-%d")
    reason = row.reason.strip() if row.reason and row.reason.strip() else None
    rendered = (
        f"`{occurred_on}` **{row.action_type}** ({row.source}) — "
        f"{neutralize_mass_mentions(reason) if reason else 'no reason recorded'}"
    )
    jump_link = build_jump_link(guild_id, row.channel_id, row.trigger_message_id)
    if jump_link:
        rendered += f" [Jump To Action]({jump_link})"
    return rendered


def render_history_message(
    profile: TargetProfile,
    rows: list[HistoryRow],
    guild_id: str,
    now: datetime,
    char_limit: int = DISCORD_MESSAGE_CHAR_LIMIT,
) -> str:
    """Render the full response, dropping rows that would breach ``char_limit``.

    Rows arrive newest-first and are rendered in that order; whatever does not
    fit is summarized by an "…and N older actions" tail, itself accounted for in
    the budget so the result never exceeds ``char_limit``.
    """
    card = format_profile_card(profile, now)
    if not rows:
        return f"{card}\n\nNo moderation history."

    rendered_rows: list[str] = []
    used_chars = len(card) + len("\n\n")
    for index, row in enumerate(rows):
        rendered_row = format_history_row(row, guild_id)
        remaining_after_row = len(rows) - index - 1
        tail_cost = (
            len("\n" + _older_actions_tail(remaining_after_row))
            if remaining_after_row
            else 0
        )
        row_cost = len(rendered_row) + (len("\n") if rendered_rows else 0)
        if used_chars + row_cost + tail_cost > char_limit:
            break
        used_chars += row_cost
        rendered_rows.append(rendered_row)

    omitted_count = len(rows) - len(rendered_rows)
    body = "\n".join(rendered_rows)
    if omitted_count:
        body = f"{body}\n{_older_actions_tail(omitted_count)}" if body else (
            _older_actions_tail(omitted_count)
        )
    return f"{card}\n\n{body}"


def _older_actions_tail(omitted_count: int) -> str:
    """The "…and N older actions" note standing in for un-rendered rows."""
    plural = "s" if omitted_count != 1 else ""
    return f"…and {omitted_count} older action{plural}"


def to_history_row(action) -> HistoryRow:
    """Project a ``ModerationAction`` row onto the renderable ``HistoryRow``."""
    return HistoryRow(
        action_type=action.action_type,
        occurred_at=action.created_at,
        reason=action.reason,
        source=action.source,
        channel_id=action.channel_id,
        trigger_message_id=action.trigger_message_id,
    )


async def resolve_target_profile(
    rest, guild_id: int, user_id: int
) -> TargetProfile:
    """Profile the target, falling back to the user endpoint for departed users.

    A 404 from the member fetch is the normal "they left" signal, so the bare
    ``GET /users/{id}`` still gives us a username to render the card with.
    """
    try:
        member = await rest.fetch_member(guild_id, user_id)
    except hikari.NotFoundError:
        user = await rest.fetch_user(user_id)
        return TargetProfile(
            user_id=str(user.id),
            username=user.username,
            joined_at=None,
            is_guild_member=False,
            has_accepted_rules=None,
        )

    is_pending = member.is_pending
    return TargetProfile(
        user_id=str(member.id),
        username=member.username,
        joined_at=member.joined_at,
        is_guild_member=True,
        has_accepted_rules=(not is_pending) if isinstance(is_pending, bool) else None,
    )


async def deny_if_not_moderator(ctx: lightbulb.Context, denial_message: str) -> bool:
    """Gate a slash command on MODERATE_MEMBERS; deny ephemerally otherwise.

    Mirrors ``plugins/admin_gate.deny_if_not_admin`` (which gates the stricter
    ADMINISTRATOR bit) so the permission check never lives inline in a handler
    body. Returns True — having already responded — when the invoker may not
    proceed, so callers can early-return.
    """
    if not isinstance(ctx.member, hikari.InteractionMember):
        await ctx.respond(
            "This command only works in a server.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return True
    permissions = lightbulb.utils.permissions_for(ctx.member)
    if not (permissions & hikari.Permissions.MODERATE_MEMBERS):
        await ctx.respond(denial_message, flags=hikari.MessageFlag.EPHEMERAL)
        return True
    return False


@plugin.command
@lightbulb.option(
    "user",
    "The user whose moderation history to look up",
    type=hikari.OptionType.USER,
    required=True,
)
@lightbulb.command(
    "history",
    "Show a user's profile and moderation history (moderators only)",
    auto_defer=True,
    ephemeral=True,
)
@lightbulb.implements(lightbulb.SlashCommand)
async def history(ctx: lightbulb.Context) -> None:
    """Show the target's profile card plus their moderation history."""

    if await deny_if_not_moderator(
        ctx, "You don't have permission to view moderation history."
    ):
        return

    guild = ctx.get_guild()
    if not guild:
        await ctx.respond(
            "Could not access guild information.",
            flags=hikari.MessageFlag.EPHEMERAL,
        )
        return

    target_user = ctx.options.user
    profile = await resolve_target_profile(ctx.bot.rest, guild.id, target_user.id)

    async with get_db_session_context() as session:
        actions = await mod_action_ops.get_actions_for_user(
            session,
            str(guild.id),
            str(target_user.id),
            limit=MODERATION_HISTORY_DEPTH,
        )

    rows = [to_history_row(action) for action in actions]
    message = render_history_message(
        profile, rows, str(guild.id), datetime.now(UTC)
    )

    # Ephemeral: a member's mod history must not be broadcast into the channel.
    # Mentions are pinned off explicitly: the card echoes the target's username
    # and each row echoes a moderator-written reason, neither of which may ping.
    await ctx.respond(
        message,
        flags=hikari.MessageFlag.EPHEMERAL,
        mentions_everyone=False,
        user_mentions=False,
        role_mentions=False,
    )

    logger.info(
        f"{ctx.author.username} ({ctx.author.id}) viewed moderation history for "
        f"{profile.username} ({profile.user_id}) in guild {guild.id}: "
        f"{len(rows)} action(s)"
    )


def load(bot: lightbulb.BotApp) -> None:
    """Load the history plugin."""
    bot.add_plugin(plugin)


def unload(bot: lightbulb.BotApp) -> None:
    """Unload the history plugin."""
    bot.remove_plugin(plugin)
