"""Disboard bumping extension.

Two admin handlers over one shared config, ported from the legacy
``DisboardBumpReminderExtension`` per
``docs/v2/feature-parity/engagement-loops-and-server-stats.md`` §4.

- ``bump-tracker`` (message trigger, scoped to the bump channel,
  ``include_bot_messages``): hard-guarded on Disboard's user id
  (``302050872383242240``), it detects the "Bump done" confirmation embed,
  credits the ``/bump`` invoker (``interaction_user_id``), maintains a 7-day
  bump ledger, rotates the Bump King crown role, keeps the channel clean, and
  arms a one-shot 2-hour reminder with ``schedule_timer`` (whose timer re-fire
  the same handler serves via its ``trigger_type == "timer"`` branch).
- ``bump-commands`` (message trigger, in a general/bot-commands channel):
  ``!bumpers`` and ``!bumps`` read the shared ledger and reply with the
  leaderboard / recent-bump list.

The two rows cross state through the guild-shared memory store
(``guild_memory_*``), keyed under a ``disboard_`` namespace. The legacy
online-count family and the startup channel-history scan are dropped per the
disposition table; the reminder is folded into the tracker via ``schedule_timer``
rather than a separate polling schedule handler (see the module docstring notes).
"""

from __future__ import annotations

from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)

MANIFEST = ExtensionManifest(
    slug="disboard-bumping",
    title="Disboard Bumping",
    summary=(
        "Tracks confirmed Disboard /bump commands, crowns the top bumper with a "
        "Bump King role, reminds the server every 2 hours, and answers !bumpers "
        "/ !bumps."
    ),
    version=1,
    config=[
        ConfigField(
            name="bump_channel_id",
            type="channel_id",
            label="Bump channel",
            help=(
                "The channel where /bump is run. The tracker is scoped here, "
                "credits confirmations, and deletes any other message to keep it "
                "clean."
            ),
        ),
        ConfigField(
            name="bump_king_role_id",
            type="role_id",
            label="Bump King role",
            help=(
                "Granted to the member with the most bumps in the last 7 days and "
                "revoked from the previous holder. The bot's top role must sit "
                "above it."
            ),
        ),
        ConfigField(
            name="commands_channel_id",
            type="channel_id",
            label="Bump commands channel",
            help=(
                "Where members run !bumpers / !bumps. Must NOT be the bump "
                "channel, where the tracker would delete the command."
            ),
        ),
        ConfigField(
            name="reminder_ping_role_id",
            type="string",
            required=False,
            default="",
            label="Reminder ping role id (optional)",
            help=(
                "A role id to ping on the 2-hour reminder (e.g. a bump-squad "
                "role). Leave blank for a silent reminder."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="bump-tracker",
            name="disboard-bump-tracker",
            trigger_type="message",
            description=(
                "Detects Disboard bump confirmations, credits the bumper, rotates "
                "the Bump King crown, keeps the bump channel clean, and arms the "
                "2-hour bump reminder"
            ),
            script_file="bump_tracker.monty",
            settings={
                "include_bot_messages": True,
                "allowed_role_ids": ["{{cfg.bump_king_role_id}}"],
            },
            channel_scope=["bump_channel_id"],
        ),
        HandlerTemplate(
            key="bump-commands",
            name="disboard-bump-commands",
            trigger_type="message",
            description=(
                "Replies to !bumpers and !bumps with the 7-day bump leaderboard "
                "and the recent-bump list"
            ),
            script_file="bump_commands.monty",
            settings={},
            channel_scope=["commands_channel_id"],
        ),
    ],
    example_config={
        "bump_channel_id": "111111111111111111",
        "bump_king_role_id": "222222222222222222",
        "commands_channel_id": "333333333333333333",
        "reminder_ping_role_id": "444444444444444444",
    },
)
