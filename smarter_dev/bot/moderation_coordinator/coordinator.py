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
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
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
from smarter_dev.bot.moderation_coordinator.state import RecentPosts
from smarter_dev.bot.moderation_coordinator.state import RingRegistry
from smarter_dev.bot.moderation_coordinator.state import SpamTracker
from smarter_dev.bot.moderation_coordinator.state import VerdictCache
from smarter_dev.bot.spam_engine import NO_SPAM_ACTION
from smarter_dev.bot.spam_ring import AuthorFacts
from smarter_dev.bot.spam_ring import TranscriptMessage
from smarter_dev.web.handler_recent_messages import content_hash

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.reads = reads
        self.classifier = classifier
        self.spam = spam or SpamTracker()
        self.recent = recent or RecentPosts()
        self.rings = rings or RingRegistry()
        self.verdict_cache = verdicts or VerdictCache()

    async def evaluate(self, facts: MessageFacts, policy: GuildPolicy) -> Evaluation:
        async with contextlib.AsyncExitStack() as stack:
            return await self._evaluate(facts, policy, stack)

    async def _evaluate(
        self, facts: MessageFacts, policy: GuildPolicy, stack: contextlib.AsyncExitStack
    ) -> Evaluation:
        started = time.monotonic()
        now = facts.posted_at
        now_dt = datetime.fromtimestamp(now, UTC)
        extra_roles = policy.staff_exempt_role_ids
        hits: list[RuleHit] = []
        review: list[str] = []

        # 1. Deterministic rules.
        spam_obs = None
        if policy.spam is not None and not facts.is_edit:
            spam_obs = self.spam.observe(facts, policy.spam, now)
            hits += rules.spam_engine_hits(
                facts,
                spam_obs.decision,
                mute_duration_seconds=policy.spam.thresholds.mute_duration_seconds,
            )
        if policy.attachments is not None:
            hits += rules.attachment_hits(facts, policy.attachments)
        if policy.edit_ping:
            hits += rules.edit_ping_hits(facts)
        if policy.bump_channel_ids:
            hits += rules.bump_channel_hits(facts, policy.bump_channel_ids)

        # 2. Rules that need labels, and what they read first.
        key = facts_hash(facts)
        earlier_posts = self.recent.read(facts.guild_id, facts.author_id, now)
        if not facts.is_edit:
            self.recent.record(
                facts.guild_id,
                facts.author_id,
                RecentPost(facts.channel_id, facts.message_id, now, key),
            )
        author_refusal: str | None = None
        refusal_known = False

        async def author_is_staff() -> bool:
            nonlocal author_refusal, refusal_known
            if not refusal_known:
                author_refusal = await self.reads.staff_refusal(
                    facts.guild_id,
                    facts.author_id,
                    is_bot=facts.author_is_bot,
                    extra_role_ids=extra_roles,
                )
                refusal_known = True
            return author_refusal is not None

        scam_review = None
        if (
            policy.scam_gate
            and not facts.is_edit
            and rules.scam_gate_content(facts)
            and not await author_is_staff()
        ):
            history = await self.reads.author_history(
                facts.guild_id, facts.author_id, now_dt
            )
            _, burst = rules.burst_copies(facts, key, earlier_posts)
            suspect = rules.scam_suspect_reasons(facts, burst)
            high_signal = rules.high_signal_reasons(
                facts,
                metrics=spam_obs.metrics if spam_obs is not None else None,
                scam_link_domains=policy.spam.scam_link_domains
                if policy.spam is not None
                else (),
                suspect=suspect,
            )
            scam_review = rules.scam_gate_review(
                facts,
                key=key,
                history=history,
                recent=earlier_posts,
                high_signal=high_signal,
                now=now_dt,
            )

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
            # incident) before a ring message behind it is judged.
            await stack.enter_async_context(
                self.rings.channel(facts.guild_id, facts.channel_id)
            )
            incident = self.rings.active(facts.guild_id, facts.channel_id, now)
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
                rules.ring_may_start(
                    facts, new_member_window=ring.new_member_window, now=now_dt
                )
                and not await author_is_staff()
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
        if scam_review is not None and scam_review.high_signal:
            provisional = Provisional(
                user_id=facts.author_id,
                duration_seconds=PROVISIONAL_SECONDS,
                reasons=scam_review.high_signal,
            )
            provisional_ms = int((time.monotonic() - started) * 1000)

        # 5. One request for the union of labels.
        labels: set[str] = set()
        reused: dict[str, LabelVerdict] = {}
        first_asker = None
        if scam_review is not None:
            first, future = self.verdict_cache.claim(
                facts.guild_id, facts.author_id, key, now
            )
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
            attachments=facts.attachments if SCAM in labels else (),
            ring_context=_ring_context(ring_pending),
            ring_ad=_ring_ad(ring_pending),
            ring_candidates=_ring_candidates(ring_pending),
            charge_author=not isinstance(ring_pending, rules.RingFollowup)
            or bool(labels & {SCAM, ABUSE}),
        )
        try:
            result = await self.classifier.classify(request)
        except BaseException:
            if first_asker is not None:
                # Copies waiting on this check get no answer: unknown.
                VerdictCache.settle(first_asker, {})
            raise
        verdicts = Verdicts(result.verdicts).merged(reused)
        if first_asker is not None and scam_review is not None:
            _share(first_asker, verdicts, scam_review.labels)

        # 6. Resolve and decide.
        scam_outcome = None
        if scam_review is not None:
            scam_hits, scam_outcome, lines = scam_review.resolve(verdicts)
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
        if (
            spam_obs is not None
            and policy.spam is not None
            and spam_obs.decision is not NO_SPAM_ACTION
            and not refusals.get(facts.author_id)
        ):
            self.spam.commit(facts, policy.spam, spam_obs.decision, now)
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
        )

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
            refusals[user_id] = await self.reads.staff_refusal(
                facts.guild_id, user_id, is_bot=False, extra_role_ids=extra_roles
            )
        return refusals


#: The ring's sweep cap (``spam_ring.MAX_SWEEP_MESSAGES``).
MAX_RING_MESSAGES = 100


def _share(future, verdicts: Verdicts, labels: frozenset[str]) -> None:
    """Hand a copy waiting on this post's check the verdicts it needs."""
    if future.done():
        return
    future.set_result({label: verdicts[label] for label in labels})


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
