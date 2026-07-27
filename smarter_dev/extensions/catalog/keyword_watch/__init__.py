"""Keyword Watch extension.

One guild-wide admin handler on the ``message`` trigger: a configured list of
watched terms is the cheap gate, and anything that trips it is handed to the
guild's own LLM instruction for a single moderation verdict, which the script
then re-validates against the actions the guild actually permitted.

The extension exists because the two halves of "watch for X and deal with it"
belong to different owners. WHAT to watch for and WHAT the guild considers a
violation are the admin's (``keywords`` + ``agent_instruction``); HOW FAR the
bot may go is a rail (``allowed_actions``); and the guards the config cannot
express — never act on a bot, never act on a moderator, never ban an
established member on an LLM's say-so — are baked into the script where no
config edit can loosen them.

MATCHING SEMANTICS. Terms match on WHOLE WORDS, not substrings. Message and
terms go through one shared normalizer (lowercase, split on whitespace, strip
punctuation off each word's *edges* only) and the result is padded with a space
at each end, so a term is tested as ``" term "`` inside ``" the message "``.
Monty has no regex, so this is how a word boundary is expressed at all. The
alternative — a plain ``term in content`` substring test — was rejected because
it is wrong in the direction that costs money and trust: "class" fires on
"classic", "ass" on "class", and every one of those false hits spends a lookup,
an agent call, and an admin's attention. Stripping only the edges keeps
"e-mail" and "don't" intact, and a multi-word entry ("free nitro") still
matches as a contiguous phrase because the normalized haystack is one
space-joined string. The known cost of the choice is that a term glued to
another word ("freenitro") and a term inside a URL are missed; a guild that
wants those adds them as their own terms.

COST SHAPE. An ordinary message costs two bool tests, one lowercase-and-split
of the content, and one membership test per configured term. No read, no agent
call, no emit, no memory — which is the property that makes a guild-wide
message handler installable at all. Everything expensive sits behind the
keyword gate, and a message that DOES trip it costs a fixed ceiling — one
``list_mod_actions`` lookup, one ``spawn_agent`` call, at most one moderation
action, and one mod-log line — no matter how many terms hit or what the agent
replies. Account age and join date are read off the trigger context rather than
from ``get_member_info``, so the facts the verdict needs cost no second lookup.

The handler keeps NO memory. The failure mode that kills guild-wide message
handlers (a per-user map that grows with traffic until the 16 KB ceiling errors
every fire) cannot happen here: prior history comes from ``list_mod_actions``
and the authoritative warn total comes back from ``warn_user`` itself.

A "none" verdict is reported to the mod log even though it takes no action, and
stays silent in the triggering channel. It costs the one message the fire was
already budgeted for, and the no-action lines are the only feedback an admin
has for tuning a term list — a watch whose misses are invisible gets widened
until it is expensive.

``message`` is channel-keyed, so the fire has a home channel: the public warn
notice lands where the message was. Only the mod-log report names a channel
explicitly, baked in as a quoted literal at install time. ``channel_scope``
stays empty — watched terms are not channel-specific.

The report links to the triggering message only when it lives in a thread. A
message-trigger context carries ``message_id``, ``guild_id`` and (in a thread)
``thread_id``, but NOT the id of the plain channel the fire came from, and a
jump link needs all three parts — so outside a thread the report names the
message id instead of guessing. Same resolution as the newcomer-watch report.

SIZE BUDGET. The rendered script must fit the host's 8 KB cap, and the three
free-text fields are spliced into it as literals; together they have roughly
700 characters of room. A longer config is refused by ``render_bundle`` at
install time with the lint reason, before any row is written — an install-time
error, never a runtime one.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import ConfigField
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.extensions.schema import HandlerTemplate

MANIFEST = ExtensionManifest(
    slug="keyword-watch",
    title="Keyword Watch",
    summary=(
        "Watches every message for a list of terms and, on a hit, asks an LLM "
        "whether it breaks your rules — applying at most one moderation action "
        "from the set you permitted and reporting the call to your mod log."
    ),
    version=1,
    config=[
        ConfigField(
            name="keywords",
            type="string",
            label="Watched terms",
            help=(
                "Comma-separated terms to watch for, e.g. "
                '"free nitro, steam gift, crypto giveaway". Matching is by '
                "WHOLE WORD, case-insensitive, ignoring surrounding "
                'punctuation — "class" will not fire on "classic". A '
                "multi-word entry matches as a contiguous phrase."
            ),
        ),
        ConfigField(
            name="agent_instruction",
            type="string",
            label="What counts as a violation",
            help=(
                'Your own instruction to the deciding LLM, e.g. "Flag scam and '
                'phishing bait; ordinary talk about giveaways is fine." It only '
                "ever sees messages that already matched a term. Keep this and "
                "your term list to about 700 characters between them — both are "
                "baked into the handler script, which the host caps at 8 KB."
            ),
        ),
        ConfigField(
            name="allowed_actions",
            type="string",
            label="Permitted actions",
            help=(
                "Comma-separated subset of none, warn, timeout, kick, ban — the "
                "MOST the bot may do. Anything the LLM names that is not on this "
                "list is downgraded to a mod-log report and never applied. Start "
                'with "none, warn" and widen once you trust the calls.'
            ),
        ),
        ConfigField(
            name="mod_log_channel_id",
            type="channel_id",
            label="Mod-log channel",
            help=(
                "Every decision is reported here, including the ones that took "
                "no action — that record is how you tune your term list. Use a "
                "staff-only channel: the line names the member and quotes the "
                "LLM's reasoning."
            ),
        ),
        ConfigField(
            name="timeout_seconds",
            type="int",
            label="Timeout length (seconds)",
            help=(
                'How long a "timeout" decision mutes the member. Default 600 '
                "(10 minutes)."
            ),
            required=False,
            default=600,
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="keyword-watch",
            name="keyword-watch",
            trigger_type="message",
            description=(
                "Watches messages for configured terms and asks an LLM for one "
                "moderation verdict, applying only actions the guild permitted "
                "and reporting every call to the mod log"
            ),
            script_file="keyword_watch.monty",
            # No roles are granted, and bot messages are deliberately NOT opted
            # into: a bot is never a moderation target here, so seeing bot
            # traffic would only cost guard work on every webhook post.
            settings={},
            # Empty scope = every channel: watched terms are not channel-specific.
            channel_scope=[],
        ),
    ],
    example_config={
        "keywords": "free nitro, steam gift, crypto giveaway",
        "agent_instruction": (
            "Flag scam and phishing bait aimed at members. Ordinary "
            "conversation that merely mentions these things is fine."
        ),
        "allowed_actions": "none, warn, timeout",
        "mod_log_channel_id": "123456789012345678",
        "timeout_seconds": 600,
    },
)
