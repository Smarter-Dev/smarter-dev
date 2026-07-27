"""Newcomer Watch extension.

One guild-wide admin handler on the ``message`` trigger. It watches the two
populations a guild's moderators actually worry about — members who joined
within the last ``new_member_days``, and established members posting for the
first time after ``inactive_days`` of silence — and asks an LLM, under the
guild's own written instruction, whether that one message warrants a moderation
action. Exactly one action is applied per fire and one line lands in the
mod-log.

WHY THE GUARD IS THE WHOLE DESIGN. A guild-wide message handler runs on every
message in the server, so the common path must cost nothing. The membership /
activity test is computed entirely from facts the dispatcher already put in the
trigger context — ``author_joined_at`` for membership duration and
``author_days_since_last_message`` for the absence — so an ordinary message from
an ordinary member costs one ISO parse and a comparison, and returns before any
lookup, agent call, or emit. Nothing below the guard is reachable without a
match.

COST SHAPE. Unwatched message: zero spends (bot authors, staff, and
content-less messages short-circuit even earlier). Watched message, worst case:
ONE ``list_mod_actions`` lookup (depth 5), ONE ``spawn_agent`` call, ONE
moderation action, and ONE mod-log message — inside the admin caps of 10
lookups / 3 agent calls / 25 moderation actions / 5 messages with room to spare
(a ``warn`` verdict additionally spends its own notice and DM from the message
pool, as ``warn_user`` documents). ``get_member_info`` is deliberately NOT
called: every fact it would add for a cached member — join date, account age —
is already in the message context for free, and when the member object is
uncached the fire can only be on the returning branch, where the ABSENCE, not
the tenure, is the signal; the prompt says "tenure unknown" rather than paying a
lookup on a path the guard has already narrowed. The handler keeps NO memory:
there is nothing to prune and nothing to outgrow.

"FIRST MESSAGE BACK" IS FREE. Dispatch reads the activity facts BEFORE it
records the triggering message (``handler_dispatch.py``: ``get_activity`` →
``activity_facts`` → ``record_activity``), so a returning member's first message
still reports the full absence, and their SECOND message reports
``author_days_since_last_message == 0`` and no longer matches. The "have I
already looked at this member?" bookkeeping a naive implementation would keep in
handler memory is therefore unnecessary — the platform's own recording order
provides it. (The bot ALSO batches every human message to ``/activity/batch``
every 30 s. If a flush happens to land between the gateway event and the
dispatch POST for the same message, that message's own timestamp is written
first and the absence reads as 0 — the fire is simply skipped. That is the
fail-closed direction: a missed review, never a duplicate one.)

An uncached member object leaves ``author_joined_at`` null, which makes the
new-member condition unsatisfiable — also fail-closed, and also the right
direction: the alternative is spending a lookup on every message just to find
out whether the message was worth looking at.

FIRST MESSAGE EVER IS DELIBERATELY NOT WATCHED. ``author_is_first_message`` is
true whenever the activity table has never observed the member, which includes
every long-standing member's first message after activity tracking began.
Treating that as an absence would fire an agent call on the entire active
membership once after install, so the returning condition requires a REAL
measured absence (``author_days_since_last_message``); a genuinely brand-new
member is covered by the new-member condition instead.

SAFETY RAILS THE CONFIG CANNOT EXPRESS. Bot/webhook authors are never acted on.
A member with guild-level Manage Messages or Administrator
(``author_has_manage_messages``) is exempt before anything is spent, matching
``/warn``'s refusal to warn moderators. The action the LLM names is re-validated
against ``allowed_actions`` AFTER parsing, so a model naming an action the guild
never permitted is downgraded to a mod-log report and never applied — and an
empty or unparseable ``allowed_actions`` degrades to report-only, not to
free rein. "none" is ALWAYS in the list the model is shown, even when the guild
left it out: the list is the model's whole menu, so a guild that wrote
"warn,timeout" must still leave it a way to say nothing is wrong. Ban proportionality is asked for in the prompt AND enforced in code:
a ban verdict is downgraded to a report unless the account is younger than 30
days and the member joined within the last 7, with an unknown age counting
against the ban — a prompt is not a rail.

A "none" verdict is silent everywhere, including the mod-log. A guild-wide watch
that logged every clean newcomer message would bury the genuine flags in
routine noise and train staff to skim past the channel; the durable handler-run
record is where "how often did this fire?" belongs.

THE MOD-LOG LINE CANNOT ALWAYS CARRY A JUMP LINK. The message trigger context
has no channel id (see ``handler_events.message_context`` — the fire's channel
is bound host-side and never exposed to the sandbox), so
``https://discord.com/channels/<guild>/<channel>/<message>`` is only
constructible when the message was typed inside a thread, where
``context["thread_id"]`` supplies the channel component. Every report therefore
carries the message id and a quoted excerpt, which also survives the message
being deleted, and adds the jump link when the thread case makes one available.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import ConfigField
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.extensions.schema import HandlerTemplate

MANIFEST = ExtensionManifest(
    slug="newcomer-watch",
    title="Newcomer Watch",
    summary=(
        "Reviews messages from members who just joined, and from established "
        "members posting for the first time after a long absence, against your "
        "own written instruction — then applies at most one permitted "
        "moderation action and reports it to the mod-log."
    ),
    version=1,
    config=[
        ConfigField(
            name="agent_instruction",
            type="string",
            label="What to watch for",
            help=(
                "Your instruction to the reviewing model, in plain English — "
                "e.g. 'Flag crypto/NFT promotion, invite-link spam, and DM "
                "solicitations. Ordinary introductions and questions are fine.' "
                "It is treated as authoritative; the message being reviewed is "
                "always treated as untrusted data."
            ),
        ),
        ConfigField(
            name="allowed_actions",
            type="string",
            label="Permitted actions",
            help=(
                "Comma-separated subset of warn, timeout, kick, ban. The model "
                "is told exactly this list (plus 'none', which is always "
                "available to it) and anything it names outside it is "
                "downgraded to a mod-log report instead of being applied. "
                "Leave it as 'none,warn' until you trust the instruction; an "
                "empty value makes the handler report-only."
            ),
            required=False,
            default="none,warn",
        ),
        ConfigField(
            name="mod_log_channel_id",
            type="channel_id",
            label="Mod-log channel",
            help=(
                "Every applied action and every downgraded report is posted "
                "here. Use a staff-only channel — the line names the member and "
                "quotes their message."
            ),
        ),
        ConfigField(
            name="new_member_days",
            type="int",
            label="New-member window (days)",
            help=(
                "A member who joined within this many days is reviewed on "
                "EVERY message they send. Keep it short — this is the setting "
                "that decides how many agent calls the handler makes."
            ),
            required=False,
            default=7,
        ),
        ConfigField(
            name="inactive_days",
            type="int",
            label="Absence threshold (days)",
            help=(
                "An established member whose previous message was longer ago "
                "than this is reviewed on their first message back, and only "
                "that one — their next message already reads as active."
            ),
            required=False,
            default=30,
        ),
        ConfigField(
            name="timeout_seconds",
            type="int",
            label="Timeout duration (seconds)",
            help=(
                "How long a 'timeout' decision mutes the member. 600 (ten "
                "minutes) is a correction; use longer only if you also expect "
                "the model to be right about repeat offenders."
            ),
            required=False,
            default=600,
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="newcomer-watch",
            name="newcomer-watch",
            trigger_type="message",
            script_file="newcomer_watch.monty",
            description=(
                "Reviews messages from newly-joined members and from members "
                "returning after a long absence, and applies at most one "
                "permitted moderation action with a mod-log report"
            ),
            # No role grants, so no allowed_role_ids; and no bot-message opt-in
            # — a bot is never a "new member" worth reviewing.
            settings={},
            # Empty scope = every channel. The populations this watches are not
            # channel-specific, and the guard is what makes guild-wide safe.
            channel_scope=[],
        ),
    ],
    example_config={
        "agent_instruction": (
            "Flag crypto or NFT promotion, invite-link spam, and unsolicited "
            "DM solicitations. Introductions and questions are fine."
        ),
        "allowed_actions": "none,warn,timeout",
        "mod_log_channel_id": "123456789012345678",
        "new_member_days": 7,
        "inactive_days": 30,
        "timeout_seconds": 600,
    },
)
