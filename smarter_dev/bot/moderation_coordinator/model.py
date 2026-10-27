"""The coordinator's vocabulary: facts in, rule hits, labels, one decision out.

Everything here is plain data. Nothing touches Discord, the database or a
model.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime

from smarter_dev.web.moderation_gateway import types as gw

# ---------------------------------------------------------------------------
# Classifier labels
# ---------------------------------------------------------------------------

#: A clear scam, phishing attempt or malicious crypto/giveaway lure.
SCAM = "scam"
#: Targeted abusive or slur content.
ABUSE = "abuse"
#: The message promotes a specific hackathon, competition or similar event.
EVENT_PROMOTION = "event_promotion"
#: Unsolicited advertising: a newer member's same post in several channels
#: (#407). Jev answers it when it is the only label; with ``scam``/``abuse``
#: or an image in the request, Haiku answers it with the rest.
PROMOTIONAL_SPAM = "promotional_spam"
PARTICIPANT_PREFIX = "participant:"

#: Labels only Claude Haiku answers. Jev serves the ring's validated yes/no
#: questions; when any of these is needed, Haiku answers every label instead.
HAIKU_LABELS = frozenset({SCAM, ABUSE})


def participant_label(user_id: str) -> str:
    return f"{PARTICIPANT_PREFIX}{user_id}"


def participant_of(label: str) -> str | None:
    if label.startswith(PARTICIPANT_PREFIX):
        return label[len(PARTICIPANT_PREFIX) :]
    return None


# How a label came back. Only ANSWERED is acted on.
ANSWERED = "answered"
LOW_CONFIDENCE = "low_confidence"
TIMEOUT = "timeout"
ERROR = "error"
INVALID_OUTPUT = "invalid_output"
BUDGET = "budget"
NOT_ASKED = "not_asked"
#: A "no" given without every image of the message in view: never clean.
UNSEEN_IMAGES = "unseen_images"
UNKNOWN_OUTCOMES = (
    LOW_CONFIDENCE,
    TIMEOUT,
    ERROR,
    INVALID_OUTPUT,
    BUDGET,
    UNSEEN_IMAGES,
)

#: A yes or a no is acted on only this sure (the ring's deployed bar: under
#: Jev's 0.5 threshold it means a probability of at least 0.85).
MIN_CONFIDENCE = 0.7


@dataclass(frozen=True, slots=True)
class LabelVerdict:
    """One label's answer, or why there is none."""

    label: str
    outcome: str
    answer: bool | None = None
    confidence: float | None = None

    @property
    def yes(self) -> bool:
        return self.outcome == ANSWERED and self.answer is True

    @property
    def no(self) -> bool:
        return self.outcome == ANSWERED and self.answer is False

    @property
    def known(self) -> bool:
        return self.outcome == ANSWERED

    def describe(self) -> str:
        if self.outcome in (ANSWERED, LOW_CONFIDENCE) and self.answer is not None:
            text = "yes" if self.answer else "no"
            if self.confidence is not None:
                text += f", confidence {percent(self.confidence)}"
            return text if self.outcome == ANSWERED else f"{text} (too unsure)"
        if self.outcome == UNSEEN_IMAGES:
            return "no, but not every image was seen"
        return self.outcome.replace("_", " ")

    def as_record(self) -> dict:
        return {
            "outcome": self.outcome,
            "answer": self.answer,
            "confidence": None
            if self.confidence is None
            else round(float(self.confidence), 3),
        }


def percent(confidence: float) -> str:
    """A model's confidence as people read it (Zech, Oct 27): "94%", never
    "0.94"."""
    return f"{round(float(confidence) * 100):.0f}%"


def settle(label: str, answer: bool | None, confidence: float | None) -> LabelVerdict:
    """A raw model answer as a verdict: below :data:`MIN_CONFIDENCE` is unknown."""
    if answer is None:
        return LabelVerdict(label, INVALID_OUTPUT)
    if confidence is None or confidence < MIN_CONFIDENCE:
        return LabelVerdict(label, LOW_CONFIDENCE, answer, confidence)
    return LabelVerdict(label, ANSWERED, answer, confidence)


class Verdicts(Mapping[str, LabelVerdict]):
    """Every label's verdict; a label nobody asked about reads as not asked."""

    def __init__(self, verdicts: Mapping[str, LabelVerdict] | None = None) -> None:
        self._verdicts = dict(verdicts or {})

    def __getitem__(self, label: str) -> LabelVerdict:
        return self._verdicts.get(label) or LabelVerdict(label, NOT_ASKED)

    def __iter__(self):
        return iter(self._verdicts)

    def __len__(self) -> int:
        return len(self._verdicts)

    def merged(self, other: Mapping[str, LabelVerdict]) -> Verdicts:
        return Verdicts({**self._verdicts, **dict(other)})


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


#: Names that say an attachment is an image, or a video (which is not).
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
VIDEO_EXTENSIONS = (".mp4", ".mov", ".webm")


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: str | None = None
    size: int | None = None
    url: str | None = None
    #: Discord's dimensions: only images and videos have them.
    width: int | None = None
    height: int | None = None

    @property
    def is_image(self) -> bool:
        """An image by its type, its name, or (not a video) its dimensions.

        The uploader's client sets the type, and can leave it out; an
        unlabelled image still counts. Whether it really is one is for its
        bytes to say (:func:`~.classifier.image_type`).
        """
        content_type = (self.content_type or "").lower()
        name = self.filename.lower()
        if content_type.startswith("image/") or name.endswith(IMAGE_EXTENSIONS):
            return True
        if content_type.startswith("video/") or name.endswith(VIDEO_EXTENSIONS):
            return False
        return self.width is not None or self.height is not None


@dataclass(frozen=True, slots=True)
class MessageFacts:
    """One revision of one guild message, as every rule sees it.

    ``revision`` is ``create`` for a new message and ``edit:<edited at, ms>``
    for an edit that changed the text, so an edit is judged as its own
    revision and a replay of the same revision is not judged twice. ``posted_at`` is when the
    revision arrived (epoch seconds), the clock every window is measured on.
    """

    guild_id: str
    channel_id: str
    message_id: str
    author_id: str
    content: str
    posted_at: float
    account_created_at: datetime
    author_name: str = ""
    #: The member's server nickname or global display name when it differs
    #: from the username, as it was at the message (the report's plain-text
    #: member line, #376).
    author_display_name: str | None = None
    joined_at: datetime | None = None
    attachments: tuple[Attachment, ...] = ()
    revision: str = "create"
    is_edit: bool = False
    author_is_bot: bool = False
    is_webhook: bool = False

    @property
    def images(self) -> tuple[Attachment, ...]:
        return tuple(item for item in self.attachments if item.is_image)


@dataclass(frozen=True, slots=True)
class PastAction:
    """One earlier moderation action on the author (``moderation_actions``)."""

    action_type: str
    created_at: datetime
    #: The moderator's or actor's reason, for the report's history note.
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class AuthorHistory:
    """What scam-banner read about an author before judging them.

    ``is_first_message`` / ``days_since_last_message`` come from
    ``member_activity`` as it stood before this message; ``None`` when that
    could not be told.
    """

    is_first_message: bool | None
    days_since_last_message: int | None
    #: Moderation actions on record, newest first; never a review hold or its
    #: release (they are no action on the member).
    actions: tuple[PastAction, ...] = ()
    #: ``member_activity.first_message_at``; None with no row (never posted)
    #: or when it could not be read.
    first_message_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RecentPost:
    """One of the author's posts in the last two minutes (burst detection)."""

    channel_id: str
    message_id: str
    posted_at: float
    content_hash: str
    #: The spam engine's duplicate digest (whitespace at the ends and case
    #: folded): a near-same copy for the promotional-spam check (#407).
    digest: str = ""


# ---------------------------------------------------------------------------
# What rules propose
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Sanction:
    kind: str  # gw.WARN / gw.TIMEOUT / gw.KICK / gw.BAN
    duration_seconds: int | None = None
    claim: str | None = None

    @property
    def rank(self) -> int:
        return gw.SANCTION_RANK[self.kind]

    def stronger_than(self, other: Sanction | None) -> bool:
        if other is None:
            return True
        if self.rank != other.rank:
            return self.rank > other.rank
        return (self.duration_seconds or 0) > (other.duration_seconds or 0)


@dataclass(frozen=True, slots=True)
class Cleanup:
    channel_id: str
    message_id: str


# Member-facing messages a rule would send (they are not sanctions).
NOTICE_SPAM_WARNING = "spam_warning"
NOTICE_ATTACHMENT_BLOCKED = "attachment_blocked"
NOTICE_ATTACHMENT_WARNED = "attachment_warned"
NOTICE_EDIT_PING = "edit_ping"
NOTICE_SCAM_REMOVED = "scam_removed"
NOTICE_SECURE_ACCOUNT = "secure_account"
NOTICE_MASS_MENTION = "mass_mention"
#: A copy of a post already judged: the post's own decision tells the member
#: (one DM per incident), so the copy sends nothing.
NOTICE_COPY = "copy"


@dataclass(frozen=True, slots=True)
class RuleHit:
    """What one rule found about one target.

    ``model_justified``: the hit exists only because a classifier label said
    yes (a deterministic rule never sets it). ``high_signal``: the author
    gets the provisional timeout while the message is judged.
    """

    rule: str
    reason: str
    target_user_id: str
    sanction: Sanction | None = None
    cleanup: tuple[Cleanup, ...] = ()
    notice: str | None = None
    model_justified: bool = False
    high_signal: bool = False
    target_name: str | None = None


@dataclass(frozen=True, slots=True)
class Provisional:
    """The timeout a high-signal scam gets while it is judged.

    In the gateway it is a timeout with the claim ``provisional:<incident>``
    (a hold, so it never makes the final decision "weaker"). ``resolution``
    is what the decision did with it: ``upgraded`` (a ban or a longer
    timeout replaced it, whether the verdict or a deterministic rule asked
    for it), ``released`` (judged clean and nothing else holds the author:
    the claim is lifted; a moderator's or a longer automated timeout stays),
    ``decided`` (judged a scam, but the sanction is held elsewhere in the
    incident or is no longer than the hold: it stays until it runs out) or
    ``expired`` (no verdict and nothing stronger: it runs out on its own).
    """

    user_id: str
    duration_seconds: int
    reasons: tuple[str, ...]
    resolution: str = "expired"
    claim_prefix: str = "provisional:"
    #: The gateway alias the author's scam burst shares: the hold opens (or
    #: joins) the burst's one incident (#376).
    alias: str | None = None


@dataclass(slots=True)
class TargetDecision:
    """Everything decided about one member."""

    user_id: str
    name: str | None = None
    sanction: Sanction | None = None
    cleanup: list[Cleanup] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    model_justified: bool = False

    @property
    def strongest(self) -> str | None:
        if self.sanction is not None:
            return self.sanction.kind
        if self.cleanup:
            return gw.DELETE
        return None

    def as_record(self) -> dict:
        return {
            "sanction": None
            if self.sanction is None
            else {
                "kind": self.sanction.kind,
                "duration_seconds": self.sanction.duration_seconds,
            },
            "cleanup": [[item.channel_id, item.message_id] for item in self.cleanup],
            "rules": list(dict.fromkeys(self.rules)),
            "reasons": list(dict.fromkeys(self.reasons)),
            "notices": list(dict.fromkeys(self.notices)),
            "model_justified": self.model_justified,
        }


#: Rank for comparing what two systems did to a message: cleanup alone is
#: below every sanction, nothing at all below that.
ACTION_RANK = {None: 0, gw.DELETE: 1, **{k: v + 1 for k, v in gw.SANCTION_RANK.items()}}


@dataclass(slots=True)
class Decision:
    """The coordinator's one decision about one message revision."""

    targets: dict[str, TargetDecision] = field(default_factory=dict)
    provisional: Provisional | None = None
    #: Members a rule proposed acting on whom the staff rule protects.
    staff_excluded: dict[str, str] = field(default_factory=dict)
    #: Lines for moderators: a check that could not be made, a skipped label.
    review: list[str] = field(default_factory=list)
    rules_fired: list[str] = field(default_factory=list)

    @property
    def strongest(self) -> str | None:
        best: str | None = None
        for target in self.targets.values():
            kind = target.strongest
            if ACTION_RANK[kind] > ACTION_RANK[best]:
                best = kind
        return best

    @property
    def acted_on(self) -> list[str]:
        return sorted(
            user_id
            for user_id, target in self.targets.items()
            if target.strongest is not None
        )
