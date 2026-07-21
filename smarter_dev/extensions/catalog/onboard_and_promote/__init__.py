"""Onboard & Promote extension.

One admin handler over a three-field config. On ``member_rules_accepted`` it
grants the configured newcomer (holding) role and arms a persisted one-shot
``schedule_timer`` for the configured delay; when that timer re-fires it promotes
the member to the full-member role and, only on a successful promotion, removes
the holding role. Ports the beginner.codes ``onboard-and-promote`` handler from
``docs/v2/feature-parity/member-lifecycle-and-role-automation.md`` (§4.1).

The handler is idempotent by construction, which matters because
``member_rules_accepted`` can fire more than once for a member (the E1
at-least-once cache-miss policy and E6's startup replay both re-dispatch only
members who hold no role beyond ``@everyone`` — precisely the case where re-running
the grant + re-arming the timer is the desired recovery). Re-adding the holding
role is a Discord no-op, and ``add_role`` returning ``False`` when the member has
left gates the holding-role removal, so a promotion that fires after the member
departed is a silent no-op.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)

MANIFEST = ExtensionManifest(
    slug="onboard-and-promote",
    title="Onboard & Promote",
    summary=(
        "Grants a newcomer role when a member accepts the rules, then promotes "
        "them to the full-member role after a configurable delay."
    ),
    version=1,
    config=[
        ConfigField(
            name="newcomer_role_id",
            type="role_id",
            label="Newcomer (holding) role",
            help="Granted the moment a member accepts the rules.",
        ),
        ConfigField(
            name="full_member_role_id",
            type="role_id",
            label="Full-member role",
            help="Granted after the promotion delay; the newcomer role is then removed.",
        ),
        ConfigField(
            name="promotion_delay_hours",
            type="int",
            required=False,
            default=48,
            label="Promotion delay (hours)",
            help=(
                "Whole hours to wait before promoting. Must be 1–720 "
                "(schedule_timer allows 1 minute to 30 days)."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="onboard-and-promote",
            name="onboard-and-promote",
            trigger_type="member_rules_accepted",
            description=(
                "Grants the newcomer role on rules acceptance and promotes to the "
                "full-member role after the configured delay"
            ),
            script_file="onboard_and_promote.monty",
            settings={
                "allowed_role_ids": [
                    "{{cfg.newcomer_role_id}}",
                    "{{cfg.full_member_role_id}}",
                ]
            },
            channel_scope=[],
        ),
    ],
    example_config={
        "newcomer_role_id": "888160821673349140",
        "full_member_role_id": "644325811301777426",
        "promotion_delay_hours": 48,
    },
)
