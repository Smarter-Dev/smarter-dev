"""Rejoin Alert extension.

One guild-scoped admin handler on the ``member_join`` trigger: when someone with
prior moderation history rejoins, the mod-log channel gets a single heads-up
line. See ``docs/v2/feature-parity/automated-and-command-moderation.md`` §4.2
"Handler E".

``member_join`` is the cheap gate that makes the mod-action read affordable — it
fires only on a join, never per message — so the whole handler is one
``list_mod_actions`` lookup and at most one message per join, and bot joins
short-circuit before even that. Join storms are bounded further by the
``member_join`` fires-per-minute ceiling at dispatch.

The trigger is guild-scoped with no home channel, so ``channel_scope`` stays
empty and the mod-log channel is baked into the script as a send-target constant
— how any guild-scoped admin handler names its output channel.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import ConfigField
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.extensions.schema import HandlerTemplate

MANIFEST = ExtensionManifest(
    slug="rejoin-alert",
    title="Rejoin Alert",
    summary=(
        "Posts a mod-log heads-up when a member with prior moderation history "
        "rejoins the server, including their most recent action."
    ),
    version=1,
    config=[
        ConfigField(
            name="mod_log_channel_id",
            type="channel_id",
            label="Mod-log channel",
            help=(
                "Rejoin alerts are posted here. Use a staff-only channel — the "
                "alert names the member and their most recent moderation action."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="rejoin-alert",
            name="rejoin-alert",
            trigger_type="member_join",
            description=(
                "Alerts the mod-log channel when a joining member already has "
                "moderation history in this guild"
            ),
            script_file="rejoin_alert.monty",
            settings={},
            # member_join is guild-scoped: no home channel, so no channel scope.
            channel_scope=[],
        ),
    ],
    example_config={"mod_log_channel_id": "123456789012345678"},
)
