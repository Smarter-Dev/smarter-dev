"""Member-count channel display extension.

A single schedule-trigger admin handler that reads the guild's approximate
member count and renames a stats channel to show it (legacy ``📊Members: 1.2k``).
The rename is **change-gated** — it fires only when the rendered name differs
from the last one applied — because Discord hard-caps channel renames at 2 per
10 minutes; the handler polls every 600s (>= the 5-minute rename rail). See
``docs/v2/feature-parity/engagement-loops-and-server-stats.md`` (Handler 4,
disposition rows #16–#19, #25).

Two placeholder systems meet in ``name_format`` and never collide:

* ``{{cfg.name_format}}`` is the INSTALL-TIME template — the installer
  substitutes it with the admin's config value before the script ever runs.
* ``{count}`` inside that value is the RUNTIME placeholder — the script itself
  replaces it with the live member count on each fire. It is ordinary text to
  the install renderer, which only ever matches the ``{{cfg.`` prefix.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)

# Fixed poll interval, baked into the handler settings as a bare int literal
# (not an admin-facing config field). Held at 600s so the rename rail — Discord's
# 2-renames-per-10-minutes cap plus the author-prompt ">= 5-minute" schedule
# floor — always holds; exposing it as config would let an admin drop below the
# floor and burn the cap.
_POLL_INTERVAL_SECONDS = 600

MANIFEST = ExtensionManifest(
    slug="member-count-display",
    title="Member Count Display",
    summary=(
        "Renames a stats channel every 10 minutes to show the guild's member "
        "count (e.g. \U0001F4CAMembers: 1.2k). Change-gated to respect Discord's "
        "2-renames-per-10-minutes limit."
    ),
    version=1,
    config=[
        ConfigField(
            name="stats_channel_id",
            type="channel_id",
            label="Stats channel",
            help=(
                "The channel whose name is rewritten to show the member count. "
                "A voice or text channel used purely as a live counter works "
                "best. The handler renames only this channel."
            ),
        ),
        ConfigField(
            name="name_format",
            type="string",
            required=False,
            default="{icon}Members: {count}",
            label="Channel name format",
            help=(
                "The channel name template. Two runtime tokens are substituted "
                "each run: {count} -> the live member count (1000+ renders as "
                "\"1.2k\", below 1000 as a plain number), and {icon} -> the "
                "legacy \U0001F4CA counter glyph. Write everything else exactly "
                "as you want it shown; e.g. the default \"{icon}Members: "
                "{count}\" renders as \"\U0001F4CAMembers: 1.2k\". These braces "
                "are runtime tokens, unrelated to the installer's own field "
                "syntax. Omit {icon} for no emoji. (The glyph is fixed to the "
                "legacy \U0001F4CA — an emoji typed directly into this field "
                "cannot be carried into the sandboxed script, so position it "
                "with {icon} rather than pasting one here.)"
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="member-count-display",
            name="member-count-display",
            trigger_type="schedule",
            description=(
                "Renames the stats channel to the guild member count, "
                "change-gated, on a 10-minute schedule"
            ),
            script_file="member_count_display.monty",
            settings={"interval_seconds": _POLL_INTERVAL_SECONDS},
            channel_scope=["stats_channel_id"],
        ),
    ],
    example_config={
        "stats_channel_id": "123456789012345678",
        "name_format": "{icon}Members: {count}",
    },
)
