"""Bot-core heuristic spam engine (docs/v2/feature-parity §4.1 "Handler B").

Port of the legacy ``codes-auto-mod`` spam engine. It lives in bot core rather
than in an admin handler because the handler runtime drops fires above 120/min
(exactly what a raid produces), loads-before/persists-after its memory (so
concurrent fires lose buffer entries), and caps that memory at 16KB. In core the
rolling buffer is in-process with none of those limits.

Layering — everything except :func:`check_spam_engine` is Discord-free and
DB-free:

* content analysis (:func:`analyze_message_content`) — pure string logic,
* the rolling buffer (:class:`SpamMessageBuffer`) — pruned by age AND by hard
  per-author / per-guild entry caps on every insert, so a long-running bot
  cannot grow unbounded,
* the six metrics — pure functions over a list of :class:`BufferedMessage`,
  taking ``now`` as an argument (never reading the clock themselves) so tests
  are deterministic,
* the escalation decision (:func:`decide_spam_response`) — pure,
* :class:`GuildSpamState` — a plain state class holding the buffer plus the
  warning/muting timestamps.

Every threshold and window comes from ``ModerationFilterConfig``, including the
buffer's own bounds: :func:`derive_buffer_bounds` sizes retention from the
longest configured window and the per-author cap from the largest configured
count, so no configured value is silently unreachable. Only three things are
hardcoded: the reason phrasing, the ``d*.gift`` lookalike pattern (the domain
list itself is configured), and the absolute memory ceilings
(:data:`ABSOLUTE_MAX_RETENTION_SECONDS`, :data:`ABSOLUTE_MAX_ENTRIES_PER_AUTHOR`,
:data:`ABSOLUTE_MAX_TRACKED_AUTHORS`) — a config that exceeds one of those is
clamped, but never quietly: the offending setting is named in a warning log.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import deque
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING

import hikari

from smarter_dev.bot.guild_event_recorder import record_guild_event
from smarter_dev.bot.mod_action_dispatch import dispatch_mod_action
from smarter_dev.shared.database import get_db_session_context
from smarter_dev.shared.guild_event_log import GuildEvent
from smarter_dev.shared.guild_event_log import mod_action_event
from smarter_dev.web.crud import ModerationActionOperations
from smarter_dev.web.crud import ModerationFilterConfigOperations
from smarter_dev.web.models import ModerationFilterConfig

if TYPE_CHECKING:
    from lightbulb import BotApp

logger = logging.getLogger(__name__)

moderation_filter_config_ops = ModerationFilterConfigOperations()
mod_action_ops = ModerationActionOperations()

AUTO_MODERATION_REASON = "Auto-moderation violation"
MOD_ACTION_SOURCE = "handler"

SPAM_ACTION_NONE = "none"
SPAM_ACTION_WARN = "warn"
SPAM_ACTION_MUTE = "mute"

SPAM_REASON_MESSAGE_RATE = "sending messages so quickly"
SPAM_REASON_CHANNEL_SPREAD = "posting in so many channels"
SPAM_REASON_DUPLICATE = "repeating the same message"
SPAM_REASON_MASS_MENTION = "mentioning everyone"
SPAM_REASON_NITRO_SCAM = "posting nitro scams"
SPAM_REASON_SCAM_LINK = "posting scam links"

MASS_MENTION_TOKENS = ("@everyone", "@here")

#: Lowercase substrings that turn a mass mention into the nitro-scam variant.
NITRO_SCAM_KEYWORDS = ("nitro", "gift", "giveaway", "free discord")

#: The one legitimate gift domain — never treated as a scam link.
LEGITIMATE_GIFT_DOMAIN = "discord.gift"
GIFT_DOMAIN_SUFFIX = ".gift"

MUTE_ALERT_CONTENT_LIMIT = 900

_LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/[^\s<>\"'`]*)?",
    re.IGNORECASE,
)
_MASS_MENTION_PATTERN = re.compile("|".join(re.escape(token) for token in MASS_MENTION_TOKENS), re.IGNORECASE)
_ZERO_WIDTH_SPACE = "​"


# ---------------------------------------------------------------------------
# Content analysis (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageAnalysis:
    """What a single message's text contains, independent of any window."""

    contains_mass_mention: bool
    contains_nitro_keyword: bool
    scam_links: tuple[str, ...]


def extract_links(content: str) -> tuple[str, ...]:
    """Return every link-looking token in ``content`` (scheme optional)."""
    return tuple(match.group(0) for match in _LINK_PATTERN.finditer(content))


def link_host(link: str) -> str:
    """Return the lowercase host of ``link``, without scheme, userinfo or port."""
    without_scheme = link.split("://", 1)[-1]
    host = without_scheme.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.split("@")[-1].split(":", 1)[0]
    return host.strip().strip(".").lower()


def registrable_domain(host: str) -> str:
    """Return the last two labels of ``host`` ("www.a.gift" -> "a.gift")."""
    labels = [label for label in host.split(".") if label]
    if len(labels) < 2:
        return host
    return ".".join(labels[-2:])


def host_matches_configured_domain(host: str, scam_link_domain: str) -> bool:
    """Whether ``host`` IS ``scam_link_domain`` or a subdomain of it.

    The dot boundary is required, so "t.me" matches "t.me" and "x.t.me" but
    never "nott.me". Configured entries are already bare lowercase hostnames
    (see ``ModerationFilterConfig.scam_link_domains``), so lower-casing the
    host is the only normalization needed here.
    """
    return host == scam_link_domain or host.endswith(f".{scam_link_domain}")


def is_scam_link(link: str, scam_link_domains: Sequence[str]) -> bool:
    """Whether ``link`` is a scam lure.

    Two independent rules, either of which is enough:

    * the configured ``scam_link_domains`` list (per guild, admin-editable) —
      matched against the link's host by :func:`host_matches_configured_domain`;
    * the hardcoded ``d*.gift`` lookalike pattern (dlscord.gift, d1scord.gift,
      ...), which stays in code because it is a shape rather than a list.

    The legitimate ``discord.gift`` is excluded from both, so no config edit can
    turn Discord's own gift links into a moderated lure.
    """
    host = link_host(link)
    domain = registrable_domain(host)
    if domain == LEGITIMATE_GIFT_DOMAIN:
        return False
    if any(
        host_matches_configured_domain(host, scam_link_domain)
        for scam_link_domain in scam_link_domains
    ):
        return True
    return domain.startswith("d") and domain.endswith(GIFT_DOMAIN_SUFFIX)


def analyze_message_content(content: str, scam_link_domains: Sequence[str]) -> MessageAnalysis:
    """Classify one message's text: mass mention, nitro keyword, scam links."""
    lowered = content.lower()
    return MessageAnalysis(
        contains_mass_mention=any(token in lowered for token in MASS_MENTION_TOKENS),
        contains_nitro_keyword=any(keyword in lowered for keyword in NITRO_SCAM_KEYWORDS),
        scam_links=tuple(
            link for link in extract_links(content) if is_scam_link(link, scam_link_domains)
        ),
    )


def sanitize_link(link: str) -> str:
    """Defang a link so quoting it in a log channel is not clickable."""
    return link.replace("http", "hxxp").replace(".", "[.]")


def sanitize_content(content: str, max_length: int = MUTE_ALERT_CONTENT_LIMIT) -> str:
    """Defang links, neutralize mass mentions, and truncate for quoting."""
    sanitized = content
    for link in sorted(set(extract_links(content)), key=len, reverse=True):
        sanitized = sanitized.replace(link, sanitize_link(link))
    sanitized = _MASS_MENTION_PATTERN.sub(
        lambda match: f"@{_ZERO_WIDTH_SPACE}{match.group(0)[1:]}", sanitized
    )
    if len(sanitized) > max_length:
        sanitized = sanitized[: max_length - 1] + "…"
    return sanitized


# ---------------------------------------------------------------------------
# Rolling buffer (pure state)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BufferedMessage:
    """One buffered message: only what the metrics need, never the raw text."""

    timestamp: float
    author_id: str
    channel_id: str
    content_digest: str
    content_length: int
    contains_mass_mention: bool
    contains_nitro_keyword: bool
    contains_scam_link: bool


def content_digest(content: str) -> str:
    """Stable digest for the duplicate-message metric (whitespace/case folded)."""
    return hashlib.sha256(content.strip().casefold().encode("utf-8")).hexdigest()


def build_buffered_message(
    *,
    timestamp: float,
    author_id: str,
    channel_id: str,
    content: str,
    scam_link_domains: Sequence[str],
) -> BufferedMessage:
    """Analyze ``content`` once and fold it into a buffer entry."""
    analysis = analyze_message_content(content, scam_link_domains)
    stripped = content.strip()
    return BufferedMessage(
        timestamp=timestamp,
        author_id=author_id,
        channel_id=channel_id,
        content_digest=content_digest(content),
        content_length=len(stripped),
        contains_mass_mention=analysis.contains_mass_mention,
        contains_nitro_keyword=analysis.contains_nitro_keyword,
        contains_scam_link=bool(analysis.scam_links),
    )


@dataclass(frozen=True, slots=True)
class SpamBufferBounds:
    """How much history one guild's buffer is allowed to keep."""

    retention_seconds: float
    max_entries_per_author: int
    max_tracked_authors: int


class SpamMessageBuffer:
    """Per-author rolling buffer of recent messages, bounded three ways.

    Every insert prunes by age (``retention_seconds``); each author's deque is
    hard-capped at ``max_entries_per_author``; and the number of tracked authors
    is capped at ``max_tracked_authors``, evicting the least recently active
    author first. A raid therefore costs a fixed, small amount of memory.

    The bounds are per guild and derived from that guild's config by
    :func:`derive_buffer_bounds` — the defaults below only apply to a buffer
    built by hand (diagnostics and tests).
    """

    DEFAULT_RETENTION_SECONDS = 300.0
    DEFAULT_MAX_ENTRIES_PER_AUTHOR = 64
    DEFAULT_MAX_TRACKED_AUTHORS = 512

    def __init__(
        self,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        max_entries_per_author: int = DEFAULT_MAX_ENTRIES_PER_AUTHOR,
        max_tracked_authors: int = DEFAULT_MAX_TRACKED_AUTHORS,
    ) -> None:
        self._bounds = SpamBufferBounds(
            retention_seconds=retention_seconds,
            max_entries_per_author=max_entries_per_author,
            max_tracked_authors=max_tracked_authors,
        )
        # Insertion-ordered: the first key is the least recently active author.
        self._entries_by_author: dict[str, deque[BufferedMessage]] = {}

    @classmethod
    def from_bounds(cls, bounds: SpamBufferBounds) -> SpamMessageBuffer:
        return cls(
            retention_seconds=bounds.retention_seconds,
            max_entries_per_author=bounds.max_entries_per_author,
            max_tracked_authors=bounds.max_tracked_authors,
        )

    @property
    def bounds(self) -> SpamBufferBounds:
        return self._bounds

    def apply_bounds(self, bounds: SpamBufferBounds) -> None:
        """Adopt ``bounds``, keeping the newest entries that still fit.

        Called when a guild's config changes under a running buffer; a deque's
        ``maxlen`` is immutable, so the per-author deques are rebuilt.
        """
        if bounds == self._bounds:
            return
        self._bounds = bounds
        self._entries_by_author = {
            author_id: deque(entries, maxlen=bounds.max_entries_per_author)
            for author_id, entries in self._entries_by_author.items()
        }

    def record(self, entry: BufferedMessage) -> None:
        """Append ``entry`` and prune the whole buffer at ``entry.timestamp``."""
        existing = self._entries_by_author.pop(entry.author_id, None)
        author_entries = (
            existing
            if existing is not None
            else deque(maxlen=self._bounds.max_entries_per_author)
        )
        author_entries.append(entry)
        self._entries_by_author[entry.author_id] = author_entries
        self.prune(entry.timestamp)

    def prune(self, now: float) -> None:
        """Drop entries older than the retention window and evict stale authors."""
        cutoff = now - self._bounds.retention_seconds
        for author_id, author_entries in list(self._entries_by_author.items()):
            while author_entries and author_entries[0].timestamp <= cutoff:
                author_entries.popleft()
            if not author_entries:
                del self._entries_by_author[author_id]
        while len(self._entries_by_author) > self._bounds.max_tracked_authors:
            least_recently_active = next(iter(self._entries_by_author))
            del self._entries_by_author[least_recently_active]

    def entries_for_author(self, author_id: str) -> tuple[BufferedMessage, ...]:
        """This author's buffered messages, oldest first."""
        return tuple(self._entries_by_author.get(author_id, ()))

    def snapshot(self) -> dict[str, tuple[BufferedMessage, ...]]:
        """A copy of the whole buffer — for diagnostics and tests."""
        return {
            author_id: tuple(entries)
            for author_id, entries in self._entries_by_author.items()
        }

    def total_entries(self) -> int:
        return sum(len(entries) for entries in self._entries_by_author.values())

    def tracked_author_count(self) -> int:
        return len(self._entries_by_author)


# ---------------------------------------------------------------------------
# Thresholds / settings snapshot (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpamThresholds:
    """The tunables the metrics need, lifted off the config row."""

    message_rate_threshold: int
    message_rate_window_seconds: int
    channel_spread_threshold: int
    channel_spread_window_seconds: int
    duplicate_message_min_length: int
    duplicate_message_window_seconds: int
    mass_mention_window_seconds: int
    warning_reoffense_window_seconds: int
    mute_duration_seconds: int

    @classmethod
    def from_config(cls, config: ModerationFilterConfig) -> SpamThresholds:
        return cls(
            message_rate_threshold=config.message_rate_threshold,
            message_rate_window_seconds=config.message_rate_window_seconds,
            channel_spread_threshold=config.channel_spread_threshold,
            channel_spread_window_seconds=config.channel_spread_window_seconds,
            duplicate_message_min_length=config.duplicate_message_min_length,
            duplicate_message_window_seconds=config.duplicate_message_window_seconds,
            mass_mention_window_seconds=config.mass_mention_window_seconds,
            warning_reoffense_window_seconds=config.warning_reoffense_window_seconds,
            mute_duration_seconds=config.mute_duration_seconds,
        )


#: Retention keeps this much slack beyond the longest configured window, so an
#: entry is still there when the window that needs it is evaluated.
RETENTION_HEADROOM_FACTOR = 1.5
MINIMUM_RETENTION_SECONDS = 60.0

#: Per-author slack beyond the largest configured count: a threshold of N needs
#: N+1 entries to fire at all, and the doubling leaves room for the messages
#: that arrive while the escalation is being applied.
ENTRY_HEADROOM_FACTOR = 2
MINIMUM_MAX_ENTRIES_PER_AUTHOR = 32

#: Absolute ceilings. These are the ONLY values a guild's config cannot raise;
#: they exist so a mistyped threshold cannot exhaust the bot's memory. Worst
#: case per guild is ABSOLUTE_MAX_ENTRIES_PER_AUTHOR * ABSOLUTE_MAX_TRACKED_AUTHORS
#: buffered entries. Hitting one is logged as a warning naming the setting.
ABSOLUTE_MAX_RETENTION_SECONDS = 86_400.0
ABSOLUTE_MAX_ENTRIES_PER_AUTHOR = 512
ABSOLUTE_MAX_TRACKED_AUTHORS = 512


def _clamped_to_ceiling(value: float, ceiling: float, setting_names: Sequence[str]) -> float:
    """Return ``value`` capped at ``ceiling``, naming the settings when capped."""
    if value <= ceiling:
        return value
    logger.warning(
        "Spam engine clamped its buffer bound to %s: %s would need %s. "
        "Lower %s or raise the engine's absolute ceiling.",
        ceiling,
        " / ".join(setting_names),
        value,
        " / ".join(setting_names),
    )
    return ceiling


def derive_buffer_bounds(thresholds: SpamThresholds) -> SpamBufferBounds:
    """Size a guild's buffer from its config.

    Retention covers the longest configured window and the per-author cap
    covers the largest configured count, both with headroom — otherwise a
    configured value is silently unreachable (a 1800s duplicate window sees
    nothing if the buffer forgets after 300s; a rate threshold of 80 can never
    fire from a buffer holding 64 entries).
    """
    longest_window_seconds = max(
        thresholds.message_rate_window_seconds,
        thresholds.channel_spread_window_seconds,
        thresholds.duplicate_message_window_seconds,
        thresholds.mass_mention_window_seconds,
    )
    retention_seconds = _clamped_to_ceiling(
        max(longest_window_seconds * RETENTION_HEADROOM_FACTOR, MINIMUM_RETENTION_SECONDS),
        ABSOLUTE_MAX_RETENTION_SECONDS,
        (
            "message_rate_window_seconds",
            "channel_spread_window_seconds",
            "duplicate_message_window_seconds",
            "mass_mention_window_seconds",
        ),
    )
    largest_count = max(
        thresholds.message_rate_threshold,
        thresholds.channel_spread_threshold,
    )
    max_entries_per_author = _clamped_to_ceiling(
        max(
            (largest_count + 1) * ENTRY_HEADROOM_FACTOR,
            MINIMUM_MAX_ENTRIES_PER_AUTHOR,
        ),
        ABSOLUTE_MAX_ENTRIES_PER_AUTHOR,
        ("message_rate_threshold", "channel_spread_threshold"),
    )
    return SpamBufferBounds(
        retention_seconds=float(retention_seconds),
        max_entries_per_author=int(max_entries_per_author),
        max_tracked_authors=ABSOLUTE_MAX_TRACKED_AUTHORS,
    )


@dataclass(frozen=True, slots=True)
class SpamEngineSettings:
    """Immutable snapshot of a guild's config, taken inside the DB session.

    Snapshotting keeps every ORM attribute read inside the session and leaves
    the rest of the engine working on plain values.
    """

    enabled: bool
    staff_exempt_role_ids: tuple[str, ...]
    scam_link_domains: tuple[str, ...]
    thresholds: SpamThresholds
    mod_alert_channel_id: str | None
    mod_ping_role_id: str | None
    scam_log_channel_id: str | None

    @classmethod
    def from_config(cls, config: ModerationFilterConfig) -> SpamEngineSettings:
        return cls(
            enabled=bool(config.spam_engine_enabled),
            staff_exempt_role_ids=tuple(
                str(role_id) for role_id in (config.staff_exempt_role_ids or [])
            ),
            scam_link_domains=tuple(
                str(domain).lower() for domain in (config.scam_link_domains or [])
            ),
            thresholds=SpamThresholds.from_config(config),
            mod_alert_channel_id=config.mod_alert_channel_id,
            mod_ping_role_id=config.mod_ping_role_id,
            scam_log_channel_id=config.scam_log_channel_id,
        )


# ---------------------------------------------------------------------------
# The six metrics (pure)
# ---------------------------------------------------------------------------


def _metric_cutoff(
    now: float,
    window_seconds: float,
    last_warned_at: float | None,
) -> float:
    """The oldest timestamp a metric may still count, exclusive.

    The window's own start, moved forward to the author's last warning when that
    is more recent. This is what makes the warn step actionable: a metric that
    already earned a warning must re-fire on evidence NEWER than the warning
    before it can earn the mute, so an author who stops is not muted by the very
    burst they were just warned for.
    """
    cutoff = now - window_seconds
    if last_warned_at is None:
        return cutoff
    return max(cutoff, last_warned_at)


def _entries_within(
    entries: Sequence[BufferedMessage],
    now: float,
    window_seconds: float,
    last_warned_at: float | None = None,
) -> list[BufferedMessage]:
    cutoff = _metric_cutoff(now, window_seconds, last_warned_at)
    return [entry for entry in entries if entry.timestamp > cutoff]


def exceeds_message_rate(
    entries: Sequence[BufferedMessage],
    now: float,
    thresholds: SpamThresholds,
    last_warned_at: float | None = None,
) -> bool:
    """Metric 1 — more than N messages in W seconds, counted since the warning."""
    recent = _entries_within(
        entries, now, thresholds.message_rate_window_seconds, last_warned_at
    )
    return len(recent) > thresholds.message_rate_threshold


def exceeds_channel_spread(
    entries: Sequence[BufferedMessage],
    now: float,
    thresholds: SpamThresholds,
    last_warned_at: float | None = None,
) -> bool:
    """Metric 2 — more than N distinct channels in W seconds, since the warning."""
    recent = _entries_within(
        entries, now, thresholds.channel_spread_window_seconds, last_warned_at
    )
    distinct_channels = {entry.channel_id for entry in recent}
    return len(distinct_channels) > thresholds.channel_spread_threshold


def has_duplicate_message(
    entries: Sequence[BufferedMessage],
    now: float,
    thresholds: SpamThresholds,
    last_warned_at: float | None = None,
) -> bool:
    """Metric 3 — a repeat of a message longer than the configured minimum.

    The lookback is the duplicate window, shortened to "since the author's last
    warning" when they were warned more recently than that: repeats the author
    was already warned for must not be punished twice.
    """
    cutoff = _metric_cutoff(now, thresholds.duplicate_message_window_seconds, last_warned_at)
    seen_digests: set[str] = set()
    for entry in entries:
        if entry.timestamp <= cutoff:
            continue
        if entry.content_length <= thresholds.duplicate_message_min_length:
            continue
        if entry.content_digest in seen_digests:
            return True
        seen_digests.add(entry.content_digest)
    return False


def has_mass_mention(
    entries: Sequence[BufferedMessage],
    now: float,
    thresholds: SpamThresholds,
    last_warned_at: float | None = None,
) -> bool:
    """Metric 4 — an @everyone/@here in the window, posted since the warning."""
    recent = _entries_within(
        entries, now, thresholds.mass_mention_window_seconds, last_warned_at
    )
    return any(entry.contains_mass_mention for entry in recent)


def has_nitro_scam(
    entries: Sequence[BufferedMessage],
    now: float,
    thresholds: SpamThresholds,
) -> bool:
    """Metric 5 — a mass mention plus a nitro/gift keyword in the same window.

    Window-scoped rather than message-scoped on purpose: scammers routinely
    split the ping and the bait across two messages.

    This is the one metric deliberately NOT clamped to the author's last
    warning. A lone ``@everyone`` earns a warning by itself, so clamping would
    permanently blind metric 5 to the exact pattern it exists for — the ping and
    the bait as two messages. The clamp's purpose is to stop old evidence
    re-punishing an author who stopped; an author who follows a warned ping with
    nitro bait has not stopped. The cost is a false positive when an innocent
    follow-up happens to contain "gift"/"giveaway" within the window, which
    :func:`decide_spam_response` limits to a mute alert rather than a delete of
    a message carrying neither half of the scam.
    """
    recent = _entries_within(entries, now, thresholds.mass_mention_window_seconds)
    return any(entry.contains_mass_mention for entry in recent) and any(
        entry.contains_nitro_keyword for entry in recent
    )


def has_scam_link(entries: Sequence[BufferedMessage]) -> bool:
    """Metric 6 — the message just posted carries a scam link.

    Deliberately the latest entry only, not a window: this metric deletes the
    offending message, and deleting on the strength of an older message would
    delete the wrong one.
    """
    if not entries:
        return False
    return entries[-1].contains_scam_link


def current_message_has_mass_mention(entries: Sequence[BufferedMessage]) -> bool:
    """Whether the message just posted carries the @everyone/@here itself.

    Same reasoning as :func:`has_scam_link`: the DELETE decision is about the
    message in hand, so it may only read the latest entry. The window-scoped
    :func:`has_mass_mention` still drives the WARN/MUTE decision.
    """
    if not entries:
        return False
    return entries[-1].contains_mass_mention


def current_message_has_nitro_keyword(entries: Sequence[BufferedMessage]) -> bool:
    """Whether the message just posted carries the nitro/gift bait itself."""
    if not entries:
        return False
    return entries[-1].contains_nitro_keyword


@dataclass(frozen=True, slots=True)
class SpamMetrics:
    """The six metric outcomes for one author at one instant.

    The two ``current_message_*`` flags are not metrics of their own — they are
    the message-scoped half of the mass-mention and nitro signals, and only the
    delete decision reads them.
    """

    message_rate: bool
    channel_spread: bool
    duplicate_message: bool
    mass_mention: bool
    nitro_scam: bool
    scam_link: bool
    current_message_mass_mention: bool
    current_message_nitro_keyword: bool

    @property
    def triggered(self) -> bool:
        return any(
            (
                self.message_rate,
                self.channel_spread,
                self.duplicate_message,
                self.mass_mention,
                self.nitro_scam,
                self.scam_link,
            )
        )

    @property
    def reasons(self) -> tuple[str, ...]:
        """Human-readable reasons, most severe last (legacy "Please stop …")."""
        reasons = []
        if self.message_rate:
            reasons.append(SPAM_REASON_MESSAGE_RATE)
        if self.channel_spread:
            reasons.append(SPAM_REASON_CHANNEL_SPREAD)
        if self.duplicate_message:
            reasons.append(SPAM_REASON_DUPLICATE)
        if self.mass_mention:
            reasons.append(SPAM_REASON_MASS_MENTION)
        if self.nitro_scam:
            reasons.append(SPAM_REASON_NITRO_SCAM)
        if self.scam_link:
            reasons.append(SPAM_REASON_SCAM_LINK)
        return tuple(reasons)


def compute_spam_metrics(
    entries: Sequence[BufferedMessage],
    now: float,
    thresholds: SpamThresholds,
    last_warned_at: float | None = None,
) -> SpamMetrics:
    """Run all six metrics over one author's buffered messages.

    ``last_warned_at`` clamps every window-scoped metric except the nitro-scam
    one (see :func:`has_nitro_scam`) to evidence newer than the warning.
    """
    return SpamMetrics(
        message_rate=exceeds_message_rate(entries, now, thresholds, last_warned_at),
        channel_spread=exceeds_channel_spread(entries, now, thresholds, last_warned_at),
        duplicate_message=has_duplicate_message(entries, now, thresholds, last_warned_at),
        mass_mention=has_mass_mention(entries, now, thresholds, last_warned_at),
        nitro_scam=has_nitro_scam(entries, now, thresholds),
        scam_link=has_scam_link(entries),
        current_message_mass_mention=current_message_has_mass_mention(entries),
        current_message_nitro_keyword=current_message_has_nitro_keyword(entries),
    )


# ---------------------------------------------------------------------------
# Escalation decision (pure)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpamDecision:
    """What the engine decided to do about one message."""

    action: str
    reasons: tuple[str, ...]
    should_delete_message: bool


NO_SPAM_ACTION = SpamDecision(action=SPAM_ACTION_NONE, reasons=(), should_delete_message=False)


def decide_spam_response(metrics: SpamMetrics, *, warned_recently: bool) -> SpamDecision:
    """Map metrics + warning state onto warn / mute / nothing.

    Mute when the author was warned inside the re-offense window, or when the
    nitro-scam or scam-link metric fired (those never get a warning). The
    escalation reads the window-scoped metrics, which are already clamped to
    evidence newer than the warning.

    The delete is decided separately and only from the message in hand: it goes
    only when THAT message carries the ping, the scam link, or the nitro bait
    half of a scam. A window-scoped metric must never delete the current
    message, because the message that earned the metric may be an older one.
    """
    if not metrics.triggered:
        return NO_SPAM_ACTION
    escalate_to_mute = warned_recently or metrics.nitro_scam or metrics.scam_link
    deletes_current_message = (
        metrics.current_message_mass_mention
        or metrics.scam_link
        or (metrics.nitro_scam and metrics.current_message_nitro_keyword)
    )
    return SpamDecision(
        action=SPAM_ACTION_MUTE if escalate_to_mute else SPAM_ACTION_WARN,
        reasons=metrics.reasons,
        should_delete_message=deletes_current_message,
    )


def is_exempt_author(
    *,
    is_bot: bool,
    has_manage_messages: bool,
    author_role_ids: Iterable[str],
    staff_exempt_role_ids: Iterable[str],
) -> bool:
    """Whether this author is outside the engine's reach entirely."""
    if is_bot or has_manage_messages:
        return True
    return bool(set(author_role_ids) & set(staff_exempt_role_ids))


# ---------------------------------------------------------------------------
# Message formatting (pure)
# ---------------------------------------------------------------------------


def build_jump_link(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def format_warning_message(author_id: str, reasons: Sequence[str]) -> str:
    return f"<@{author_id}> Please stop {', '.join(reasons)}."


def format_mute_alert(
    *,
    ping_role_id: str | None,
    author_id: str,
    author_username: str,
    reasons: Sequence[str],
    mute_duration_seconds: int,
    jump_link: str,
    sanitized_content: str,
) -> str:
    """The mod-alert post. Any mention text inside it is already neutralized."""
    ping = f"<@&{ping_role_id}> " if ping_role_id else ""
    lines = [
        f"{ping}Auto-moderation muted **{author_username}** (<@{author_id}>) "
        f"for {mute_duration_seconds}s.",
        f"Reasons: {', '.join(reasons)}",
        f"Message: {jump_link}",
    ]
    if sanitized_content:
        lines.append(f"> {sanitized_content}")
    return "\n".join(lines)


def format_scam_log_entry(
    *,
    author_id: str,
    author_username: str,
    channel_id: str,
    scam_links: Sequence[str],
) -> str:
    sanitized = ", ".join(sanitize_link(link) for link in scam_links)
    return (
        f"Scam links from **{author_username}** (<@{author_id}>) "
        f"in <#{channel_id}>: {sanitized}"
    )


# ---------------------------------------------------------------------------
# Per-guild state (plain state class, no Discord/DB)
# ---------------------------------------------------------------------------


class GuildSpamState:
    """One guild's rolling buffer plus its warning and muting timestamps."""

    def __init__(self, buffer: SpamMessageBuffer | None = None) -> None:
        self.buffer = buffer if buffer is not None else SpamMessageBuffer()
        self._warned_at: dict[str, float] = {}
        self._muted_until: dict[str, float] = {}
        # Set by prune(); until then the state is never considered releasable.
        self._retains_nothing_after = float("inf")

    def record_message(self, entry: BufferedMessage) -> None:
        self.buffer.record(entry)

    def entries_for_author(self, author_id: str) -> tuple[BufferedMessage, ...]:
        return self.buffer.entries_for_author(author_id)

    def last_warned_at(self, author_id: str) -> float | None:
        return self._warned_at.get(author_id)

    def was_warned_recently(
        self,
        author_id: str,
        now: float,
        thresholds: SpamThresholds,
    ) -> bool:
        warned_at = self._warned_at.get(author_id)
        if warned_at is None:
            return False
        return now - warned_at <= thresholds.warning_reoffense_window_seconds

    def record_warning(self, author_id: str, timestamp: float) -> None:
        self._warned_at[author_id] = timestamp

    def is_muted(self, author_id: str, now: float) -> bool:
        muted_until = self._muted_until.get(author_id)
        return muted_until is not None and muted_until > now

    def record_mute(self, author_id: str, muted_until: float) -> None:
        self._muted_until[author_id] = muted_until

    def prune(self, now: float, thresholds: SpamThresholds) -> None:
        """Drop expired mutes, stale warnings, and aged-out buffer entries."""
        self.buffer.prune(now)
        window = thresholds.warning_reoffense_window_seconds
        self._warned_at = {
            author_id: warned_at
            for author_id, warned_at in self._warned_at.items()
            if now - warned_at <= window
        }
        self._muted_until = {
            author_id: muted_until
            for author_id, muted_until in self._muted_until.items()
            if muted_until > now
        }
        self._retains_nothing_after = max(
            now + self.buffer.bounds.retention_seconds + window,
            max(self._muted_until.values(), default=now),
        )

    def retains_nothing_at(self, now: float) -> bool:
        """Whether every entry, warning and mute this state holds has expired.

        True means the state is information-free: dropping it at ``now`` cannot
        change any later decision, so the registry may release it.
        """
        return now >= self._retains_nothing_after


_guild_spam_states: dict[str, GuildSpamState] = {}


def get_guild_spam_state(
    guild_id: str,
    thresholds: SpamThresholds,
    now: float,
) -> GuildSpamState:
    """The process-wide state for ``guild_id``, sized from that guild's config.

    Also the sweep point for idle guilds: without it a guild that goes quiet
    right after a raid would hold its buffer until the process restarts, since a
    guild's own state is only pruned when that guild sees another message.
    ``release_guild_spam_state`` covers the sharper case of leaving a guild.
    """
    release_idle_guild_spam_states(now)
    bounds = derive_buffer_bounds(thresholds)
    state = _guild_spam_states.get(guild_id)
    if state is None:
        state = GuildSpamState(buffer=SpamMessageBuffer.from_bounds(bounds))
        _guild_spam_states[guild_id] = state
    else:
        # The guild's config may have been edited under a running buffer.
        state.buffer.apply_bounds(bounds)
    return state


def release_idle_guild_spam_states(now: float) -> tuple[str, ...]:
    """Drop every guild state that no longer holds anything; return their ids."""
    idle_guild_ids = tuple(
        guild_id
        for guild_id, state in _guild_spam_states.items()
        if state.retains_nothing_at(now)
    )
    for guild_id in idle_guild_ids:
        del _guild_spam_states[guild_id]
    return idle_guild_ids


def release_guild_spam_state(guild_id: str) -> bool:
    """Forget one guild entirely — call this when the bot leaves it.

    Returns whether there was any state to release.
    """
    return _guild_spam_states.pop(guild_id, None) is not None


def tracked_guild_count() -> int:
    """How many guilds the process-wide registry currently holds."""
    return len(_guild_spam_states)


def reset_spam_states() -> None:
    """Drop every guild's state — for tests and for a clean reconnect."""
    _guild_spam_states.clear()


# ---------------------------------------------------------------------------
# Discord/DB shell
# ---------------------------------------------------------------------------


def resolve_member_permissions(guild, member) -> hikari.Permissions:
    """Guild-level permission union for ``member`` (owner implies ADMINISTRATOR).

    Fails closed on exemption: an uncached guild or member yields NO permissions,
    so the message gets scanned rather than silently skipped.
    """
    if guild is None or member is None:
        return hikari.Permissions.NONE
    if getattr(guild, "owner_id", None) == member.id:
        return hikari.Permissions.ADMINISTRATOR
    permissions = hikari.Permissions.NONE
    for role_id in member.role_ids:
        role = guild.get_role(role_id)
        if role is not None:
            permissions |= role.permissions
    return permissions


def has_moderator_permissions(permissions: hikari.Permissions) -> bool:
    return bool(
        permissions & (hikari.Permissions.MANAGE_MESSAGES | hikari.Permissions.ADMINISTRATOR)
    )


async def load_spam_engine_settings(guild_id: str) -> SpamEngineSettings | None:
    """Read the guild's moderation config and snapshot it as plain values."""
    async with get_db_session_context() as session:
        config = await moderation_filter_config_ops.get_config(session, guild_id)
        if config is None:
            return None
        return SpamEngineSettings.from_config(config)


async def check_spam_engine(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent,
    *,
    state: GuildSpamState | None = None,
    now: float | None = None,
) -> SpamDecision:
    """Run the spam engine over one guild message.

    This is the listener entry point: a thin shell around the pure layer above.
    It loads the guild config, applies the exemption and re-entrancy gates,
    records the message in the rolling buffer, computes the six metrics, and
    performs whatever the decision asks for (warn, mute + mod alert + audit row,
    delete + scam-log post).

    Args:
        bot: the bot instance (only ``bot.rest`` and ``bot.get_me()`` are used).
        event: the guild message create event.
        state: injected per-guild state; defaults to the process-wide registry.
        now: injected epoch-seconds clock; defaults to ``time.time()``.

    Returns:
        The :class:`SpamDecision` that was acted on — ``NO_SPAM_ACTION`` when the
        engine is disabled, the author is exempt, or nothing fired.
    """
    guild_id = getattr(event, "guild_id", None)
    author = getattr(event, "author", None)
    if guild_id is None or author is None or author.is_bot:
        return NO_SPAM_ACTION

    settings = await load_spam_engine_settings(str(guild_id))
    if settings is None or not settings.enabled:
        return NO_SPAM_ACTION

    member = getattr(event, "member", None)
    author_role_ids = tuple(str(role_id) for role_id in member.role_ids) if member else ()
    permissions = resolve_member_permissions(event.get_guild(), member)
    if is_exempt_author(
        is_bot=author.is_bot,
        has_manage_messages=has_moderator_permissions(permissions),
        author_role_ids=author_role_ids,
        staff_exempt_role_ids=settings.staff_exempt_role_ids,
    ):
        return NO_SPAM_ACTION

    timestamp = time.time() if now is None else now
    thresholds = settings.thresholds
    guild_state = (
        state
        if state is not None
        else get_guild_spam_state(str(guild_id), thresholds, timestamp)
    )
    guild_state.prune(timestamp, thresholds)

    author_id = str(author.id)
    if guild_state.is_muted(author_id, timestamp):
        # Re-entrancy guard: the mute is already applied and announced.
        return NO_SPAM_ACTION

    content = event.content or ""
    guild_state.record_message(
        build_buffered_message(
            timestamp=timestamp,
            author_id=author_id,
            channel_id=str(event.channel_id),
            content=content,
            scam_link_domains=settings.scam_link_domains,
        )
    )
    metrics = compute_spam_metrics(
        guild_state.entries_for_author(author_id),
        timestamp,
        thresholds,
        last_warned_at=guild_state.last_warned_at(author_id),
    )
    decision = decide_spam_response(
        metrics,
        warned_recently=guild_state.was_warned_recently(author_id, timestamp, thresholds),
    )
    if decision.action == SPAM_ACTION_NONE:
        return decision

    if decision.action == SPAM_ACTION_MUTE:
        timeout_applied = await _mute_author(bot, event, settings, decision, timestamp)
        # Only arm the re-entrancy guard when Discord actually applied the timeout.
        # Recording it unconditionally would silence the engine for the full mute
        # duration against an author who was never muted (missing permissions, role
        # hierarchy, a departed member, or a rejected duration), handing them a free
        # pass for exactly as long as the punishment they escaped.
        if timeout_applied:
            guild_state.record_mute(author_id, timestamp + thresholds.mute_duration_seconds)
    else:
        await _warn_author(bot, event, decision)
        guild_state.record_warning(author_id, timestamp)

    if decision.should_delete_message:
        await _delete_offending_message(bot, event, decision)
        await _post_scam_log(
            bot,
            event,
            settings,
            analyze_message_content(content, settings.scam_link_domains),
        )

    logger.info(
        "Spam engine %s %s in guild %s: %s",
        decision.action,
        author_id,
        guild_id,
        ", ".join(decision.reasons),
    )
    return decision


async def _warn_author(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent,
    decision: SpamDecision,
) -> None:
    """Post "Please stop …" in the offending channel, pinging only the author."""
    try:
        await bot.rest.create_message(
            event.channel_id,
            format_warning_message(str(event.author.id), decision.reasons),
            mentions_everyone=False,
            user_mentions=[event.author.id],
            role_mentions=False,
        )
    except hikari.ForbiddenError:
        logger.warning(
            "Cannot post spam warning in channel %s (missing permissions)", event.channel_id
        )
        return
    # TODO(§3.8): a spam warning writes no ModerationAction row today, so the
    # short-term event log is captured here. When feature-parity §3.8 gives it a
    # row it will flow through dispatch_mod_action — drop this call then, or the
    # warning lands in the bot's memory twice.
    await record_guild_event(bot, _spam_action_event(event, "warn", decision.reasons))


async def _mute_author(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent,
    settings: SpamEngineSettings,
    decision: SpamDecision,
    timestamp: float,
) -> bool:
    """Timeout the author, record the audit row, and alert the mods.

    Returns whether Discord actually applied the timeout, so the caller can
    decide whether to arm its re-entrancy guard. The mods are alerted either
    way — a refused timeout is precisely when they need to know.
    """
    duration_seconds = settings.thresholds.mute_duration_seconds
    timeout_applied = await _apply_timeout(bot, event, timestamp, duration_seconds)
    if timeout_applied:
        await _record_timeout_action(bot, event, duration_seconds)
    await _post_mute_alert(bot, event, settings, decision)
    return timeout_applied


async def _apply_timeout(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent,
    timestamp: float,
    duration_seconds: int,
) -> bool:
    """Apply the native Discord timeout. False when Discord refused it."""
    muted_until = datetime.fromtimestamp(timestamp, UTC) + timedelta(seconds=duration_seconds)
    try:
        await bot.rest.edit_member(
            event.guild_id,
            event.author.id,
            communication_disabled_until=muted_until,
            reason=AUTO_MODERATION_REASON,
        )
        return True
    except (hikari.ForbiddenError, hikari.NotFoundError) as error:
        # Missing MODERATE_MEMBERS / role hierarchy, or the member already left.
        # The mods still get alerted; no audit row for an action that did not land.
        logger.warning(
            "Spam engine could not time out %s in guild %s: %s",
            event.author.id,
            event.guild_id,
            error,
        )
        return False
    except hikari.BadRequestError as error:
        # Discord rejected the timeout itself — a duration past its 28-day limit
        # is the likely cause. Defense in depth: the admin form already caps the
        # duration, but one bad config value must not abort the rest of the
        # enforcement (the delete of a scam link happens after this call).
        logger.warning(
            "Discord rejected a %ss timeout for %s in guild %s: %s",
            duration_seconds,
            event.author.id,
            event.guild_id,
            error,
        )
        return False


async def _record_timeout_action(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent,
    duration_seconds: int,
) -> None:
    """Write the ``source="handler"`` audit row and fire the mod_action trigger."""
    bot_user = bot.get_me()
    async with get_db_session_context() as session:
        action = await mod_action_ops.create_action(
            session,
            guild_id=str(event.guild_id),
            target_user_id=str(event.author.id),
            target_username=event.author.username,
            moderator_user_id=str(bot_user.id) if bot_user else None,
            moderator_username=bot_user.username if bot_user else None,
            action_type="timeout",
            reason=AUTO_MODERATION_REASON,
            duration_seconds=duration_seconds,
            source=MOD_ACTION_SOURCE,
            channel_id=str(event.channel_id),
            trigger_message_id=str(event.message_id),
        )
        await session.commit()
    await dispatch_mod_action(action, bot=bot)


async def _post_mute_alert(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent,
    settings: SpamEngineSettings,
    decision: SpamDecision,
) -> None:
    """Alert the mod channel, pinging ONLY the configured mod role.

    ``allowed_mentions`` is built explicitly and never allows @everyone/@here,
    so quoting the offending content can never re-broadcast the ping.
    """
    if not settings.mod_alert_channel_id:
        logger.debug("No mod alert channel configured for guild %s", event.guild_id)
        return
    ping_role_id = settings.mod_ping_role_id
    content = format_mute_alert(
        ping_role_id=ping_role_id,
        author_id=str(event.author.id),
        author_username=event.author.username,
        reasons=decision.reasons,
        mute_duration_seconds=settings.thresholds.mute_duration_seconds,
        jump_link=build_jump_link(
            str(event.guild_id), str(event.channel_id), str(event.message_id)
        ),
        sanitized_content=sanitize_content(event.content or ""),
    )
    try:
        await bot.rest.create_message(
            hikari.Snowflake(settings.mod_alert_channel_id),
            content,
            mentions_everyone=False,
            user_mentions=False,
            role_mentions=[hikari.Snowflake(ping_role_id)] if ping_role_id else False,
        )
    except hikari.ForbiddenError:
        logger.warning(
            "Cannot post spam mute alert to channel %s (missing permissions)",
            settings.mod_alert_channel_id,
        )


async def _delete_offending_message(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent,
    decision: SpamDecision,
) -> None:
    """Delete the offending message; a 404 means someone else already did.

    ``decision`` is here for its reasons: the delete carries no audit row, so
    the short-term event log is the only place that records WHY the bot removed
    the message, and "I deleted a message" without the why is useless to it.
    """
    try:
        await bot.rest.delete_message(event.channel_id, event.message_id)
    except hikari.NotFoundError:
        logger.debug("Offending message %s was already deleted", event.message_id)
        return
    except hikari.ForbiddenError:
        logger.warning(
            "Cannot delete offending message in channel %s (missing permissions)",
            event.channel_id,
        )
        return
    # TODO(§3.8): same as the warn above — no ModerationAction row exists for a
    # spam delete yet, so it is captured here rather than via dispatch_mod_action.
    await record_guild_event(bot, _spam_action_event(event, "delete", decision.reasons))


def _spam_action_event(
    event: hikari.GuildMessageCreateEvent,
    action_type: str,
    reasons: Sequence[str],
) -> GuildEvent:
    """The short-term-memory event for a row-less spam action, as the bot recalls it."""
    return mod_action_event(
        {
            "action_type": action_type,
            "target_username": event.author.username,
            "reason": ", ".join(reasons),
            "source": MOD_ACTION_SOURCE,
            "channel_id": str(event.channel_id),
        },
        guild_id=str(event.guild_id),
    )


async def _post_scam_log(
    bot: BotApp,
    event: hikari.GuildMessageCreateEvent,
    settings: SpamEngineSettings,
    analysis: MessageAnalysis,
) -> None:
    """Archive the sanitized scam links; nothing to archive means no post."""
    if not analysis.scam_links or not settings.scam_log_channel_id:
        return
    content = format_scam_log_entry(
        author_id=str(event.author.id),
        author_username=event.author.username,
        channel_id=str(event.channel_id),
        scam_links=analysis.scam_links,
    )
    try:
        await bot.rest.create_message(
            hikari.Snowflake(settings.scam_log_channel_id),
            content,
            mentions_everyone=False,
            user_mentions=False,
            role_mentions=False,
        )
    except hikari.ForbiddenError:
        logger.warning(
            "Cannot post to scam log channel %s (missing permissions)",
            settings.scam_log_channel_id,
        )
