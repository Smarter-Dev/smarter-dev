"""Pure moderation rules: what fired, on whom, and what it would take.

Every function here takes facts and settings and returns
:class:`~.model.RuleHit` values (or a *pending* rule that needs classifier
labels first). None acts, reports, reads the database or calls a model, so
each can be replayed and tested on plain data.

The rules are the legacy actors' decisions, lifted rather than rewritten:

* **spam engine**: its six metrics and its warn/mute/delete decision are the
  engine's own pure functions (``smarter_dev.bot.spam_engine``);
* **attachment filter**: its tiers (``classify_attachments``);
* **spam ring**: its prefilter, new-member and join-proximity tests and its
  candidate selection (``smarter_dev.bot.spam_ring``), with the deployed
  policy: new for ``spam_ring_new_member_days`` (2), joined within 24 h of the
  ad's author, staff and bots never, a 60 min / 100 message lookback, a yes
  counted at confidence 0.7, follow-ups for ``spam_ring_followup_minutes``;
* **scam gate**: ``scam-banner.monty``'s gates, account-age rule, repeat rule
  and copy cleanup;
* **edit-ping**: ``edit-ping-catch``'s check of an edit's current text;
* **bump channel**: ``disboard-bump-tracker``'s "anything else posted here is
  removed", for members. Disboard's own messages, the reminders and the crown
  stay with the handler: that is upkeep, not moderation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta

from smarter_dev.bot.attachment_filter import classify_attachments
from smarter_dev.bot.moderation_coordinator.model import ABUSE
from smarter_dev.bot.moderation_coordinator.model import EVENT_PROMOTION
from smarter_dev.bot.moderation_coordinator.model import NOTICE_ATTACHMENT_BLOCKED
from smarter_dev.bot.moderation_coordinator.model import NOTICE_ATTACHMENT_WARNED
from smarter_dev.bot.moderation_coordinator.model import NOTICE_EDIT_PING
from smarter_dev.bot.moderation_coordinator.model import NOTICE_SCAM_REMOVED
from smarter_dev.bot.moderation_coordinator.model import NOTICE_SECURE_ACCOUNT
from smarter_dev.bot.moderation_coordinator.model import NOTICE_SPAM_WARNING
from smarter_dev.bot.moderation_coordinator.model import SCAM
from smarter_dev.bot.moderation_coordinator.model import AuthorHistory
from smarter_dev.bot.moderation_coordinator.model import Cleanup
from smarter_dev.bot.moderation_coordinator.model import MessageFacts
from smarter_dev.bot.moderation_coordinator.model import RecentPost
from smarter_dev.bot.moderation_coordinator.model import RuleHit
from smarter_dev.bot.moderation_coordinator.model import Sanction
from smarter_dev.bot.moderation_coordinator.model import Verdicts
from smarter_dev.bot.moderation_coordinator.model import participant_label
from smarter_dev.bot.moderation_coordinator.policy import AttachmentPolicy
from smarter_dev.bot.spam_engine import SPAM_ACTION_MUTE
from smarter_dev.bot.spam_engine import SPAM_ACTION_NONE
from smarter_dev.bot.spam_engine import SPAM_ACTION_WARN
from smarter_dev.bot.spam_engine import SpamDecision
from smarter_dev.bot.spam_engine import SpamMetrics
from smarter_dev.bot.spam_engine import analyze_message_content
from smarter_dev.bot.spam_ring import JOIN_PROXIMITY
from smarter_dev.bot.spam_ring import AuthorFacts
from smarter_dev.bot.spam_ring import TranscriptMessage
from smarter_dev.bot.spam_ring import is_new_member
from smarter_dev.bot.spam_ring import joined_near_participant
from smarter_dev.bot.spam_ring import passes_prefilter
from smarter_dev.bot.spam_ring import select_candidates
from smarter_dev.web.moderation_gateway import types as gw

# Rule names, as decisions and the shadow record name them.
SPAM_ENGINE = "spam_engine"
ATTACHMENT_FILTER = "attachment_filter"
SPAM_RING = "spam_ring"
SCAM_GATE = "scam_gate"
EDIT_PING = "edit_ping"
BUMP_CHANNEL = "bump_channel"

DAY = 86400

# ---------------------------------------------------------------------------
# Spam engine
# ---------------------------------------------------------------------------


def spam_engine_hits(
    facts: MessageFacts,
    decision: SpamDecision,
    *,
    mute_duration_seconds: int,
) -> list[RuleHit]:
    """The engine's decision about one message as rule hits.

    Mute → a timeout of the configured length; warn → a warn (its notice is
    the "Please stop …" message); the delete, when the message in hand carries
    the ping, the scam link or the nitro bait, is cleanup. A warn that comes
    with a delete is still a warn.
    """
    if decision.action == SPAM_ACTION_NONE:
        return []
    reason = ", ".join(decision.reasons)
    cleanup = (
        (Cleanup(facts.channel_id, facts.message_id),)
        if decision.should_delete_message
        else ()
    )
    if decision.action == SPAM_ACTION_MUTE:
        sanction = Sanction(gw.TIMEOUT, duration_seconds=mute_duration_seconds)
        notice = None
    else:
        sanction = Sanction(gw.WARN)
        notice = NOTICE_SPAM_WARNING
    return [
        RuleHit(
            rule=SPAM_ENGINE,
            reason=reason,
            target_user_id=facts.author_id,
            target_name=facts.author_name,
            sanction=sanction,
            cleanup=cleanup,
            notice=notice,
        )
    ]


SPAM_ACTIONS = (SPAM_ACTION_NONE, SPAM_ACTION_WARN, SPAM_ACTION_MUTE)


# ---------------------------------------------------------------------------
# Attachment filter
# ---------------------------------------------------------------------------


_FILE_TYPE = re.compile(r"\.[a-z0-9]{1,10}")


def _shown_type(extension: str) -> str:
    """The file type for a row's reasons. Whatever follows a name's last dot
    can be anything the member wrote (``x.call me at 555-0100``), so only an
    extension-shaped one is kept."""
    if extension == "(no extension)" or _FILE_TYPE.fullmatch(extension):
        return extension
    return "(unusual extension)"


def attachment_hits(facts: MessageFacts, policy: AttachmentPolicy) -> list[RuleHit]:
    """Blocked files: the message is removed and the author told; warn-tier
    files: the author is told. Neither is a sanction."""
    if not facts.attachments or facts.is_edit:
        return []
    warned, blocked = classify_attachments(
        [item.filename for item in facts.attachments],
        ignored_extensions=policy.ignored_extensions,
        warn_extensions=policy.warn_extensions,
    )
    if blocked:
        return [
            RuleHit(
                rule=ATTACHMENT_FILTER,
                # The file type, not its name: a name is member content.
                reason=f"blocked attachment type {_shown_type(blocked[0]['extension'])}",
                target_user_id=facts.author_id,
                target_name=facts.author_name,
                cleanup=(Cleanup(facts.channel_id, facts.message_id),),
                notice=NOTICE_ATTACHMENT_BLOCKED,
            )
        ]
    if warned:
        return [
            RuleHit(
                rule=ATTACHMENT_FILTER,
                reason=f"cautioned attachment type {_shown_type(warned[0]['extension'])}",
                target_user_id=facts.author_id,
                target_name=facts.author_name,
                notice=NOTICE_ATTACHMENT_WARNED,
            )
        ]
    return []


# ---------------------------------------------------------------------------
# Edit-ping and the bump channel
# ---------------------------------------------------------------------------


def edit_ping_hits(facts: MessageFacts) -> list[RuleHit]:
    """An edit whose current text carries ``@everyone``/``@here``: removed.

    The same case-sensitive check the handler makes on the edit's text. Only
    edits: a new message with a mass mention is the spam engine's.
    """
    if not facts.is_edit:
        return []
    if "@everyone" not in facts.content and "@here" not in facts.content:
        return []
    return [
        RuleHit(
            rule=EDIT_PING,
            reason="edited a server-wide mention into a posted message",
            target_user_id=facts.author_id,
            target_name=facts.author_name,
            cleanup=(Cleanup(facts.channel_id, facts.message_id),),
            notice=NOTICE_EDIT_PING,
        )
    ]


def bump_channel_hits(
    facts: MessageFacts, bump_channel_ids: frozenset[str]
) -> list[RuleHit]:
    """A member's message in the Disboard bump channel: removed, nothing more."""
    if facts.is_edit or facts.channel_id not in bump_channel_ids:
        return []
    return [
        RuleHit(
            rule=BUMP_CHANNEL,
            reason="chatter in the bump channel",
            target_user_id=facts.author_id,
            target_name=facts.author_name,
            cleanup=(Cleanup(facts.channel_id, facts.message_id),),
        )
    ]


# ---------------------------------------------------------------------------
# High signal
# ---------------------------------------------------------------------------

#: scam-banner's link test (scheme or www required, unlike the spam engine's).
_SCAM_GATE_LINK = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_RISKY_WORDS = re.compile(
    r"\b(?:crypto|bitcoin|ethereum|eth|wallet|seed phrase|airdrop|giveaway|nitro|claim"
    r"|fag(?:got)?|nigg(?:er|a)|kike|spic|chink|tranny)\b",
    re.IGNORECASE,
)

HIGH_SIGNAL_SCAM_LINK = "scam link"
HIGH_SIGNAL_NITRO_SCAM = "mass mention with nitro bait"
HIGH_SIGNAL_MENTION_LINK = "mass mention with a link"
HIGH_SIGNAL_BURST = "same post in several channels within 10 s"
HIGH_SIGNAL_IMAGES = "four or more images"


def high_signal_reasons(
    facts: MessageFacts,
    *,
    metrics: SpamMetrics | None,
    scam_link_domains: Sequence[str],
    suspect: Sequence[str] = (),
) -> tuple[str, ...]:
    """Why this message is a high-signal scam event, if it is.

    The exact set (Zech's decision: these get an immediate, provisional
    timeout while they are judged):

    1. the spam engine's scam-link metric (a configured domain or a
       ``d*.gift`` lookalike) fired on this message;
    2. its nitro-scam metric fired: a mass mention and a nitro keyword within
       the mass-mention window;
    3. this one message carries ``@everyone``/``@here`` and a link;
    4. scam-banner's "suspect": four or more images, or the same post in
       another channel within 10 s (``suspect`` holds those reasons).

    1–3 need the spam engine on (``metrics``), 4 the scam gate.
    """
    reasons: list[str] = []
    if metrics is not None:
        if metrics.scam_link:
            reasons.append(HIGH_SIGNAL_SCAM_LINK)
        if metrics.nitro_scam:
            reasons.append(HIGH_SIGNAL_NITRO_SCAM)
        analysis = analyze_message_content(facts.content, scam_link_domains)
        if analysis.contains_mass_mention and _SCAM_GATE_LINK.search(facts.content):
            reasons.append(HIGH_SIGNAL_MENTION_LINK)
    reasons.extend(suspect)
    return tuple(dict.fromkeys(reasons))


# ---------------------------------------------------------------------------
# Scam gate (scam-banner.monty)
# ---------------------------------------------------------------------------

#: A repeat inside this window turns a warn into a 24 h timeout.
RECENT_ACTION_SECONDS = 30 * DAY
#: Repeat hold, and the timeout a confirmed high-signal scam gets.
REPEAT_TIMEOUT_SECONDS = DAY
#: Burst: a same-looking post in another channel within this long.
BURST_SECONDS = 10.0
#: Under this account age a confirmed scam is a ban.
BAN_ACCOUNT_AGE = timedelta(days=7)
#: How far back the history scam-banner reads reaches (its 3650 days).
HISTORY_SECONDS = 3650 * DAY
SANCTION_ACTIONS = ("warn", "timeout", "kick", "ban")


def scam_gate_content(facts: MessageFacts) -> bool:
    """scam-banner's first gate: a link, a risky word, or any attachment."""
    return bool(
        _SCAM_GATE_LINK.search(facts.content)
        or _RISKY_WORDS.search(facts.content)
        or facts.attachments
    )


def burst_copies(
    facts: MessageFacts, mine: str, recent: Sequence[RecentPost]
) -> tuple[list[RecentPost], bool]:
    """The author's other posts that look the same, and whether they burst.

    ``mine`` is this message's content hash. A burst is a copy in another
    channel posted within :data:`BURST_SECONDS` of this one.
    """
    copies = [
        post
        for post in recent
        if post.content_hash == mine and post.message_id != facts.message_id
    ]
    burst = any(
        post.channel_id != facts.channel_id
        and abs(post.posted_at - facts.posted_at) <= BURST_SECONDS
        for post in copies
    )
    return copies, burst


def actions_within(history: AuthorHistory, seconds: float, now: datetime) -> list:
    return [
        action
        for action in history.actions
        if action.action_type in SANCTION_ACTIONS
        and (now - action.created_at).total_seconds() < seconds
    ]


def scam_suspect_reasons(facts: MessageFacts, burst: bool) -> tuple[str, ...]:
    reasons = []
    if len(facts.images) >= 4:
        reasons.append(HIGH_SIGNAL_IMAGES)
    if burst:
        reasons.append(HIGH_SIGNAL_BURST)
    return tuple(reasons)


def scam_risky_author(
    facts: MessageFacts, history: AuthorHistory, now: datetime
) -> tuple[str, ...]:
    """scam-banner's author gate: new, first-time, long-silent, recently moderated."""
    reasons = []
    if facts.joined_at is not None and (now - facts.joined_at).total_seconds() < DAY:
        reasons.append("joined in the last day")
    if history.is_first_message:
        reasons.append("first message")
    if (history.days_since_last_message or 0) >= 30:
        reasons.append("silent for 30 days or more")
    if actions_within(history, RECENT_ACTION_SECONDS, now):
        reasons.append("moderated in the last 30 days")
    return tuple(reasons)


@dataclass(frozen=True, slots=True)
class ScamReview:
    """A message the scam gate wants judged (labels ``scam`` and ``abuse``).

    Its copies (the same post elsewhere in the last two minutes) are judged
    with it: a verdict removes every copy, and a later copy reuses the
    verdict instead of asking again.
    """

    facts: MessageFacts
    key: str
    copies: tuple[RecentPost, ...]
    suspect: tuple[str, ...]
    risky_author: tuple[str, ...]
    high_signal: tuple[str, ...]
    account_age: timedelta
    repeat: bool

    labels: frozenset[str] = frozenset({SCAM, ABUSE})

    def resolve(self, verdicts: Verdicts) -> tuple[list[RuleHit], str, list[str]]:
        """The hits, the verdict (``violation``/``clean``/``unknown``) and review lines.

        A violation is a confident yes on either label; clean is a confident
        no on both; anything else is unknown, which proposes nothing (no
        punishment only the model would justify).
        """
        scam, abuse = verdicts[SCAM], verdicts[ABUSE]
        if scam.yes or abuse.yes:
            return self._violation(scam.yes), "violation", []
        if scam.no and abuse.no:
            return [], "clean", []
        unknown = [v for v in (scam, abuse) if not v.known]
        why = ", ".join(f"{v.label}: {v.describe()}" for v in unknown) or "unsure"
        return (
            [],
            "unknown",
            [f"Scam check on <@{self.facts.author_id}> came back unknown ({why})."],
        )

    def _violation(self, is_scam: bool) -> list[RuleHit]:
        facts = self.facts
        reason = "confirmed scam" if is_scam else "confirmed abusive content"
        cleanup = (Cleanup(facts.channel_id, facts.message_id),) + tuple(
            Cleanup(post.channel_id, post.message_id) for post in self.copies
        )
        compromised = bool(self.suspect)
        if self.account_age < BAN_ACCOUNT_AGE:
            sanction = Sanction(gw.BAN)
            reason += ", account under a week old"
            notice = None
        elif self.high_signal:
            # Zech: a high-signal scam held while it was judged ends in a
            # timeout plus cleanup when the account is not new.
            sanction = Sanction(gw.TIMEOUT, duration_seconds=REPEAT_TIMEOUT_SECONDS)
            notice = NOTICE_SECURE_ACCOUNT
        elif self.repeat:
            sanction = Sanction(
                gw.TIMEOUT,
                duration_seconds=REPEAT_TIMEOUT_SECONDS,
                claim=f"repeat:{self.key}",
            )
            reason += ", repeat within 30 days"
            notice = NOTICE_SECURE_ACCOUNT if compromised else NOTICE_SCAM_REMOVED
        else:
            sanction = Sanction(gw.WARN)
            notice = NOTICE_SECURE_ACCOUNT if compromised else NOTICE_SCAM_REMOVED
        return [
            RuleHit(
                rule=SCAM_GATE,
                reason=reason,
                target_user_id=facts.author_id,
                target_name=facts.author_name,
                sanction=sanction,
                cleanup=cleanup,
                notice=notice,
                model_justified=True,
                high_signal=bool(self.high_signal),
            )
        ]


def scam_gate_review(
    facts: MessageFacts,
    *,
    key: str,
    history: AuthorHistory,
    recent: Sequence[RecentPost],
    high_signal: Sequence[str],
    now: datetime,
) -> ScamReview | None:
    """Whether scam-banner would have reviewed this message, and with what.

    A review needs the content gate and one of: a suspect post (four or more
    images, or a cross-channel burst), a risky author, or a high-signal
    event. The last is new: a scam lure from an established account (a
    compromised one) is judged instead of only being muted.
    """
    if facts.is_edit or not scam_gate_content(facts):
        return None
    copies, burst = burst_copies(facts, key, recent)
    suspect = scam_suspect_reasons(facts, burst)
    risky = scam_risky_author(facts, history, now)
    if not (suspect or risky or high_signal):
        return None
    return ScamReview(
        facts=facts,
        key=key,
        copies=tuple(copies),
        suspect=suspect,
        risky_author=risky,
        high_signal=tuple(high_signal),
        account_age=now - facts.account_created_at,
        repeat=bool(actions_within(history, RECENT_ACTION_SECONDS, now)),
    )


# ---------------------------------------------------------------------------
# Spam ring
# ---------------------------------------------------------------------------


def ring_may_start(
    facts: MessageFacts, *, new_member_window: timedelta, now: datetime
) -> bool:
    """The ring's trigger: a new member's message with event wording or a link."""
    if facts.is_edit or facts.is_webhook or facts.author_is_bot:
        return False
    return passes_prefilter(facts.content) and is_new_member(
        facts.joined_at, now, new_member_window
    )


@dataclass(frozen=True, slots=True)
class RingCandidate:
    user_id: str
    username: str
    messages: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RingSweep:
    """A possible ring ad, with the conversation around it already read.

    One request asks whether the ad promotes an event and, for each other
    candidate, whether it took part; the participant answers count only when
    the ad does.
    """

    facts: MessageFacts
    messages: tuple[TranscriptMessage, ...]
    candidates: tuple[RingCandidate, ...]
    context: tuple[str, ...]
    joins: Mapping[str, datetime | None]
    timeout_seconds: int
    history_read: bool = True

    @property
    def labels(self) -> frozenset[str]:
        return frozenset(
            {EVENT_PROMOTION}
            | {participant_label(candidate.user_id) for candidate in self.candidates}
        )

    def resolve(self, verdicts: Verdicts) -> tuple[list[RuleHit], list[str], list[str]]:
        """The hits, the participants and review lines."""
        facts = self.facts
        promotion = verdicts[EVENT_PROMOTION]
        if not promotion.yes:
            review = []
            if not promotion.known:
                review.append(
                    f"Ring check on <@{facts.author_id}>'s message came back "
                    f"unknown ({promotion.describe()})."
                )
            return [], [], review
        participants = [facts.author_id]
        review: list[str] = []
        unjudged = []
        for candidate in self.candidates:
            verdict = verdicts[participant_label(candidate.user_id)]
            if verdict.yes:
                participants.append(candidate.user_id)
            elif not verdict.known:
                unjudged.append(candidate.user_id)
        if unjudged:
            review.append(
                "Ring participant check unknown for "
                + ", ".join(f"<@{user_id}>" for user_id in unjudged)
                + "; not actioned."
            )
        if not self.history_read:
            review.append(
                "The channel history could not be read; only the ad was judged."
            )
        names = {candidate.user_id: candidate.username for candidate in self.candidates}
        names[facts.author_id] = facts.author_name
        hits = []
        for user_id in participants:
            theirs = tuple(
                Cleanup(facts.channel_id, message.message_id)
                for message in self.messages
                if message.author_id == user_id
            )
            if user_id == facts.author_id and not any(
                item.message_id == facts.message_id for item in theirs
            ):
                theirs += (Cleanup(facts.channel_id, facts.message_id),)
            hits.append(
                RuleHit(
                    rule=SPAM_RING,
                    reason="spam ring: staged event promotion",
                    target_user_id=user_id,
                    target_name=names.get(user_id),
                    sanction=Sanction(
                        gw.TIMEOUT, duration_seconds=self.timeout_seconds
                    ),
                    cleanup=theirs,
                    model_justified=True,
                )
            )
        return hits, participants, review


def ring_sweep(
    facts: MessageFacts,
    messages: Sequence[TranscriptMessage],
    authors: Mapping[str, AuthorFacts],
    *,
    new_member_window: timedelta,
    timeout_seconds: int,
    now: datetime,
    history_read: bool = True,
) -> RingSweep:
    """The candidates around a possible ad (``select_candidates``, unchanged)."""
    candidate_ids = select_candidates(
        messages,
        authors,
        ad_author_id=facts.author_id,
        ad_author_joined_at=facts.joined_at,
        now=now,
        new_member_window=new_member_window,
    )

    def name(user_id: str) -> str:
        known = authors.get(user_id)
        return known.username if known else user_id

    candidates = tuple(
        RingCandidate(
            user_id=user_id,
            username=name(user_id),
            messages=tuple(
                message.content or "(no text)"
                for message in messages
                if message.author_id == user_id
            ),
        )
        for user_id in candidate_ids
        if user_id != facts.author_id
    )
    context = tuple(
        f"[{name(message.author_id)}] {message.content or '(no text)'}"
        for message in messages
    )
    joins: dict[str, datetime | None] = {facts.author_id: facts.joined_at}
    for candidate in candidates:
        known = authors.get(candidate.user_id)
        joins[candidate.user_id] = known.effective_join() if known else None
    return RingSweep(
        facts=facts,
        messages=tuple(messages),
        candidates=candidates,
        context=context,
        joins=joins,
        timeout_seconds=timeout_seconds,
        history_read=history_read,
    )


@dataclass(slots=True)
class RingIncident:
    """A ring the coordinator found, kept for its follow-up window."""

    guild_id: str
    channel_id: str
    followup_until: float
    timeout_seconds: int
    participant_joins: list[datetime]
    participants: set[str]
    ad_text: str
    context: list[str]
    spared: set[str] = field(default_factory=set)
    handled_message_ids: set[str] = field(default_factory=set)


def ring_followup_applies(
    facts: MessageFacts,
    incident: RingIncident,
    *,
    new_member_window: timedelta,
    now: datetime,
) -> bool:
    """A newcomer near a participant's join posting during the follow-up."""
    if facts.is_edit or facts.is_webhook or facts.author_is_bot:
        return False
    if facts.message_id in incident.handled_message_ids:
        return False
    if facts.author_id in incident.spared:
        return False
    if not is_new_member(facts.joined_at, now, new_member_window):
        return False
    if facts.author_id in incident.participants:
        return True
    return joined_near_participant(
        facts.joined_at, incident.participant_joins, JOIN_PROXIMITY
    )


@dataclass(frozen=True, slots=True)
class RingFollowup:
    """One follow-up author. A participant already timed out needs no label."""

    facts: MessageFacts
    incident: RingIncident
    known_participant: bool

    @property
    def labels(self) -> frozenset[str]:
        if self.known_participant:
            return frozenset()
        return frozenset({participant_label(self.facts.author_id)})

    def resolve(self, verdicts: Verdicts) -> tuple[list[RuleHit], bool, list[str]]:
        """The hits, whether the author is a participant, review lines."""
        facts = self.facts
        if not self.known_participant:
            verdict = verdicts[participant_label(facts.author_id)]
            if not verdict.yes:
                return (
                    [],
                    False,
                    [
                        f"Ring follow-up by <@{facts.author_id}> not actioned "
                        f"(participant check: {verdict.describe()}); review."
                    ],
                )
        return (
            [
                RuleHit(
                    rule=SPAM_RING,
                    reason="spam ring follow-up",
                    target_user_id=facts.author_id,
                    target_name=facts.author_name,
                    sanction=Sanction(
                        gw.TIMEOUT, duration_seconds=self.incident.timeout_seconds
                    ),
                    cleanup=(Cleanup(facts.channel_id, facts.message_id),),
                    model_justified=not self.known_participant,
                )
            ],
            True,
            [],
        )
