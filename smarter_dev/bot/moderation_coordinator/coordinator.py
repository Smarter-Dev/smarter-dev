"""The coordinator: facts → rules → at most one classifier request → one decision.

:meth:`Coordinator.evaluate` is the whole pipeline for one message revision.
It reads only through :class:`CoordinatorReads` (the staff rule, the author's
history, a channel's recent messages) and never through anything that can
change Discord: it returns an :class:`Evaluation`, and what is done with it is
the caller's business (the shadow records it; the cutover will hand it to the
moderation gateway).

Order, and why:

1. Deterministic rules first (spam engine, attachments, edit-ping, bump
   channel). They cost nothing and never wait for a model.
2. The scam gate and the ring decide whether a label is needed, reading what
   they need *before* the request (the author's history, the channel window),
   so the ring's promotion and participant questions go in the same request.
3. The author's staff standing is asked once a rule fired or a label is
   wanted; a protected author is never sent to the model.
4. A high-signal scam gets its provisional timeout here, before the request.
5. One request for the union of labels (:mod:`.classifier`).
6. Model-dependent rules resolve against the verdicts; :func:`.decide.decide`
   makes the one decision.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import Protocol

from smarter_dev.bot.moderation_coordinator import rules
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierRequest
from smarter_dev.bot.moderation_coordinator.classifier import ClassifierResult
from smarter_dev.bot.moderation_coordinator.classifier import ModerationClassifier
from smarter_dev.bot.moderation_coordinator.decide import PROVISIONAL_SECONDS
from smarter_dev.bot.moderation_coordinator.decide import decide
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import TIMEOUT
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import Decision
from smarter_dev.bot.moderation_coordinator.model import LabelVerdict
from smarter_dev.bot.moderation_coordinator.model import MessageFacts
from smarter_dev.bot.moderation_coordinator.model import Provisional
from smarter_dev.bot.moderation_coordinator.model import RecentPost
from smarter_dev.bot.moderation_coordinator.model import RuleHit
from smarter_dev.bot.moderation_coordinator.model import Verdicts
from smarter_dev.bot.moderation_coordinator.policy import GuildPolicy
from smarter_dev.bot.moderation_coordinator.policy import in_scope
from smarter_dev.bot.moderation_coordinator.state import RecentPosts
from smarter_dev.bot.moderation_coordinator.state import RingRegistry
from smarter_dev.bot.moderation_coordinator.state import ScamSanctions
from smarter_dev.bot.moderation_coordinator.state import SpamTracker
from smarter_dev.bot.moderation_coordinator.state import VerdictCache
from smarter_dev.bot.spam_engine import NO_SPAM_ACTION
from smarter_dev.bot.spam_ring import AuthorFacts
from smarter_dev.bot.spam_ring import TranscriptMessage
from smarter_dev.web.handler_recent_messages import content_hash

logger = logging.getLogger(__name__)

#: Every wait in a decision is bounded, so a decision is made once and soon
#: (#240): the staff check (unanswered refuses, as an unknown standing always
#: does), the author's history (unread is an empty history), and the wait for
#: a ring channel whose ad is being judged (one ad's history read, staff
#: checks and classifier deadline fit inside it).
STAFF_CHECK_SECONDS = 5.0
HISTORY_READ_SECONDS = 5.0
RING_LOCK_SECONDS = 20.0
STAFF_CHECK_TIMED_OUT = "the staff check did not answer in time"
STAFF_CHECK_FAILED = "the staff check failed"


class CoordinatorReads(Protocol):
    """What the coordinator may read. Nothing here can change Discord."""

    async def staff_refusal(
        self,
        guild_id: str,
        user_id: str,
        *,
        is_bot: bool | None,
        extra_role_ids: tuple[str, ...],
    ) -> str | None:
        """The shared staff-protection rule; never raises (unknown refuses)."""
        ...

    async def author_history(
        self, guild_id: str, user_id: str, before: datetime
    ) -> AuthorHistory: ...

    async def channel_window(
        self,
        guild_id: str,
        channel_id: str,
        since: datetime,
        limit: int,
        extra_role_ids: tuple[str, ...] = (),
    ) -> tuple[list[TranscriptMessage], dict[str, AuthorFacts]] | None:
        """The channel's messages since ``since`` (oldest first, capped) and
        their authors; None when the history could not be read."""
        ...


@dataclass(slots=True)
class Evaluation:
    """Everything the coordinator concluded about one revision."""

    facts: MessageFacts
    policy: GuildPolicy
    decision: Decision
    classifier: ClassifierResult
    verdicts: dict[str, LabelVerdict] = field(default_factory=dict)
    labels_wanted: list[str] = field(default_factory=list)
    scam_outcome: str | None = None
    #: Labels answered from an earlier copy's request, not a new one.
    reused_labels: list[str] = field(default_factory=list)
    #: Milliseconds from intake to the provisional timeout, and to the decision.
    provisional_ms: int | None = None
    latency_ms: int = 0
    #: The post's content hash (the scam gate's key for its copies).
    key: str = ""
    #: The author's history, when the scam gate read it (the report's note).
    history: AuthorHistory | None = None
    #: A ring's conversation, for the report's transcript, and the alias its
    #: ad and follow-ups share in the gateway.
    ring_context: tuple[str, ...] = ()
    ring_alias: str | None = None
    #: When the deterministic rules were already carried out (before any
    #: classifier wait), what is left to carry out; ``decision`` stays the
    #: whole decision, for the record.
    remaining: Decision | None = None

    @property
    def to_carry_out(self) -> Decision:
        return self.decision if self.remaining is None else self.remaining


class _Slot:
    """A concurrency slot that can be put down while waiting on something
    else (a ring channel's lock), so lock waiters never hold every slot."""

    def __init__(self, semaphore: asyncio.Semaphore | None) -> None:
        self._semaphore = semaphore
        self._held = False

    async def take(self) -> None:
        if self._semaphore is not None and not self._held:
            await self._semaphore.acquire()
            self._held = True

    def give(self) -> None:
        if self._semaphore is not None and self._held:
            self._semaphore.release()
            self._held = False


def facts_hash(facts: MessageFacts) -> str:
    """The same content hash the handler dispatch keeps for scam-banner."""
    return content_hash(
        {
            "author_id": facts.author_id,
            "message_content": facts.content,
            "attachments": [
                {"filename": item.filename, "size": item.size}
                for item in facts.attachments
            ],
        }
    )


class Coordinator:
    """One process's coordinator: its own state, one classifier."""

    def __init__(
        self,
        reads: CoordinatorReads,
        classifier: ModerationClassifier,
        *,
        spam: SpamTracker | None = None,
        recent: RecentPosts | None = None,
        rings: RingRegistry | None = None,
        verdicts: VerdictCache | None = None,
        sanctions: ScamSanctions | None = None,
    ) -> None:
        self.reads = reads
        self.classifier = classifier
        self.spam = spam or SpamTracker()
        self.recent = recent or RecentPosts()
        self.rings = rings or RingRegistry()
        self.verdict_cache = verdicts or VerdictCache()
        self.sanctions = sanctions or ScamSanctions()

    async def evaluate(
        self,
        facts: MessageFacts,
        policy: GuildPolicy,
        *,
        slot: asyncio.Semaphore | None = None,
        on_provisional: Callable[[MessageFacts, Provisional, str], Awaitable[None]]
        | None = None,
        on_deterministic: Callable[[MessageFacts, Decision, str], Awaitable[None]]
        | None = None,
        judge_labels: bool = True,
    ) -> Evaluation:
        """Judge one revision.

        The deterministic rules (spam engine, attachment filter, edit-ping,
        mass mention, bump channel) run first and hold no slot: under load
        they are never queued behind classifier requests. When the
        coordinator acts, ``on_deterministic`` carries their decision out
        right away, before anything waits on the classifier or a ring lock.

        ``slot`` then bounds how many label checks run at once; it is held
        except while waiting on a busy ring channel. ``on_provisional`` places
        the provisional timeout (with the post's key) before the classifier is
        asked. ``judge_labels=False`` (a backlog past the queue) judges the
        deterministic rules only and never takes a slot.
        """
        held = _Slot(slot)
        async with contextlib.AsyncExitStack() as stack:
            stack.callback(held.give)
            return await self._evaluate(
                facts,
                policy,
                stack,
                held,
                on_provisional,
                on_deterministic=on_deterministic,
                judge_labels=judge_labels,
            )

    async def _staff_refusal(
        self,
        guild_id: str,
        user_id: str,
        *,
        is_bot: bool | None,
        extra_role_ids: tuple[str, ...],
    ) -> str:
        try:
            return await asyncio.wait_for(
                self.reads.staff_refusal(
                    guild_id, user_id, is_bot=is_bot, extra_role_ids=extra_role_ids
                ),
                STAFF_CHECK_SECONDS,
            )
        except TimeoutError:
            logger.warning("Coordinator staff check on %s timed out", user_id)
            return STAFF_CHECK_TIMED_OUT
        except Exception:  # noqa: BLE001 — an unknown standing refuses
            logger.warning("Coordinator staff check on %s failed", user_id)
            return STAFF_CHECK_FAILED

    async def _author_history(
        self,
        facts: MessageFacts,
        before: datetime,
        review: list[str],
        *,
        actions_before: float,
    ) -> AuthorHistory:
        """The author's history; only actions taken before ``actions_before``
        (the first of the post and its copies) count as earlier ones."""
        try:
            history = await asyncio.wait_for(
                self.reads.author_history(facts.guild_id, facts.author_id, before),
                HISTORY_READ_SECONDS,
            )
        except Exception as error:  # noqa: BLE001 — judged without it
            logger.warning(
                "Coordinator could not read %s's history (%s)",
                facts.author_id,
                type(error).__name__,
            )
            review.append(
                f"<@{facts.author_id}>'s history could not be read; judged as if "
                "they had none (not new, not silent, no earlier actions)."
            )
            return AuthorHistory(is_first_message=None, days_since_last_message=None)
        cutoff = datetime.fromtimestamp(actions_before, UTC)
        return replace(
            history,
            actions=tuple(a for a in history.actions if a.created_at < cutoff),
        )

    async def _evaluate(
        self,
        facts: MessageFacts,
        policy: GuildPolicy,
        stack: contextlib.AsyncExitStack,
        held: _Slot,
        on_provisional,
        *,
        on_deterministic=None,
        judge_labels: bool = True,
    ) -> Evaluation:
        started = time.monotonic()
        now = facts.posted_at
        now_dt = datetime.fromtimestamp(now, UTC)
        extra_roles = policy.staff_exempt_role_ids
        hits: list[RuleHit] = []
        review: list[str] = []

        author_refusal: str | None = None
        refusal_known = False

        async def author_is_staff() -> bool:
            nonlocal author_refusal, refusal_known
            if not refusal_known:
                author_refusal = await self._staff_refusal(
                    facts.guild_id,
                    facts.author_id,
                    is_bot=facts.author_is_bot,
                    extra_role_ids=extra_roles,
                )
                refusal_known = True
            return author_refusal is not None

        # 1. Deterministic rules.
        spam_obs = None
        if policy.spam is not None and not facts.is_edit:
            spam_obs = self.spam.observe(
                facts, policy.spam, now, mass_mention_rule=policy.mass_mention
            )
            hits += rules.spam_engine_hits(
                facts,
                spam_obs.decision,
                mute_duration_seconds=policy.spam.thresholds.mute_duration_seconds,
            )
            # Armed now, as the engine arms it once it acted, not after a
            # classifier wait: the author's next messages in a burst must
            # already see the mute (or the warning).
            if spam_obs.decision is not NO_SPAM_ACTION and not await author_is_staff():
                self.spam.commit(facts, policy.spam, spam_obs.decision, now)
        if policy.attachments is not None:
            hits += rules.attachment_hits(facts, policy.attachments)
        if policy.edit_ping and in_scope(
            policy.edit_ping_channel_ids, facts.channel_id
        ):
            hits += rules.edit_ping_hits(facts)
        if policy.bump_channel_ids:
            hits += rules.bump_channel_hits(facts, policy.bump_channel_ids)
        if policy.mass_mention:
            hits += rules.mass_mention_hits(facts)

        key = facts_hash(facts)
        early_hits = 0
        preloaded = None
        scam_candidate = (
            policy.scam_gate
            and in_scope(policy.scam_gate_channel_ids, facts.channel_id)
            and not facts.is_edit
            and rules.scam_gate_content(facts)
        )
        if on_deterministic is not None and hits:
            # Carried out now, not after a classifier wait: a raid's spam
            # mutes and removals land while the label checks queue.
            await author_is_staff()
            early_hits = len(hits)
            early = decide(
                hits,
                refusals=await self._refusals(
                    facts, hits, author_refusal, refusal_known, extra_roles
                ),
            )
            if (
                judge_labels
                and scam_candidate
                and facts.images
                and author_refusal is None
                and _deletes(early, facts.message_id)
            ):
                # The CDN stops serving a deleted message's files: the scam
                # check's images are loaded before the removal, or it is blind.
                preloaded = await self.classifier.preload(facts.attachments)
            await on_deterministic(facts, early, key)
        earlier_posts = self.recent.read(facts.guild_id, facts.author_id, now)
        if not facts.is_edit:
            self.recent.record(
                facts.guild_id,
                facts.author_id,
                RecentPost(facts.channel_id, facts.message_id, now, key),
            )
        if not judge_labels:
            # Past the queue: the deterministic rules only, and no slot taken
            # (a raid must not fill the backlog waiting for one).
            if hits and not refusal_known:
                await author_is_staff()
            decision = decide(
                hits,
                refusals=await self._refusals(
                    facts, hits, author_refusal, refusal_known, extra_roles
                ),
                review=review,
            )
            return Evaluation(
                facts=facts,
                policy=policy,
                decision=decision,
                classifier=ClassifierResult(),
                latency_ms=int((time.monotonic() - started) * 1000),
                key=key,
                remaining=decide([], refusals={}) if early_hits else None,
            )
        await held.take()

        # 2. Rules that need labels, and what they read first.
        scam_review = None
        history = None
        known_copy = False
        escalated = False
        if scam_candidate and not await author_is_staff():
            copies, burst = rules.burst_copies(facts, key, earlier_posts)
            # The post's own actions (its early mass-mention warning, and a
            # copy's post's) are this incident, not an earlier one.
            first_posted = min([now, *(post.posted_at for post in copies)])
            history = await self._author_history(
                facts, now_dt, review, actions_before=first_posted
            )
            known_copy = self.verdict_cache.known(
                facts.guild_id, facts.author_id, key, now
            )
            suspect = rules.scam_suspect_reasons(facts, burst)
            high_signal = rules.high_signal_reasons(
                facts,
                metrics=spam_obs.metrics if spam_obs is not None else None,
                suspect=rules.hold_reasons(
                    suspect, rules.scam_risky_author(facts, history, now_dt)
                ),
            )
            # A copy with a burst or other high-signal reason its post was not
            # judged under (an established member's 4+ images: no hold, one
            # sampled image) is held and asked again with every image.
            escalated = known_copy and self.verdict_cache.escalates(
                facts.guild_id, facts.author_id, key, high_signal, now
            )
            known_copy = known_copy and not escalated
            scam_review = rules.scam_gate_review(
                facts,
                key=key,
                history=history,
                recent=earlier_posts,
                high_signal=high_signal,
                now=now_dt,
                known_copy=known_copy,
            )
        claimed = None
        if scam_review is not None:
            # Claimed here, with nothing awaited since ``known`` and
            # ``escalates`` read the cache: a post and its copy cannot both
            # miss each other, and a post decided later sees its escalated
            # copy's check (the one-sanction wait, re-review item 2).
            claimed = self.verdict_cache.claim(
                facts.guild_id,
                facts.author_id,
                key,
                now,
                reasons=scam_review.high_signal,
                replace=escalated,
            )
            if claimed[0]:
                # Copies waiting on this check get no answer if it never runs.
                stack.callback(self._settle, facts, key, claimed[1], {})

        ring_pending = None
        ring = policy.ring
        if (
            ring is not None
            and not facts.is_edit
            and not facts.author_is_bot
            and (
                self.rings.busy(facts.guild_id, facts.channel_id)
                or self.rings.active(facts.guild_id, facts.channel_id, now) is not None
                or rules.ring_may_start(
                    facts, new_member_window=ring.new_member_window, now=now_dt
                )
            )
        ):
            # Held to the end: the ad's sweep finishes (and opens its
            # incident) before a ring message behind it is judged. The wait
            # is bounded and holds no concurrency slot: a busy ring channel
            # never stalls the other channels.
            held.give()
            locked = False
            try:
                async with asyncio.timeout(RING_LOCK_SECONDS):
                    await stack.enter_async_context(
                        self.rings.channel(facts.guild_id, facts.channel_id)
                    )
                locked = True
            except TimeoutError:
                review.append(
                    f"Ring check on <@{facts.author_id}>'s message skipped: the "
                    f"channel's ring check was still running after "
                    f"{RING_LOCK_SECONDS:.0f} s."
                )
            finally:
                await held.take()
            incident = (
                self.rings.active(facts.guild_id, facts.channel_id, now)
                if locked
                else None
            )
            if incident is not None and rules.ring_followup_applies(
                facts, incident, new_member_window=ring.new_member_window, now=now_dt
            ):
                if not await author_is_staff():
                    ring_pending = rules.RingFollowup(
                        facts=facts,
                        incident=incident,
                        known_participant=facts.author_id in incident.participants,
                    )
            elif (
                locked
                and rules.ring_may_start(
                    facts, new_member_window=ring.new_member_window, now=now_dt
                )
                and not await author_is_staff()
            ):
                # The history is only read for a request the budget would
                # take (as the ring checks its budget first): past it, the
                # request is refused anyway and the read would be wasted.
                window = None
                if (
                    self.classifier.budget_refusal(
                        facts.guild_id, facts.author_id, acting=policy.acting
                    )
                    is None
                ):
                    window = await self.reads.channel_window(
                        facts.guild_id,
                        facts.channel_id,
                        now_dt - ring.lookback,
                        MAX_RING_MESSAGES,
                        extra_roles,
                    )
                messages, authors = window if window is not None else ([], {})
                if not any(m.message_id == facts.message_id for m in messages):
                    messages.append(
                        TranscriptMessage(
                            message_id=facts.message_id,
                            author_id=facts.author_id,
                            timestamp=now_dt,
                            content=facts.content,
                        )
                    )
                ring_pending = rules.ring_sweep(
                    facts,
                    messages,
                    authors,
                    new_member_window=ring.new_member_window,
                    timeout_seconds=ring.timeout_seconds,
                    now=now_dt,
                    history_read=window is not None,
                )

        # 3. The author's standing, once anything wants to act or ask.
        if hits and not refusal_known:
            await author_is_staff()

        # 4. Provisional containment for a high-signal scam under review.
        provisional = None
        provisional_ms = None
        # A copy of a post already judged (or being judged) is covered by
        # that post's hold and verdict: it is not held again.
        if scam_review is not None and scam_review.high_signal and not known_copy:
            provisional = Provisional(
                user_id=facts.author_id,
                duration_seconds=PROVISIONAL_SECONDS,
                reasons=scam_review.high_signal,
            )
            if on_provisional is not None:
                # Contain first: the verdict is up to 10 s away.
                await on_provisional(facts, provisional, key)
            provisional_ms = int((time.monotonic() - started) * 1000)

        # 5. One request for the union of labels.
        labels: set[str] = set()
        reused: dict[str, LabelVerdict] = {}
        first_asker = None
        if scam_review is not None and claimed is not None:
            first, future = claimed
            if first:
                first_asker = future
                labels |= scam_review.labels
            else:
                try:
                    reused = await asyncio.wait_for(
                        asyncio.shield(future), self.classifier.deadline
                    )
                except TimeoutError:
                    reused = {
                        label: LabelVerdict(label, TIMEOUT)
                        for label in scam_review.labels
                    }
        if ring_pending is not None:
            labels |= ring_pending.labels
        request = ClassifierRequest(
            guild_id=facts.guild_id,
            channel_id=facts.channel_id,
            author_id=facts.author_id,
            labels=frozenset(labels),
            content=facts.content,
            # Images go with any question: a message with one is judged by
            # Haiku, which sees them (Jev cannot).
            attachments=facts.attachments,
            ring_context=_ring_context(ring_pending),
            ring_ad=_ring_ad(ring_pending),
            ring_candidates=_ring_candidates(ring_pending),
            charge_author=not isinstance(ring_pending, rules.RingFollowup)
            or bool(labels & {SCAM, ABUSE}),
            sampled_images=_image_sample(facts, scam_review),
            acting=policy.acting,
            preloaded_images=preloaded,
        )
        try:
            result = await self.classifier.classify(request)
        except BaseException:
            if first_asker is not None:
                # Copies waiting on this check get no answer: unknown.
                self._settle(facts, key, first_asker, {})
            raise
        verdicts = Verdicts(result.verdicts).merged(reused)
        if first_asker is not None and scam_review is not None:
            self._settle(
                facts,
                key,
                first_asker,
                {label: verdicts[label] for label in scam_review.labels},
            )

        # 6. Resolve and decide.
        scam_outcome = None
        if scam_review is not None:
            # Read the copies again: the verdict outlasts a burst, so a copy
            # posted while it was asked is removed with the rest (as
            # scam-banner re-read them after its review). The same author's
            # same post only.
            later, _ = rules.burst_copies(
                facts,
                key,
                self.recent.read(facts.guild_id, facts.author_id, now),
            )
            known = {post.message_id for post in scam_review.copies}
            scam_review = replace(
                scam_review,
                copies=scam_review.copies
                + tuple(post for post in later if post.message_id not in known),
            )
            scam_hits, scam_outcome, lines = scam_review.resolve(verdicts)
            scam_hits = await self._one_sanction(
                facts, key, scam_hits, first_asker, now, held
            )
            hits += scam_hits
            review += lines
        if isinstance(ring_pending, rules.RingSweep):
            ring_hits, participants, lines = ring_pending.resolve(verdicts)
            hits += ring_hits
            review += lines
            if participants:
                self.rings.open(
                    rules.RingIncident(
                        guild_id=facts.guild_id,
                        channel_id=facts.channel_id,
                        followup_until=now + ring.followup.total_seconds(),
                        timeout_seconds=ring.timeout_seconds,
                        participant_joins=[
                            joined
                            for user_id in participants
                            if (joined := ring_pending.joins.get(user_id)) is not None
                        ],
                        participants=set(participants),
                        ad_text=facts.content,
                        context=list(ring_pending.context),
                        spared={
                            c.user_id
                            for c in ring_pending.candidates
                            if c.user_id not in participants
                        },
                        handled_message_ids={
                            m.message_id
                            for m in ring_pending.messages
                            if m.author_id in participants
                        },
                        ad_message_id=facts.message_id,
                    )
                )
        elif isinstance(ring_pending, rules.RingFollowup):
            ring_hits, joined, lines = ring_pending.resolve(verdicts)
            hits += ring_hits
            review += lines
            incident = ring_pending.incident
            incident.handled_message_ids.add(facts.message_id)
            incident.context.append(
                f"[{facts.author_name or facts.author_id}] {facts.content or '(no text)'}"
            )
            if joined:
                incident.participants.add(facts.author_id)
            else:
                incident.spared.add(facts.author_id)

        ring_context: tuple[str, ...] = ()
        ring_alias = None
        if isinstance(ring_pending, rules.RingSweep) and any(
            hit.rule == rules.SPAM_RING for hit in hits
        ):
            ring_context = ring_pending.context
            ring_alias = _ring_alias(facts.guild_id, facts.channel_id, facts.message_id)
        elif isinstance(ring_pending, rules.RingFollowup):
            ring_context = tuple(ring_pending.incident.context)
            ring_alias = _ring_alias(
                facts.guild_id, facts.channel_id, ring_pending.incident.ad_message_id
            )

        refusals = await self._refusals(
            facts, hits, author_refusal, refusal_known, extra_roles
        )
        decision = decide(
            hits,
            refusals=refusals,
            provisional=provisional,
            scam_outcome=scam_outcome,
            review=review,
        )
        remaining = (
            decide(
                hits[early_hits:],
                refusals=refusals,
                provisional=provisional,
                scam_outcome=scam_outcome,
                review=review,
            )
            if early_hits
            else None
        )
        return Evaluation(
            facts=facts,
            policy=policy,
            decision=decision,
            classifier=result,
            verdicts=dict(verdicts),
            labels_wanted=sorted(labels | set(reused)),
            scam_outcome=scam_outcome,
            reused_labels=sorted(reused),
            provisional_ms=provisional_ms,
            latency_ms=int((time.monotonic() - started) * 1000),
            key=key,
            history=history,
            ring_context=ring_context,
            ring_alias=ring_alias,
            remaining=remaining,
        )

    def _settle(
        self,
        facts: MessageFacts,
        key: str,
        future: asyncio.Future,
        verdicts: dict[str, LabelVerdict],
    ) -> None:
        """Hand copies waiting on this post's check the verdicts they need.

        A check that found nothing (a crash, a spent budget, a timeout) is
        forgotten: a later copy asks for itself rather than being judged
        on no answer (#310 round 3, item 3)."""
        VerdictCache.settle(future, verdicts)
        if future.cancelled() or not any(
            verdict.answer is not None for verdict in future.result().values()
        ):
            self.verdict_cache.drop(facts.guild_id, facts.author_id, key, future)

    async def _one_sanction(
        self,
        facts: MessageFacts,
        key: str,
        scam_hits: list[RuleHit],
        asked: asyncio.Future | None,
        now: float,
        held: _Slot,
    ) -> list[RuleHit]:
        """The ``scam:{key}`` claim: a post and its copies are sanctioned once,
        with the strongest sanction (#310 re-review item 2).

        A post whose escalated copy is still being checked waits for that
        check, so the post's weaker sanction is not given first: a copy found
        a violation claims its own sanction as soon as its verdict is shared,
        and the post's then applies only when stronger. The post claims
        whatever the copy did: a copy that fails before claiming leaves the
        sanction to the post (#310 round 3, item 1)."""
        sanctions = [hit.sanction for hit in scam_hits if hit.sanction is not None]
        if not sanctions:
            return scam_hits
        newer = (
            self.verdict_cache.newer(facts.guild_id, facts.author_id, key, asked)
            if asked is not None
            else None
        )
        if newer is not None:
            # The post's own check is done: the wait holds no slot.
            held.give()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(newer), self.classifier.deadline)
        if self.sanctions.claim(
            facts.guild_id, facts.author_id, key, sanctions[0], now
        ):
            return scam_hits
        return rules.without_sanction(scam_hits, ", already sanctioned for this post")

    async def _refusals(
        self,
        facts: MessageFacts,
        hits: list[RuleHit],
        author_refusal: str | None,
        refusal_known: bool,
        extra_roles: tuple[str, ...],
    ) -> Mapping[str, str | None]:
        """Every target's staff standing; the author's is already known."""
        refusals: dict[str, str | None] = {}
        if refusal_known:
            refusals[facts.author_id] = author_refusal
        for hit in hits:
            user_id = hit.target_user_id
            if user_id in refusals:
                continue
            refusals[user_id] = await self._staff_refusal(
                facts.guild_id, user_id, is_bot=False, extra_role_ids=extra_roles
            )
        return refusals


#: The ring's sweep cap (``spam_ring.MAX_SWEEP_MESSAGES``).
MAX_RING_MESSAGES = 100


#: Images an established member's unheld image post is checked on.
SAMPLED_IMAGES = 1


def _image_sample(facts: MessageFacts, review: rules.ScamReview | None) -> int | None:
    """Zech, Oct 23: when a member who is not high risk posts four or more
    images and is not held, the review sees one of them at random. A
    high-risk author, a held post and everything else show up to four."""
    if (
        review is None
        or len(facts.images) < 4
        or review.risky_author
        or review.high_signal
    ):
        return None
    return SAMPLED_IMAGES


def _deletes(decision, message_id: str) -> bool:
    """Whether ``decision`` removes this message."""
    return any(
        item.message_id == message_id
        for target in decision.targets.values()
        for item in target.cleanup
    )


def _ring_alias(guild_id: str, channel_id: str, ad_message_id: str) -> str:
    """One gateway incident for a ring's ad and its follow-ups."""
    return f"coordinator_ring:{guild_id}:{channel_id}:{ad_message_id}"


def _ring_context(pending) -> tuple[str, ...]:
    if isinstance(pending, rules.RingSweep):
        return pending.context
    if isinstance(pending, rules.RingFollowup):
        return tuple(pending.incident.context)
    return ()


def _ring_ad(pending) -> str | None:
    if isinstance(pending, rules.RingFollowup):
        return pending.incident.ad_text
    return None


def _ring_candidates(pending) -> tuple[rules.RingCandidate, ...]:
    if isinstance(pending, rules.RingSweep):
        return pending.candidates
    if isinstance(pending, rules.RingFollowup) and not pending.known_participant:
        facts = pending.facts
        return (
            rules.RingCandidate(
                user_id=facts.author_id,
                username=facts.author_name or facts.author_id,
                messages=(facts.content or "(no text)",),
            ),
        )
    return ()
