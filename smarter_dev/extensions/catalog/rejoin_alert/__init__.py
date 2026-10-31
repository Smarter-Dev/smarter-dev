"""Rejoin Alert extension.

One guild-scoped admin handler on the ``member_join`` trigger: when someone with
prior moderation history rejoins, the moderators get a single heads-up. See
``docs/v2/feature-parity/automated-and-command-moderation.md`` §4.2
"Handler E".

``member_join`` is the cheap gate that makes the mod-action read affordable — it
fires only on a join, never per message — so the whole handler is one
``list_mod_actions`` lookup and at most one report per join, and bot joins
short-circuit before even that. Join storms are bounded further by the
``member_join`` fires-per-minute ceiling at dispatch.

Version 2 hands the heads-up to the moderation system with
``report_for_review`` instead of posting its own line in a mod-log channel: a
review-only incident (no effect on the member, no ``moderation_actions`` row)
reported in the guild's incident report channel, the one every moderation
incident goes to, and nowhere while the guild's policy has reporting switched
off. A member is reported at most once per review window, however often they
rejoin. So the extension has no config any more: version 1's
``mod_log_channel_id`` is dropped, and updating an install that saved one
discards it (``validate_stored_config``).
"""

from __future__ import annotations

from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.extensions.schema import HandlerTemplate

MANIFEST = ExtensionManifest(
    slug="rejoin-alert",
    title="Rejoin Alert",
    summary=(
        "Reports a member with prior moderation history who rejoins the server "
        "to the moderators, with their most recent action, in the moderation "
        "incident report channel."
    ),
    version=2,
    config=[],
    handlers=[
        HandlerTemplate(
            key="rejoin-alert",
            name="rejoin-alert",
            trigger_type="member_join",
            description=(
                "Reports a joining member who already has moderation history in "
                "this guild to the moderators for review"
            ),
            script_file="rejoin_alert.monty",
            settings={},
            # member_join is guild-scoped: no home channel, so no channel scope.
            channel_scope=[],
        ),
    ],
    example_config={},
)
