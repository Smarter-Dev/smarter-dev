"""Server Birthday extension.

One schedule handler that posts a celebration in a configured announcement
channel on the server's founding anniversary, once per calendar year. The
handler self-filters to the founding month/day and computes the ordinal
("Nth birthday") from the founding year, so it generalises the beginner.codes
Nov-13 birthday message (which hard-coded a 2020 founding year) to any guild.
See ``docs/v2/feature-parity/member-lifecycle-and-role-automation.md`` (§4.2
"Standard handler ``server-birthday``", disposition **handler-today**).

Idempotence is a memory gate on the year it last announced: a repeat fire on
the anniversary day (worker retry, restart) is a harmless no-op.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)

MANIFEST = ExtensionManifest(
    slug="server-birthday",
    title="Server Birthday",
    summary=(
        "Posts a celebration in your announcement channel every year on the "
        "server's founding anniversary."
    ),
    version=1,
    config=[
        ConfigField(
            name="announce_channel_id",
            type="channel_id",
            label="Announcement channel",
            help="The birthday message is posted here.",
        ),
        ConfigField(
            name="founding_month",
            type="int",
            label="Founding month (1-12)",
            help="The month the server was created.",
        ),
        ConfigField(
            name="founding_day",
            type="int",
            label="Founding day (1-31)",
            help="The day of the month the server was created.",
        ),
        ConfigField(
            name="founding_year",
            type="int",
            label="Founding year",
            help=(
                "The year the server was created. Used to compute the "
                "'Nth birthday' number."
            ),
        ),
        ConfigField(
            name="celebration_gif_url",
            type="string",
            required=False,
            default="",
            label="Celebration image/GIF URL (optional)",
            help=(
                "Posted just before the birthday message. Leave blank to send "
                "only the text. Note: link previews are suppressed, so a GIF "
                "link appears as a bare URL."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="birthday-announcement",
            name="server-birthday-announcement",
            trigger_type="schedule",
            description=(
                "Posts a once-a-year celebration on the server's founding "
                "anniversary"
            ),
            script_file="server_birthday.monty",
            # Daily 01:00 UTC no-op check; the script self-filters to the
            # founding month/day (the feature-parity doc's chosen fire time).
            settings={"daily_time": "01:00"},
            channel_scope=["announce_channel_id"],
        ),
    ],
    example_config={
        "announce_channel_id": "123456789012345678",
        "founding_month": 11,
        "founding_day": 13,
        "founding_year": 2020,
        "celebration_gif_url": "https://example.com/party.gif",
    },
)
