"""Member Milestones extension.

Two guild-scoped celebration handlers over one shared config. ``milestone-announce``
posts to the announce channel each time the server's human member count crosses a
new multiple of the configured milestone step (memory-gated by a high-water mark
so each milestone announces once, and self-healing after a purge). ``booster-thanks``
thanks a member the moment they start boosting, preserving both the total boost
count and the number of boosting members. See
``docs/v2/feature-parity/member-lifecycle-and-role-automation.md``
(celebration-engagement group: ``check_for_highscore`` and the server-boost
announcement).

Both triggers (``member_join`` / ``member_role_change``) are guild-scoped with no
home channel, so ``channel_scope`` stays empty and the announce channel is baked
into each script as a send-target constant — exactly how a guild-scoped admin
handler names its output channel.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)

MANIFEST = ExtensionManifest(
    slug="member-milestones",
    title="Member Milestones",
    summary=(
        "Announces member-count milestones as the server grows and thanks "
        "members the moment they start boosting — both to one announce channel."
    ),
    version=1,
    config=[
        ConfigField(
            name="announce_channel_id",
            type="channel_id",
            label="Announce channel",
            help="Milestone and booster celebrations are posted here.",
        ),
        ConfigField(
            name="milestone_step",
            type="int",
            required=False,
            default=250,
            label="Milestone step",
            help=(
                "Announce each time the human member count crosses a new "
                "multiple of this number (e.g. 250 → 250, 500, 750, …)."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="milestone-announce",
            name="member-milestones",
            trigger_type="member_join",
            description=(
                "Announces when the human member count crosses a new milestone "
                "step (high-water-mark gated so each milestone fires once)"
            ),
            script_file="milestone_announce.monty",
            settings={},
            channel_scope=[],
        ),
        HandlerTemplate(
            key="booster-thanks",
            name="server-booster-thanks",
            trigger_type="member_role_change",
            description="Thanks a member in the announce channel when they start boosting",
            script_file="booster_thanks.monty",
            settings={},
            channel_scope=[],
        ),
    ],
    example_config={
        "announce_channel_id": "123456789012345678",
        "milestone_step": 250,
    },
)
