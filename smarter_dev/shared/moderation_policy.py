"""One versioned moderation policy per guild (#241).

Everything the moderation coordinator and the staff-ping triage are set up
with lives in one document (``moderation_policies.document``), saved as a new
revision on every change. The admin page, the API and the import all go
through :func:`validate`, so the document a process loads is always one this
module accepted.

What a guild may change is here. What it may not is enforced in code and is
not a setting:

* the shared staff rule (owner, bots, the staff roles, the moderator
  permissions; an unknown standing protects) only ever grows by
  ``protected_role_ids``;
* the triage caps (a timeout of at most an hour, a purge of at most 50
  messages, one delete): Zech's decision on #237;
* a review hold never counts as an earlier action.

**An empty list means none.** No ``enabled_tools`` is no destructive triage
tool, no ``sanctions.allowed`` is no sanction, no ``provisional.signals`` is
no hold, no ``bump_channel.channels`` is no bump channel. Nothing is filled in
for an empty list. The one list where empty means "everywhere" is a rule's
channel scope (``channels``), as a handler's scope always has.

The defaults (:func:`default_document`) are the values production ran with
before the policy existed; a test compares them with the code constants they
replace.

Nothing here imports the bot or the web app: the import script, the web
process and the bot all use it.
"""

from __future__ import annotations

import re
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError
from pydantic import field_validator
from pydantic import model_validator

SCHEMA_VERSION = 1

OFF = "off"
EVALUATE = "evaluate"
ENFORCE = "enforce"
MODES = (OFF, EVALUATE, ENFORCE)
Mode = Literal["off", "evaluate", "enforce"]

# The coordinator's rules, by the names its decisions use.
SPAM_ENGINE = "spam_engine"
ATTACHMENT_FILTER = "attachment_filter"
SPAM_RING = "spam_ring"
SCAM_GATE = "scam_gate"
EDIT_PING = "edit_ping"
BUMP_CHANNEL = "bump_channel"
MASS_MENTION = "mass_mention"
RULES = (
    SPAM_ENGINE,
    ATTACHMENT_FILTER,
    SPAM_RING,
    SCAM_GATE,
    EDIT_PING,
    BUMP_CHANNEL,
    MASS_MENTION,
)

# Sanctions the coordinator may propose. ``delete`` covers every removal of a
# member's message (a single delete and a purge). Kick is a moderator's call:
# no rule proposes one.
WARN = "warn"
TIMEOUT = "timeout"
DELETE = "delete"
BAN = "ban"
SANCTIONS = (WARN, TIMEOUT, DELETE, BAN)

# The high-signal events that get the provisional timeout while judged.
SIGNAL_SCAM_LINK = "scam_link"
SIGNAL_NITRO_BAIT = "nitro_bait"
SIGNAL_MENTION_LINK = "mention_link"
SIGNAL_BURST = "burst"
SIGNAL_IMAGES_HIGH_RISK = "images_high_risk"
SIGNALS = (
    SIGNAL_SCAM_LINK,
    SIGNAL_NITRO_BAIT,
    SIGNAL_MENTION_LINK,
    SIGNAL_BURST,
    SIGNAL_IMAGES_HIGH_RISK,
)

# Where questions that only the spam ring asks are sent. Haiku answers every
# other question, and any question about a message with images (Jev cannot
# see them).
JEV = "jev"
HAIKU = "haiku"

# Triage's destructive tools; the caps on each are in code.
TRIAGE_TOOLS = ("timeout", "purge", "delete")
TRIAGE_ACTIVE_MODES = (EVALUATE, ENFORCE)

DAY = 86_400
#: Discord refuses a timeout longer than this.
DISCORD_MAX_TIMEOUT_SECONDS = 28 * DAY
#: The provisional hold never outlasts this: it is a hold while a verdict is
#: due within seconds, not a sanction.
MAX_PROVISIONAL_SECONDS = 3_600
MAX_CHANNELS = 200
MAX_ROLES = 100
MAX_DOMAINS = 500
MAX_EXTENSIONS = 200
MAX_TEXT = 2_000
MAX_INSTRUCTIONS = 8_000

_SNOWFLAKE = re.compile(r"[0-9]{15,22}")
_HOSTNAME = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+"
)
_EXTENSION = re.compile(r"\.[a-z0-9]{1,10}")


class PolicyError(ValueError):
    """A document this module does not accept. ``errors`` name each field."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _ids(values: list[str], what: str, limit: int) -> list[str]:
    if len(values) > limit:
        raise ValueError(f"at most {limit} {what}")
    out: list[str] = []
    for value in values:
        value = str(value).strip()
        if not _SNOWFLAKE.fullmatch(value):
            raise ValueError(f"{value!r} is not a Discord id")
        if value not in out:
            out.append(value)
    return out


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _Rule(_Section):
    mode: Mode = OFF
    #: Channels the rule runs in; empty is every channel.
    channels: list[str] = Field(default_factory=list)

    @field_validator("channels", mode="before")
    @classmethod
    def _channels(cls, value):
        return _ids(list(value or []), "channels", MAX_CHANNELS)


class SpamEngineRule(_Rule):
    message_rate_threshold: int = Field(5, ge=1, le=1000)
    message_rate_window_seconds: int = Field(5, ge=1, le=3600)
    channel_spread_threshold: int = Field(3, ge=1, le=1000)
    channel_spread_window_seconds: int = Field(15, ge=1, le=3600)
    duplicate_message_min_length: int = Field(15, ge=1, le=4000)
    duplicate_message_window_seconds: int = Field(60, ge=1, le=86_400)
    mass_mention_window_seconds: int = Field(15, ge=1, le=3600)
    warning_reoffense_window_seconds: int = Field(120, ge=1, le=86_400)
    mute_duration_seconds: int = Field(DAY, ge=1, le=DISCORD_MAX_TIMEOUT_SECONDS)
    #: Bare lowercase hostnames; a link matches its host or a subdomain.
    scam_link_domains: list[str] = Field(default_factory=list)
    #: Where defanged scam links the engine removed are archived; None is off.
    scam_log_channel_id: str | None = None

    @field_validator("scam_link_domains", mode="before")
    @classmethod
    def _domains(cls, value):
        domains = [str(d).strip().lower().rstrip(".") for d in (value or [])]
        if len(domains) > MAX_DOMAINS:
            raise ValueError(f"at most {MAX_DOMAINS} domains")
        for domain in domains:
            if not _HOSTNAME.fullmatch(domain):
                raise ValueError(f"{domain!r} is not a bare hostname")
        return list(dict.fromkeys(domains))

    @field_validator("scam_log_channel_id", mode="before")
    @classmethod
    def _log_channel(cls, value):
        return _optional_id(value)


class AttachmentRule(_Rule):
    #: Allowed outright.
    ignored_extensions: list[str] = Field(default_factory=list)
    #: Warned about, not removed. Every other file type is removed.
    warn_extensions: list[str] = Field(default_factory=list)
    #: The member's notices; ``{user}``, ``{extension}`` and ``{filename}``
    #: are filled in. None uses the built-in text.
    warn_message: str | None = Field(None, max_length=MAX_TEXT)
    delete_message: str | None = Field(None, max_length=MAX_TEXT)

    @field_validator("ignored_extensions", "warn_extensions", mode="before")
    @classmethod
    def _extensions(cls, value):
        out = []
        for raw in value or []:
            ext = str(raw).strip().lower()
            if ext and not ext.startswith("."):
                ext = "." + ext
            if not _EXTENSION.fullmatch(ext):
                raise ValueError(f"{raw!r} is not a file extension")
            out.append(ext)
        if len(out) > MAX_EXTENSIONS:
            raise ValueError(f"at most {MAX_EXTENSIONS} extensions")
        return list(dict.fromkeys(out))

    @field_validator("warn_message", "delete_message")
    @classmethod
    def _template(cls, value):
        if value is None or not value.strip():
            return None
        try:
            value.format(user="", extension="", filename="")
        except (KeyError, IndexError, ValueError) as error:
            raise ValueError(
                "only {user}, {extension} and {filename} may be filled in"
            ) from error
        return value


class SpamRingRule(_Rule):
    new_member_days: int = Field(2, ge=1, le=90)
    timeout_seconds: int = Field(7 * DAY, ge=60, le=DISCORD_MAX_TIMEOUT_SECONDS)
    lookback_minutes: int = Field(60, ge=1, le=1440)
    followup_minutes: int = Field(15, ge=1, le=1440)


class ScamGateRule(_Rule):
    #: A member who is not high risk and posts four or more images, without a
    #: hold, is checked on this many of them, picked at random. 0 checks the
    #: post's text only.
    image_sample: int = Field(1, ge=0, le=4)


class EditPingRule(_Rule):
    pass


class BumpChannelRule(_Rule):
    """Members' messages in these channels are removed. The channels are the
    rule, not a scope: an empty list is no bump channel."""


class MassMentionRule(_Rule):
    #: A formal warning on the member's record, once per incident. Off, the
    #: message is only removed.
    warn: bool = True
    #: The warning is sent to the member by DM.
    dm: bool = True


class Rules(_Section):
    spam_engine: SpamEngineRule = Field(default_factory=SpamEngineRule)
    attachment_filter: AttachmentRule = Field(default_factory=AttachmentRule)
    spam_ring: SpamRingRule = Field(default_factory=SpamRingRule)
    scam_gate: ScamGateRule = Field(default_factory=ScamGateRule)
    edit_ping: EditPingRule = Field(default_factory=EditPingRule)
    bump_channel: BumpChannelRule = Field(default_factory=BumpChannelRule)
    mass_mention: MassMentionRule = Field(default_factory=MassMentionRule)


class Controls(_Section):
    #: The rules judge messages at all. Off, nothing is judged or recorded.
    evaluate: bool = True
    #: Automated actions are carried out. Off, every rule only evaluates.
    enforce: bool = True
    #: Incidents are reported to ``destinations.report_channel_id``.
    report: bool = True


class RiskWindows(_Section):
    """How long an earlier action leaves its member high risk (Zech, Oct 24):
    a warning for a day; a timeout, kick or ban for 30 days (a ban counts
    after an unban too)."""

    warn: int = Field(DAY, ge=0, le=365 * DAY)
    timeout: int = Field(30 * DAY, ge=0, le=365 * DAY)
    kick: int = Field(30 * DAY, ge=0, le=365 * DAY)
    ban: int = Field(30 * DAY, ge=0, le=365 * DAY)


class Risk(_Section):
    windows: RiskWindows = Field(default_factory=RiskWindows)
    #: High risk while the member joined less than this long ago; 0 is off.
    joined_within_seconds: int = Field(DAY, ge=0, le=90 * DAY)
    #: High risk on a member's first message.
    first_message: bool = True
    #: High risk after this many days without a message; 0 is off.
    silent_days: int = Field(30, ge=0, le=3650)


class Sanctions(_Section):
    allowed: list[Literal["warn", "timeout", "delete", "ban"]] = Field(
        default_factory=lambda: list(SANCTIONS)
    )
    #: No automated timeout is longer than this.
    max_timeout_seconds: int = Field(
        DISCORD_MAX_TIMEOUT_SECONDS, ge=60, le=DISCORD_MAX_TIMEOUT_SECONDS
    )
    #: A confirmed scam from an account younger than this is a ban.
    scam_ban_account_age_days: int = Field(7, ge=0, le=365)
    #: The timeout a confirmed high-signal or repeat scam gets otherwise.
    scam_timeout_seconds: int = Field(DAY, ge=60, le=DISCORD_MAX_TIMEOUT_SECONDS)
    #: A second scam within this long of an earlier sanction is a repeat.
    repeat_window_seconds: int = Field(30 * DAY, ge=0, le=365 * DAY)

    @field_validator("allowed")
    @classmethod
    def _unique(cls, value):
        return list(dict.fromkeys(value))


class Provisional(_Section):
    enabled: bool = True
    duration_seconds: int = Field(300, ge=30, le=MAX_PROVISIONAL_SECONDS)
    signals: list[
        Literal["scam_link", "nitro_bait", "mention_link", "burst", "images_high_risk"]
    ] = Field(default_factory=lambda: list(SIGNALS))

    @field_validator("signals")
    @classmethod
    def _unique(cls, value):
        return list(dict.fromkeys(value))


class Classifier(_Section):
    #: The backend for a request only the spam ring needs.
    ring_only_backend: Literal["jev", "haiku"] = JEV
    #: The one request's deadline. At most the brief's 10 s: the clients
    #: are built with that timeout.
    deadline_seconds: float = Field(10.0, ge=1.0, le=10.0)
    #: Each at least 1: a cap of 0 would switch judging off unannounced.
    per_author: int = Field(5, ge=1, le=100)
    author_window_seconds: int = Field(600, ge=1, le=DAY)
    per_guild_minute: int = Field(20, ge=1, le=1000)
    #: The acting guild's daily ceiling; a guild in shadow keeps the shadow's.
    per_guild_day: int = Field(1500, ge=1, le=100_000)


class Destinations(_Section):
    #: The one channel incident reports go to; None reports nowhere.
    report_channel_id: str | None = None
    #: The one role a spam mute's report may ping; None pings nobody.
    mod_ping_role_id: str | None = None
    #: Notices posted in the member's channel (a removed file, an edit-ping).
    channel_notices: bool = True
    #: Messages sent to the member by DM.
    member_dms: bool = True

    @field_validator("report_channel_id", "mod_ping_role_id", mode="before")
    @classmethod
    def _id(cls, value):
        return _optional_id(value)


class Triage(_Section):
    """The staff-ping review: a message that mentions a monitored role.

    ``enforce`` runs it with ``enabled_tools``; ``evaluate`` runs it with no
    destructive tool (it still assesses and reports); ``off`` does not run
    it."""

    mode: Mode = OFF
    monitored_role_ids: list[str] = Field(default_factory=list)
    instructions: str = Field("", max_length=MAX_INSTRUCTIONS)
    enabled_tools: list[Literal["timeout", "purge", "delete"]] = Field(
        default_factory=list
    )
    context_message_limit: int = Field(25, ge=1, le=100)
    #: Where the review posts its report; None posts it in the channel the
    #: ping came from.
    response_channel_id: str | None = None

    @field_validator("response_channel_id", mode="before")
    @classmethod
    def _channel(cls, value):
        return _optional_id(value)

    @field_validator("monitored_role_ids", mode="before")
    @classmethod
    def _roles(cls, value):
        return _ids(list(value or []), "roles", MAX_ROLES)

    @field_validator("enabled_tools")
    @classmethod
    def _unique(cls, value):
        return list(dict.fromkeys(value))


class PolicyDocument(_Section):
    schema_version: Literal[1] = Field(SCHEMA_VERSION, alias="schema")
    controls: Controls = Field(default_factory=Controls)
    #: Added to the shared staff rule; they can only widen it.
    protected_role_ids: list[str] = Field(default_factory=list)
    rules: Rules = Field(default_factory=Rules)
    risk: Risk = Field(default_factory=Risk)
    sanctions: Sanctions = Field(default_factory=Sanctions)
    provisional: Provisional = Field(default_factory=Provisional)
    classifier: Classifier = Field(default_factory=Classifier)
    destinations: Destinations = Field(default_factory=Destinations)
    triage: Triage = Field(default_factory=Triage)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @field_validator("protected_role_ids", mode="before")
    @classmethod
    def _roles(cls, value):
        return _ids(list(value or []), "roles", MAX_ROLES)

    @model_validator(mode="after")
    def _consistent(self):
        if self.sanctions.scam_timeout_seconds > self.sanctions.max_timeout_seconds:
            raise ValueError(
                "sanctions.scam_timeout_seconds is longer than "
                "sanctions.max_timeout_seconds"
            )
        return self

    # -- reading ------------------------------------------------------------

    def rule(self, name: str) -> _Rule:
        return getattr(self.rules, name)

    def mode(self, name: str) -> str:
        """A rule's effective mode: the guild-wide controls first."""
        if not self.controls.evaluate:
            return OFF
        mode = self.rule(name).mode
        if mode == ENFORCE and not self.controls.enforce:
            return EVALUATE
        return mode

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def _optional_id(value):
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    value = str(value).strip()
    if not _SNOWFLAKE.fullmatch(value):
        raise ValueError(f"{value!r} is not a Discord id")
    return value


def validate(document: Any) -> PolicyDocument:
    """The document as this module reads it; raises :class:`PolicyError`.

    Unknown keys are refused rather than dropped: a misspelt setting must not
    look saved."""
    if isinstance(document, PolicyDocument):
        return document
    if not isinstance(document, dict):
        raise PolicyError(["the policy must be a JSON object"])
    try:
        return PolicyDocument.model_validate(document)
    except ValidationError as error:
        raise PolicyError(
            [
                f"{'.'.join(str(part) for part in item['loc']) or 'policy'}: "
                f"{item['msg']}"
                for item in error.errors()
            ]
        ) from None


def read_document(document: Any) -> PolicyDocument:
    """A stored document as this code reads it: settings it does not know
    are ignored, as is a newer schema number.

    A save (:func:`validate`) refuses unknown keys, so a misspelt setting
    never looks saved; a read must not, or rolling the image back after a
    newer one saved a setting this one lacks would stop moderation. A value
    this code cannot accept still raises: the reader fails closed."""
    if not isinstance(document, dict):
        raise PolicyError(["the policy must be a JSON object"])
    known = _known(PolicyDocument, document)
    known["schema"] = SCHEMA_VERSION
    return validate(known)


def _known(model: type[BaseModel], data: dict[str, Any]) -> dict[str, Any]:
    kept: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        key = info.alias or name
        if key not in data:
            continue
        value = data[key]
        annotation = info.annotation
        if (
            isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
            and isinstance(value, dict)
        ):
            value = _known(annotation, value)
        kept[key] = value
    return kept


def default_document() -> PolicyDocument:
    """Every rule off, the settings at production's values."""
    return PolicyDocument()


def changed_fields(
    before: dict[str, Any] | None, after: dict[str, Any], prefix: str = ""
) -> list[str]:
    """The dotted paths whose values differ, for the revision history."""
    before = before or {}
    changed: list[str] = []
    for key in sorted(set(before) | set(after)):
        path = f"{prefix}{key}"
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            changed += changed_fields(old, new, path + ".")
        elif old != new:
            changed.append(path)
    return changed
