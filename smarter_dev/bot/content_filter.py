"""Bot-core message content filters: blocked TLDs, leaked webhooks, invites.

This is the port of the legacy ``codes-auto-mod`` content filters (see
docs/v2/feature-parity/automated-and-command-moderation.md §4.1 "Handler A").
It deliberately lives in bot core rather than in an admin handler: the handler
dispatch path silently drops fires above ``ADMIN_FIRES_PER_MIN``, which is
exactly the moment a raid is happening and the filters matter most.

Layout
------
Everything that decides *what* is in a message — link extraction, TLD matching,
webhook-URL validation, invite detection, sanitization and message formatting —
is a pure module-level function with no Discord and no database dependency, unit
tested directly. :func:`check_content_filters` is a thin shell that reads the
per-guild :class:`ModerationFilterConfig`, applies the exemptions, and performs
the REST side effects.

Error policy (deliberately unlike ``attachment_filter.py``): nothing is wrapped
in a bare ``except Exception``. The expected REST outcomes are caught — a 404
(the message or webhook is already gone, a routine race with another mod or bot)
and a 403 (the bot is missing a channel permission, an operator misconfiguration
worth a warning but not worth aborting enforcement). The one place that catches
the whole ``hikari.HTTPError`` family is the invite-exemption channel lookup: a
403, a 429 or a transport error there means the exemption is unprovable, which
fails closed (not exempt) rather than aborting the filters that already decided
to act. A database failure while recording the action propagates to the caller.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import hikari

from smarter_dev.bot.mod_action_dispatch import dispatch_mod_action
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.web.crud import ModerationActionOperations
from smarter_dev.web.crud import ModerationFilterConfigOperations
from smarter_dev.web.models import ModerationFilterConfig
from smarter_dev.web.webhook_urls import WEBHOOK_PATH_MARKER
from smarter_dev.web.webhook_urls import is_discord_webhook_url
from smarter_dev.web.webhook_urls import normalize_webhook_url
from smarter_dev.web.webhook_urls import parse_discord_webhook_url

if TYPE_CHECKING:
    from hikari.traits import RESTAware

logger = logging.getLogger(__name__)

moderation_filter_config_ops = ModerationFilterConfigOperations()
mod_action_ops = ModerationActionOperations()

# How much of the offending message the mod-log line reproduces (legacy: ~800).
MOD_LOG_CONTENT_LIMIT = 800

# ``ModerationAction`` shape for a filter deletion. ``source="handler"`` marks
# every auto-mod row, matching §3.8; ``action_type`` is capped at 20 chars.
DELETION_ACTION_TYPE = "delete"
DELETION_SOURCE = "handler"

# A link is an optional scheme + a dotted host whose last label is alphabetic +
# an optional port and path. The leading look-behind stops the pattern matching
# in the middle of a word or of an already-matched URL, and requiring an
# alphabetic final label keeps prose like "e.g." and version numbers out.
_LINK_PATTERN = re.compile(
    r"(?<![\w@./-])"
    r"(?:https?://)?"
    r"(?:[\w-]+\.)+[A-Za-z]{2,}"
    r"(?::\d{1,5})?"
    r"(?:/[^\s<>\"'`]*)?",
    re.IGNORECASE,
)

# Punctuation that trails a link in prose ("see https://x.gay/y.") and is not
# part of it.
_TRAILING_LINK_PUNCTUATION = ".,;:!?)]}>\"'"

# Webhook URL validation (the regex, the normalization, and the id/token parse)
# lives in ``smarter_dev.web.webhook_urls`` because the admin-handler scripting
# function deletes leaked webhooks too. Both deleters MUST answer "is this a
# real Discord webhook?" identically — they carried mirrored copies once and
# drifted, so the same leaked URL was killed by one path and refused by the
# other. Do not reintroduce a local copy here.

_INVITE_SHORT_HOSTS = frozenset({"discord.gg"})
_INVITE_LONG_HOSTS = frozenset(
    {"discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
)
_INVITE_LONG_PATH_PREFIX = "/invite/"
_INVITE_CODE_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")


@dataclass(frozen=True)
class ContentFilterResult:
    """What the filters found and did for one message.

    ``rejected_webhook_urls`` are webhook-shaped links that failed validation
    (foreign host, traversal, missing token): they are reported, and they alone
    never cause a webhook DELETE nor a message delete.
    ``already_dead_webhook_urls`` are valid webhooks whose DELETE returned 404 —
    an expected outcome, the webhook was already revoked.
    ``message_deleted`` is True when the single delete every fired filter shares
    succeeded; a validated leaked webhook removes the message just like a
    blocked TLD or an invite does.
    """

    blocked_tld_links: tuple[str, ...] = ()
    invite_links: tuple[str, ...] = ()
    killed_webhook_urls: tuple[str, ...] = ()
    already_dead_webhook_urls: tuple[str, ...] = ()
    rejected_webhook_urls: tuple[str, ...] = ()
    message_deleted: bool = False

    @property
    def took_action(self) -> bool:
        """True when any filter fired, whether or not the REST calls succeeded."""
        return bool(
            self.blocked_tld_links
            or self.invite_links
            or self.killed_webhook_urls
            or self.already_dead_webhook_urls
            or self.rejected_webhook_urls
        )


# ---------------------------------------------------------------------------
# Pure layer: link extraction
# ---------------------------------------------------------------------------


def extract_links(content: str) -> list[str]:
    """Every link-looking substring of ``content``, first-seen order, deduped.

    Bare hosts (``sketchy.xxx/now``) count as links: spammers routinely omit the
    scheme, and the legacy filter scanned raw text.
    """
    seen: set[str] = set()
    links: list[str] = []
    for match in _LINK_PATTERN.finditer(content):
        link = match.group(0).rstrip(_TRAILING_LINK_PUNCTUATION)
        if not link or link in seen:
            continue
        seen.add(link)
        links.append(link)
    return links


def link_host(link: str) -> str:
    """Lowercased host of ``link``, without scheme, userinfo, port or path."""
    remainder = link.split("://", 1)[-1]
    host = re.split(r"[/?#]", remainder, maxsplit=1)[0]
    host = host.rsplit("@", 1)[-1]
    host = host.split(":", 1)[0]
    return host.lower().rstrip(".")


def link_path(link: str) -> str:
    """Path portion of ``link`` (including the leading slash), '' when absent."""
    remainder = link.split("://", 1)[-1]
    slash_index = remainder.find("/")
    if slash_index == -1:
        return ""
    return remainder[slash_index:]


# ---------------------------------------------------------------------------
# Pure layer: blocked TLDs
# ---------------------------------------------------------------------------


def normalize_blocked_tlds(blocked_tlds: list[str]) -> tuple[str, ...]:
    """Config TLDs as lowercase dot-prefixed suffixes, blanks dropped.

    Accepts both ``.gay`` and ``gay`` because the admin UI stores whatever was
    typed.
    """
    normalized = {
        f".{tld.strip().lstrip('.').lower()}"
        for tld in blocked_tlds
        if tld and tld.strip().strip(".")
    }
    return tuple(sorted(normalized))


def find_blocked_tld_links(content: str, blocked_tlds: list[str]) -> list[str]:
    """Links in ``content`` whose host ends in one of ``blocked_tlds``.

    Suffix match on the host only, so ``.gay`` hits ``evil.gay`` but neither
    ``notgay.com`` nor ``x.gaymer.net``.
    """
    suffixes = normalize_blocked_tlds(blocked_tlds)
    if not suffixes:
        return []
    return [
        link
        for link in extract_links(content)
        if any(link_host(link).endswith(suffix) for suffix in suffixes)
    ]


# ---------------------------------------------------------------------------
# Pure layer: webhook URLs
# ---------------------------------------------------------------------------


def find_webhook_url_candidates(content: str) -> list[str]:
    """Links that claim to be Discord webhooks, valid or not.

    Validation is a separate step so a foreign-host lookalike is surfaced to the
    caller (and the log) instead of vanishing.
    """
    return [
        link
        for link in extract_links(content)
        if WEBHOOK_PATH_MARKER in link_path(link).lower()
    ]


def partition_webhook_candidates(
    webhook_candidates: list[str],
) -> tuple[list[str], list[str]]:
    """Split candidates into ``(deletable, rejected)``, order preserved.

    ``deletable`` are the urls that validate as real Discord webhooks and may
    become a DELETE; ``rejected`` are the webhook-shaped lookalikes, which are
    only ever reported.
    """
    deletable = [url for url in webhook_candidates if is_discord_webhook_url(url)]
    rejected = [url for url in webhook_candidates if not is_discord_webhook_url(url)]
    return deletable, rejected


def destroyed_webhook_ids(webhook_urls: list[str]) -> list[str]:
    """Webhook ids of ``webhook_urls``, deduped in first-seen order.

    Tokens are dropped here so no caller can accidentally put one in a log line
    or an audit row.
    """
    ids: list[str] = []
    for url in webhook_urls:
        reference = parse_discord_webhook_url(url)
        if reference is None or reference.webhook_id in ids:
            continue
        ids.append(reference.webhook_id)
    return ids


# ---------------------------------------------------------------------------
# Pure layer: invites
# ---------------------------------------------------------------------------


def find_invite_links(content: str) -> list[str]:
    """Discord invite links in ``content`` (``discord.gg/x``, ``/invite/x``).

    A bare ``discord.gg`` with no code, and any other discord.com path, are not
    invites.
    """
    invites: list[str] = []
    for link in extract_links(content):
        host = link_host(link)
        path = link_path(link)
        if host in _INVITE_SHORT_HOSTS:
            code = path.strip("/").split("/")[0]
        elif host in _INVITE_LONG_HOSTS and path.lower().startswith(
            _INVITE_LONG_PATH_PREFIX
        ):
            code = path[len(_INVITE_LONG_PATH_PREFIX) :].split("/")[0]
        else:
            continue
        if code and _INVITE_CODE_PATTERN.match(code):
            invites.append(link)
    return invites


# ---------------------------------------------------------------------------
# Pure layer: sanitization and formatting
# ---------------------------------------------------------------------------


def sanitize_link(link: str) -> str:
    """Spaced-out form of ``link`` that Discord will not linkify."""
    return link.replace("://", ":// ").replace(".", " . ")


def sanitize_links(text: str) -> str:
    """``text`` with every link spaced out so nothing in it stays clickable."""
    return _LINK_PATTERN.sub(lambda match: sanitize_link(match.group(0)), text)


def redact_webhook_tokens(text: str) -> str:
    """``text`` with the token of every Discord webhook url replaced.

    Mod-log lines quote attacker-controlled content, and that content is exactly
    where a leaked webhook url lives: republishing the token would hand the
    credential to everyone who can read the mod-log channel. The webhook id is
    kept so a moderator can still identify which webhook leaked.
    """

    def redact(match: re.Match[str]) -> str:
        reference = parse_discord_webhook_url(match.group(0))
        if reference is None:
            return match.group(0)
        normalized = normalize_webhook_url(match.group(0))
        return normalized[: -len(reference.token)] + "<token redacted>"

    return _LINK_PATTERN.sub(redact, text)


def truncate_content(text: str, limit: int = MOD_LOG_CONTENT_LIMIT) -> str:
    """``text`` capped at ``limit`` characters, ellipsis included in the cap."""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def build_mod_log_line(
    *,
    filter_name: str,
    author_id: str,
    author_username: str,
    channel_id: str,
    links: list[str],
    content: str,
) -> str:
    """One mod-log line: who, where, which links, and the sanitized content.

    Links and content are stripped of clickability and of any webhook token
    before they are quoted.
    """
    sanitized_links = (
        ", ".join(sanitize_link(redact_webhook_tokens(link)) for link in links)
        or "(none)"
    )
    body = truncate_content(sanitize_links(redact_webhook_tokens(content)))
    return (
        f"**{filter_name}** — {author_username} (`{author_id}`) in <#{channel_id}>\n"
        f"Links: {sanitized_links}\n"
        f"Content: {body}"
    )


def build_blocked_tld_notice(author_id: str) -> str:
    """In-channel notice for a message removed by the blocked-TLD filter."""
    return (
        f"<@{author_id}> your message was removed: links to that domain are not "
        f"allowed in this server."
    )


def build_invite_notice(author_id: str) -> str:
    """In-channel notice for a message removed by the invite filter."""
    return (
        f"<@{author_id}> your message was removed: Discord invite links are not "
        f"allowed in this channel."
    )


def build_webhook_killed_notice(
    *, author_id: str, killed_count: int, already_dead_count: int
) -> str:
    """In-channel notice explaining which leaked webhooks were destroyed."""
    total = killed_count + already_dead_count
    plural = "s" if total != 1 else ""
    detail = f"{killed_count} deleted"
    if already_dead_count:
        detail += f", {already_dead_count} already revoked"
    return (
        f"<@{author_id}> your message was removed: it leaked {total} Discord "
        f"webhook URL{plural} ({detail}). A leaked webhook lets anyone post to "
        f"this server, so it has been destroyed — create a new one and keep the "
        f"URL secret."
    )


def build_webhook_kill_mod_log_line(
    *,
    author_id: str,
    author_username: str,
    channel_id: str,
    webhook_ids: list[str],
) -> str:
    """Mod-log line for destroyed leaked webhooks: who, where, which ids.

    Deliberately quotes neither the urls nor the message content: a webhook
    token is a bearer credential, and a mod-log channel is not a place to
    republish one.
    """
    ids = ", ".join(f"`{webhook_id}`" for webhook_id in webhook_ids) or "(none)"
    return (
        f"**Leaked webhook** — {author_username} (`{author_id}`) in <#{channel_id}>\n"
        f"Destroyed webhook ids: {ids}\n"
        f"Tokens are never logged. The message was removed."
    )


def build_webhook_kill_reason(webhook_ids: list[str]) -> str:
    """Audit-row reason for a webhook kill; ids only, never tokens."""
    return "Leaked webhook destroyed: " + (", ".join(webhook_ids) or "(unknown)")


# ---------------------------------------------------------------------------
# Pure layer: exemptions
# ---------------------------------------------------------------------------


def author_is_staff_exempt(
    author_role_ids: list[str], staff_exempt_role_ids: list[str]
) -> bool:
    """True when the author holds any configured staff-exempt role."""
    exempt = {str(role_id) for role_id in staff_exempt_role_ids}
    return any(str(role_id) in exempt for role_id in author_role_ids)


def category_is_invite_exempt(
    parent_category_id: str | None, exempt_category_ids: list[str]
) -> bool:
    """True when the channel's parent category is invite-exempt.

    An unknown parent (uncached, or a channel with no category) is never exempt:
    exemption checks fail closed, so the message gets scanned.
    """
    if parent_category_id is None:
        return False
    return str(parent_category_id) in {
        str(category_id) for category_id in exempt_category_ids
    }


def member_has_manage_messages(
    guild: hikari.Guild | None, member: hikari.Member | None
) -> bool:
    """True when ``member`` can manage messages in ``guild``.

    Guild owners and administrators implicitly hold the permission. A missing
    guild or an uncached member fails closed (``False``) so an unknown author is
    filtered rather than trusted.
    """
    if guild is None or member is None:
        return False
    if guild.owner_id == member.id:
        return True
    permissions = hikari.Permissions.NONE
    for role_id in member.role_ids:
        role = guild.get_role(role_id)
        if role is not None:
            permissions |= role.permissions
    if permissions & hikari.Permissions.ADMINISTRATOR:
        return True
    return bool(permissions & hikari.Permissions.MANAGE_MESSAGES)


# ---------------------------------------------------------------------------
# Listener shell
# ---------------------------------------------------------------------------


async def check_content_filters(
    bot: RESTAware, event: hikari.GuildMessageCreateEvent
) -> ContentFilterResult:
    """Run the blocked-TLD, webhook-killer and invite filters over one message.

    Entry point for the gateway listener. Returns what was found and done; the
    listener needs nothing from the return value, it exists for tests and for
    callers that want to skip further processing of a removed message.

    Raises whatever the moderation-action write raises — a lost audit row is a
    real failure, not something to log and forget.
    """
    if event.is_bot or not event.guild_id:
        return ContentFilterResult()

    content = event.message.content or ""
    if not content.strip():
        return ContentFilterResult()

    config = await _load_config(str(event.guild_id))
    if config is None or not config.content_filter_enabled:
        return ContentFilterResult()

    if _author_is_exempt(event, config):
        return ContentFilterResult()

    blocked_tld_links = find_blocked_tld_links(content, config.blocked_tlds)
    invite_links = find_invite_links(content) if config.invite_filter_enabled else []
    if invite_links and await _invite_is_category_exempt(bot, event, config):
        invite_links = []
    webhook_candidates = (
        find_webhook_url_candidates(content) if config.webhook_killer_enabled else []
    )
    deletable_webhook_urls, rejected_webhook_urls = partition_webhook_candidates(
        webhook_candidates
    )
    for rejected_url in rejected_webhook_urls:
        logger.warning("Ignoring non-Discord webhook-shaped URL: %r", rejected_url)

    if not (blocked_tld_links or invite_links or webhook_candidates):
        return ContentFilterResult()

    # One delete serves every filter that fired. A leaked webhook URL is removed
    # along with the rest: the kill can only be as complete as the REST calls it
    # made, and a live-looking webhook URL sitting in channel history is a
    # standing invitation to whoever scrapes it next. A webhook-shaped lookalike
    # on a foreign host is NOT grounds for a delete — it is only reported.
    message_is_removable = bool(
        blocked_tld_links or invite_links or deletable_webhook_urls
    )
    message_deleted = (
        await _delete_offending_message(bot, event) if message_is_removable else False
    )
    recorded_reasons: list[str] = []

    if blocked_tld_links:
        await _send_notice(bot, event, build_blocked_tld_notice(str(event.author.id)))
        await _post_mod_log(
            bot,
            config,
            build_mod_log_line(
                filter_name="Blocked TLD",
                author_id=str(event.author.id),
                author_username=event.author.username,
                channel_id=str(event.channel_id),
                links=blocked_tld_links,
                content=content,
            ),
        )
        recorded_reasons.append(
            "Blocked TLD link removed: "
            + ", ".join(sanitize_link(link) for link in blocked_tld_links)
        )

    killed, already_dead = await _kill_leaked_webhooks(bot, deletable_webhook_urls)
    if killed or already_dead:
        destroyed_ids = destroyed_webhook_ids([*killed, *already_dead])
        await _send_notice(
            bot,
            event,
            build_webhook_killed_notice(
                author_id=str(event.author.id),
                killed_count=len(killed),
                already_dead_count=len(already_dead),
            ),
        )
        await _post_mod_log(
            bot,
            config,
            build_webhook_kill_mod_log_line(
                author_id=str(event.author.id),
                author_username=event.author.username,
                channel_id=str(event.channel_id),
                webhook_ids=destroyed_ids,
            ),
        )
        recorded_reasons.append(build_webhook_kill_reason(destroyed_ids))

    if invite_links:
        await _send_notice(bot, event, build_invite_notice(str(event.author.id)))
        await _post_mod_log(
            bot,
            config,
            build_mod_log_line(
                filter_name="Discord invite",
                author_id=str(event.author.id),
                author_username=event.author.username,
                channel_id=str(event.channel_id),
                links=invite_links,
                content=content,
            ),
        )
        recorded_reasons.append(
            "Invite link removed: "
            + ", ".join(sanitize_link(link) for link in invite_links)
        )

    if recorded_reasons:
        await _record_deletions(bot, event, recorded_reasons)

    return ContentFilterResult(
        blocked_tld_links=tuple(blocked_tld_links),
        invite_links=tuple(invite_links),
        killed_webhook_urls=tuple(killed),
        already_dead_webhook_urls=tuple(already_dead),
        rejected_webhook_urls=tuple(rejected_webhook_urls),
        message_deleted=message_deleted,
    )


async def _load_config(guild_id: str) -> ModerationFilterConfig | None:
    """Read the guild's filter config; None when the guild has never set one."""
    async with get_db_session_context() as session:
        return await moderation_filter_config_ops.get_config(session, guild_id)


def _author_is_exempt(
    event: hikari.GuildMessageCreateEvent, config: ModerationFilterConfig
) -> bool:
    """True when the author is staff and skips every content filter."""
    member = event.member
    author_role_ids = [str(role_id) for role_id in member.role_ids] if member else []
    if author_is_staff_exempt(author_role_ids, config.staff_exempt_role_ids):
        return True
    return member_has_manage_messages(event.get_guild(), member)


async def _invite_is_category_exempt(
    bot: RESTAware,
    event: hikari.GuildMessageCreateEvent,
    config: ModerationFilterConfig,
) -> bool:
    """True when this channel sits under an invite-exempt category.

    Fails closed on every REST outcome: if the channel cannot be resolved we
    cannot prove the exemption, so the message is filtered. A raid is exactly
    when this fetch gets rate-limited, and an unprovable exemption must never
    abort the blocked-TLD delete or the webhook kill that were already decided.
    """
    if not config.invite_filter_exempt_category_ids:
        return False
    channel = event.get_channel()
    if channel is None:
        try:
            channel = await bot.rest.fetch_channel(event.channel_id)
        except hikari.NotFoundError:
            logger.warning(
                "Channel %s vanished while resolving invite exemption", event.channel_id
            )
            return False
        except hikari.HTTPError:
            logger.warning(
                "Could not resolve channel %s for the invite exemption; treating "
                "the channel as not exempt",
                event.channel_id,
                exc_info=True,
            )
            return False
    parent_id = getattr(channel, "parent_id", None)
    parent_category_id = None if parent_id is None else str(parent_id)
    return category_is_invite_exempt(
        parent_category_id, config.invite_filter_exempt_category_ids
    )


async def _delete_offending_message(
    bot: RESTAware, event: hikari.GuildMessageCreateEvent
) -> bool:
    """Delete the message; False when it was already gone or undeletable.

    A 404 is the routine race with another moderator or bot, and a 403 means the
    bot is missing MANAGE_MESSAGES here — neither may abort the other filters.
    """
    try:
        await bot.rest.delete_message(event.channel_id, event.message.id)
    except hikari.NotFoundError:
        logger.info(
            "Message %s in channel %s was already deleted",
            event.message.id,
            event.channel_id,
        )
        return False
    except hikari.ForbiddenError:
        logger.warning(
            "Missing permission to delete message %s in channel %s",
            event.message.id,
            event.channel_id,
        )
        return False
    return True


async def _kill_leaked_webhooks(
    bot: RESTAware, deletable_webhook_urls: list[str]
) -> tuple[list[str], list[str]]:
    """DELETE every url in ``deletable_webhook_urls``; returns ``(killed, already_dead)``.

    Callers pass only urls that already validated as Discord webhooks (see
    :func:`partition_webhook_candidates`), so a lookalike host can never become
    an arbitrary-host DELETE. A url that somehow fails validation here is
    skipped rather than requested.
    """
    killed: list[str] = []
    already_dead: list[str] = []
    for url in deletable_webhook_urls:
        reference = parse_discord_webhook_url(url)
        if reference is None:
            logger.warning("Refusing to DELETE non-Discord webhook URL: %r", url)
            continue
        try:
            await bot.rest.delete_webhook(
                hikari.Snowflake(reference.webhook_id), token=reference.token
            )
        except hikari.NotFoundError:
            # Expected: the webhook was already revoked or deleted by someone else.
            logger.info("Leaked webhook %s was already gone", reference.webhook_id)
            already_dead.append(url)
            continue
        except hikari.UnauthorizedError:
            # Expected: a well-shaped URL whose token Discord rejects is not a
            # live webhook, so there is nothing to kill. Never a crash — a raid
            # can trivially paste one of these into every channel.
            logger.info(
                "Leaked webhook %s rejected its token; nothing to delete",
                reference.webhook_id,
            )
            already_dead.append(url)
            continue
        killed.append(url)
    return killed, already_dead


async def _send_notice(
    bot: RESTAware, event: hikari.GuildMessageCreateEvent, content: str
) -> None:
    """Post an in-channel notice mentioning the author."""
    try:
        await bot.rest.create_message(
            event.channel_id,
            content,
            user_mentions=[event.author.id],
            role_mentions=False,
            mentions_everyone=False,
        )
    except hikari.ForbiddenError:
        logger.warning(
            "Missing permission to post a filter notice in channel %s", event.channel_id
        )


async def _post_mod_log(
    bot: RESTAware, config: ModerationFilterConfig, line: str
) -> None:
    """Post a mod-log line when the guild configured a mod-log channel.

    All mentions are disabled: the line quotes attacker-controlled content.
    """
    if not config.mod_log_channel_id:
        return
    try:
        await bot.rest.create_message(
            int(config.mod_log_channel_id),
            line,
            user_mentions=False,
            role_mentions=False,
            mentions_everyone=False,
        )
    except hikari.ForbiddenError:
        logger.warning(
            "Missing permission to post to mod-log channel %s",
            config.mod_log_channel_id,
        )
    except hikari.NotFoundError:
        logger.warning(
            "Configured mod-log channel %s does not exist", config.mod_log_channel_id
        )


async def _record_deletions(
    bot: RESTAware, event: hikari.GuildMessageCreateEvent, reasons: list[str]
) -> None:
    """Write one ``ModerationAction`` per fired filter and fire ``mod_action``.

    ``bot`` is threaded through only so the dispatch can also write the delete
    into the guild's short-term event log — the chat agent has to be able to own
    a filter delete it made, the same as any other action of its account.

    Failures propagate: a missing audit row is a real problem, and unlike the
    slash commands there is no user-facing response that would otherwise lie.
    """
    async with get_db_session_context() as session:
        actions = [
            await mod_action_ops.create_action(
                session,
                guild_id=str(event.guild_id),
                target_user_id=str(event.author.id),
                target_username=event.author.username,
                action_type=DELETION_ACTION_TYPE,
                reason=reason,
                source=DELETION_SOURCE,
                channel_id=str(event.channel_id),
                trigger_message_id=str(event.message.id),
            )
            for reason in reasons
        ]
        await session.commit()
    for action in actions:
        await dispatch_mod_action(action, bot=bot)
