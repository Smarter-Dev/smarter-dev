"""The coordinator's own process state: activity, recent posts, rings, verdicts.

None of it is shared with the legacy actors. The spam engine's buffer lives
in one process-wide dict; feeding the same messages into it from here would
count every message twice, so the coordinator keeps its own
:class:`SpamTracker` over the engine's own buffer and state classes.

Everything is bounded: buffers prune by age, the ring registry by its
follow-up window, the verdict cache by :data:`VERDICT_TTL_SECONDS`.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterator
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace

from smarter_dev.bot.moderation_coordinator.model import LabelVerdict
from smarter_dev.bot.moderation_coordinator.model import MessageFacts
from smarter_dev.bot.moderation_coordinator.model import RecentPost
from smarter_dev.bot.moderation_coordinator.model import Sanction
from smarter_dev.bot.moderation_coordinator.rules import RingIncident
from smarter_dev.bot.spam_engine import NO_SPAM_ACTION
from smarter_dev.bot.spam_engine import SPAM_ACTION_MUTE
from smarter_dev.bot.spam_engine import SPAM_ACTION_NONE
from smarter_dev.bot.spam_engine import GuildSpamState
from smarter_dev.bot.spam_engine import SpamBufferBounds
from smarter_dev.bot.spam_engine import SpamDecision
from smarter_dev.bot.spam_engine import SpamEngineSettings
from smarter_dev.bot.spam_engine import SpamMessageBuffer
from smarter_dev.bot.spam_engine import SpamMetrics
from smarter_dev.bot.spam_engine import build_buffered_message
from smarter_dev.bot.spam_engine import compute_spam_metrics
from smarter_dev.bot.spam_engine import decide_spam_response
from smarter_dev.bot.spam_engine import derive_buffer_bounds

#: How long a post is remembered for burst detection (scam-banner's window).
RECENT_POST_SECONDS = 120.0
MAX_RECENT_POSTS = 30
#: How long a verdict on one post is reused for its copies.
VERDICT_TTL_SECONDS = 120.0
#: How long a post's scam sanction is remembered against its copies.
SANCTION_TTL_SECONDS = 600.0
#: How long after a burst's first scam-gate post a member's next one still
#: joins it (one incident, one sanction). Measured from the first post, so
#: a member posting every minute starts a new burst every two minutes
#: instead of staying in one for good. The 14:07Z burst took 1.9 s; 120 s
#: is the verdict cache's reach (VERDICT_TTL_SECONDS) and the scam-banner
#: window (RECENT_POST_SECONDS), so a burst's posts are those its first
#: verdict can still answer.
BURST_MAX_SECONDS = 120.0
MAX_TRACKED_AUTHORS = 4096


@dataclass(frozen=True, slots=True)
class SpamObservation:
    """The engine's view of one message, before the staff rule is asked."""

    decision: SpamDecision
    metrics: SpamMetrics | None


class SpamTracker:
    """The spam engine's metrics over the coordinator's own buffer.

    Same steps as ``check_spam_engine``: prune, skip an author already muted
    (their messages are not even recorded), record, compute, decide. The
    warning or mute is committed only after the staff rule cleared the
    author (:meth:`commit`), as the engine records them only once it acted.
    """

    def __init__(self) -> None:
        self._states: dict[str, GuildSpamState] = {}
        self._bounds: dict[str, SpamBufferBounds] = {}

    def _state(self, guild_id: str, settings: SpamEngineSettings) -> GuildSpamState:
        bounds = derive_buffer_bounds(settings.thresholds)
        state = self._states.get(guild_id)
        if state is None:
            state = GuildSpamState(buffer=SpamMessageBuffer.from_bounds(bounds))
            self._states[guild_id] = state
        elif self._bounds.get(guild_id) != bounds:
            state.buffer.apply_bounds(bounds)
        self._bounds[guild_id] = bounds
        return state

    def observe(
        self,
        facts: MessageFacts,
        settings: SpamEngineSettings,
        now: float,
        *,
        mass_mention_rule: bool = False,
    ) -> SpamObservation:
        """The engine's decision on this message.

        ``mass_mention_rule``: the coordinator's own ``@everyone``/``@here``
        rule (delete only, code exempt; Zech, Oct 23 22:36) is on, so the
        engine's plain mass-mention metric (a warning and a delete by itself)
        is left to it. Nitro bait with a mass mention still mutes."""
        state = self._state(facts.guild_id, settings)
        thresholds = settings.thresholds
        state.prune(now, thresholds)
        if state.is_muted(facts.author_id, now):
            return SpamObservation(NO_SPAM_ACTION, None)
        state.record_message(
            build_buffered_message(
                timestamp=now,
                author_id=facts.author_id,
                channel_id=facts.channel_id,
                content=facts.content,
                scam_link_domains=settings.scam_link_domains,
            )
        )
        metrics = compute_spam_metrics(
            state.entries_for_author(facts.author_id),
            now,
            thresholds,
            last_warned_at=state.last_warned_at(facts.author_id),
        )
        if mass_mention_rule:
            metrics = replace(
                metrics, mass_mention=False, current_message_mass_mention=False
            )
        decision = decide_spam_response(
            metrics,
            warned_recently=state.was_warned_recently(facts.author_id, now, thresholds),
        )
        return SpamObservation(decision, metrics)

    def commit(
        self,
        facts: MessageFacts,
        settings: SpamEngineSettings,
        decision: SpamDecision,
        now: float,
    ) -> None:
        """Arm the re-entrancy guard or the warning, as if the engine had acted."""
        if decision.action == SPAM_ACTION_NONE:
            return
        state = self._state(facts.guild_id, settings)
        if decision.action == SPAM_ACTION_MUTE:
            state.record_mute(
                facts.author_id, now + settings.thresholds.mute_duration_seconds
            )
        else:
            state.record_warning(facts.author_id, now)

    def disarm(self, guild_id: str, author_id: str) -> None:
        """Undo :meth:`commit`'s mute when the timeout did not take effect,
        as the engine arms its guard only once the author is timed out: an
        author the timeout missed is not let off for its whole length."""
        state = self._states.get(guild_id)
        if state is not None:
            state.clear_mute(author_id)


class RecentPosts:
    """Each author's posts in the last two minutes, for burst detection.

    The same notes the handler dispatch keeps in Redis for scam-banner
    (``handler_recent_messages``): where, when and a hash of how it looked,
    never the text.
    """

    def __init__(self) -> None:
        self._posts: dict[tuple[str, str], deque[RecentPost]] = {}

    def _trim(self, posts: deque[RecentPost], now: float) -> None:
        while posts and posts[0].posted_at < now - RECENT_POST_SECONDS:
            posts.popleft()
        while len(posts) > MAX_RECENT_POSTS:
            posts.popleft()

    def read(self, guild_id: str, author_id: str, now: float) -> list[RecentPost]:
        posts = self._posts.get((guild_id, author_id))
        if not posts:
            return []
        self._trim(posts, now)
        return list(posts)

    def record(self, guild_id: str, author_id: str, post: RecentPost) -> None:
        posts = self._posts.setdefault((guild_id, author_id), deque())
        posts.append(post)
        self._trim(posts, post.posted_at)
        if len(self._posts) > MAX_TRACKED_AUTHORS:
            self.prune(post.posted_at)

    def prune(self, now: float) -> None:
        for key, posts in list(self._posts.items()):
            self._trim(posts, now)
            if not posts:
                del self._posts[key]


class RingRegistry:
    """The rings the coordinator found, per channel, for their follow-ups.

    :meth:`channel` serialises the ring's work in one channel, as the spam
    ring's own channel lock does: a ring message that arrives while the ad is
    being judged waits, and is then judged as a follow-up.
    """

    def __init__(self) -> None:
        self._incidents: dict[tuple[str, str], RingIncident] = {}
        #: (guild, channel) -> (lock, turns holding or waiting on it).
        self._locks: dict[tuple[str, str], tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def channel(self, guild_id: str, channel_id: str) -> AsyncIterator[None]:
        key = (guild_id, channel_id)
        lock, users = self._locks.get(key, (None, 0))
        if lock is None:
            lock = asyncio.Lock()
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users <= 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    def busy(self, guild_id: str, channel_id: str) -> bool:
        return (guild_id, channel_id) in self._locks

    def active(self, guild_id: str, channel_id: str, now: float) -> RingIncident | None:
        self.prune(now)
        return self._incidents.get((guild_id, channel_id))

    def open(self, incident: RingIncident) -> None:
        self._incidents[(incident.guild_id, incident.channel_id)] = incident

    def prune(self, now: float) -> None:
        for key, incident in list(self._incidents.items()):
            if incident.followup_until <= now:
                del self._incidents[key]


class VerdictCache:
    """One scam check per post and its copies.

    scam-banner reviewed a post once (``claim("review:" + hash)``) and removed
    every later copy on the strength of that review. Here the first revision
    of a post asks; a copy within :data:`VERDICT_TTL_SECONDS` waits for that
    answer (bounded by the caller's deadline) instead of asking again.
    """

    def __init__(self) -> None:
        #: (guild, author, post) -> (asked at, its answer, the high-signal
        #: reasons it was asked under, whether it saw only a sample of the
        #: post's images).
        self._entries: dict[
            tuple[str, str, str], tuple[float, asyncio.Future, frozenset[str], bool]
        ] = {}

    def _prune(self, now: float) -> None:
        for key, (at, *_) in list(self._entries.items()):
            if now - at > VERDICT_TTL_SECONDS:
                del self._entries[key]

    def claim(
        self,
        guild_id: str,
        author_id: str,
        key: str,
        now: float | None = None,
        *,
        reasons: Sequence[str] = (),
        replace: bool = False,
        sampled: bool = False,
    ) -> tuple[bool, asyncio.Future]:
        """``(True, future)`` for the first asker, who must settle the future;
        ``(False, future)`` for a copy, which awaits it.

        ``reasons`` are the high-signal reasons the asker was judged (and
        held) under. ``replace`` makes this asker the first again: a copy
        that :meth:`escalates` asks with its stronger reasons, and later
        copies wait for that answer instead. ``sampled``: the asker's check
        sees only a sample of the post's images."""
        now = time.time() if now is None else now
        self._prune(now)
        entry = self._entries.get((guild_id, author_id, key))
        if entry is not None and not replace:
            return False, entry[1]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._entries[(guild_id, author_id, key)] = (
            now,
            future,
            frozenset(reasons),
            sampled,
        )
        return True, future

    def known(
        self, guild_id: str, author_id: str, key: str, now: float | None = None
    ) -> bool:
        """Whether this author's post was judged, or is being judged, lately."""
        now = time.time() if now is None else now
        self._prune(now)
        return (guild_id, author_id, key) in self._entries

    def escalates(
        self,
        guild_id: str,
        author_id: str,
        key: str,
        reasons: Sequence[str],
        now: float | None = None,
    ) -> bool:
        """Whether a copy is asked about again: it brings high-signal reasons
        (a burst, a mass mention with a link) its post was not judged under,
        and the post's check saw only a sample of the images, so its verdict
        does not cover what the copy is held for. A check that saw every
        image already answered the copy's question: the copy waits for it
        (:meth:`holds_while_asked`) instead of spending the author's budget
        on the same question (#376). A post already found to be a violation
        covers every copy: it is removed, not asked about again."""
        now = time.time() if now is None else now
        self._prune(now)
        entry = self._entries.get((guild_id, author_id, key))
        return entry is not None and entry[3] and _adds_to(entry, reasons)

    def holds_while_asked(
        self,
        guild_id: str,
        author_id: str,
        key: str,
        reasons: Sequence[str],
        now: float | None = None,
    ) -> bool:
        """Whether a copy is held while it waits for its post's check: it
        brings high-signal reasons the post was not held under, and that
        check is still out. A check already answered decides the copy at
        once, with nothing to contain meanwhile."""
        now = time.time() if now is None else now
        self._prune(now)
        entry = self._entries.get((guild_id, author_id, key))
        return entry is not None and not entry[1].done() and _adds_to(entry, reasons)

    def asked(self, guild_id: str, author_id: str, key: str) -> asyncio.Future | None:
        """The post's check as it stands (its answer, or the wait for it)."""
        entry = self._entries.get((guild_id, author_id, key))
        return entry[1] if entry is not None else None

    def newer(
        self, guild_id: str, author_id: str, key: str, future: asyncio.Future
    ) -> asyncio.Future | None:
        """The check an escalated copy started after ``future``'s, if any."""
        entry = self._entries.get((guild_id, author_id, key))
        if entry is None or entry[1] is future:
            return None
        return entry[1]

    @staticmethod
    def settle(future: asyncio.Future, verdicts: dict[str, LabelVerdict]) -> None:
        if not future.done():
            future.set_result(verdicts)

    def drop(
        self, guild_id: str, author_id: str, key: str, future: asyncio.Future
    ) -> None:
        """Forget ``future``'s check, which found nothing (a crash, a spent
        budget, a timeout): a later copy asks for itself, held as the post
        was, instead of reading that empty answer."""
        entry = self._entries.get((guild_id, author_id, key))
        if entry is not None and entry[1] is future:
            del self._entries[(guild_id, author_id, key)]


def _adds_to(
    entry: tuple[float, asyncio.Future, frozenset[str], bool], reasons
) -> bool:
    """Whether ``reasons`` go past what ``entry``'s check was asked under,
    and that check did not already find a violation (which covers copies)."""
    if not set(reasons) - entry[2]:
        return False
    future = entry[1]
    if future.done() and not future.cancelled():
        if any(verdict.yes for verdict in future.result().values()):
            return False
    return True


class ScamBursts:
    """A member's scam-gate posts close together are one burst (#376).

    Two different scam posts, each copied to a second channel, were judged
    as two posts: two incidents, two warnings, two DMs. Every post of the
    burst now shares its first message as the burst's anchor: one incident
    (the gateway alias) and one sanction (:class:`ScamSanctions`). A post
    more than :data:`BURST_MAX_SECONDS` after the burst's first one starts a
    new burst, however steadily the member kept posting."""

    def __init__(self) -> None:
        #: (guild, author) -> (the burst's first message, when it was posted).
        self._bursts: dict[tuple[str, str], tuple[str, float]] = {}

    def join(self, guild_id: str, author_id: str, message_id: str, now: float) -> str:
        """The anchor of the burst this post belongs to (its own id when it
        starts one)."""
        entry = self._bursts.get((guild_id, author_id))
        if entry is not None and now - entry[1] <= BURST_MAX_SECONDS:
            return entry[0]
        self._bursts[(guild_id, author_id)] = (message_id, now)
        if len(self._bursts) > MAX_TRACKED_AUTHORS:
            for stale, (_, at) in list(self._bursts.items()):
                if now - at > BURST_MAX_SECONDS:
                    del self._bursts[stale]
        return message_id


class ScamSanctions:
    """One scam sanction per post and its copies (the ``scam:{key}`` claim),
    and per burst (:class:`ScamBursts`).

    An original and its escalated copy are separate messages, so separate
    incidents; both judged a violation would each warn or time out. So
    would two different posts of one burst. The first sanction is recorded
    here and a later one applies only when it is stronger (a warn, then a
    held copy's timeout)."""

    def __init__(self) -> None:
        self._given: dict[tuple[str, str, str], tuple[float, Sanction]] = {}

    def claim(
        self,
        guild_id: str,
        author_id: str,
        key: str,
        sanction: Sanction,
        now: float | None = None,
        *,
        burst: str | None = None,
    ) -> bool:
        """Whether ``sanction`` applies: it is stronger than what the post
        and its burst were already given. True records it as both's."""
        now = time.time() if now is None else now
        for stale, (at, _) in list(self._given.items()):
            if now - at > SANCTION_TTL_SECONDS:
                del self._given[stale]
        keys = [(guild_id, author_id, key)]
        if burst is not None:
            keys.append((guild_id, author_id, f"burst:{burst}"))
        for claim in keys:
            given = self._given.get(claim)
            if given is not None and not sanction.stronger_than(given[1]):
                return False
        for claim in keys:
            self._given[claim] = (now, sanction)
        return True

    def given(
        self, guild_id: str, author_id: str, *, burst: str, now: float | None = None
    ) -> bool:
        """Whether the burst was already given its sanction."""
        now = time.time() if now is None else now
        entry = self._given.get((guild_id, author_id, f"burst:{burst}"))
        return entry is not None and now - entry[0] <= SANCTION_TTL_SECONDS
