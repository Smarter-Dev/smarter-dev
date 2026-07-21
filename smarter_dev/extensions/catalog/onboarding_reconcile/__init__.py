"""Onboarding reconcile sweep extension.

A single daily ``schedule`` handler that recovers members stuck in the newcomer
(holding) role. The happy-path promotion is owned by the ``member_rules_accepted``
onboarding handler's ``schedule_timer`` two days after acceptance; this sweep is
the E6 downtime-recovery net for members whose promotion timer was lost or never
armed (the bot was down when it should have fired). Each daily run reads the
newcomer role's members via ``get_role_members`` and promotes any whose
``joined_at`` is older than the configured delay — add the full role, then remove
the newcomer role gated on the add succeeding. See
``docs/v2/feature-parity/member-lifecycle-and-role-automation.md`` (§4.1, E6).

Idempotent by construction: promoting an already-promoted member is a no-op (they
no longer hold the newcomer role, so they never appear in the sweep again), so the
handler is safe to run every day forever.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)

MANIFEST = ExtensionManifest(
    slug="onboarding-reconcile",
    title="Onboarding Reconcile Sweep",
    summary=(
        "A daily sweep that promotes members stuck in the newcomer role past the "
        "configured delay — the downtime-recovery net behind the timed onboarding "
        "promotion."
    ),
    version=1,
    config=[
        ConfigField(
            name="newcomer_role_id",
            type="role_id",
            label="Newcomer (holding) role",
            help=(
                "The role assigned on rules acceptance. Its members are swept "
                "daily; the sweep reads at most the first 200 holders."
            ),
        ),
        ConfigField(
            name="full_member_role_id",
            type="role_id",
            label="Full member role",
            help="Granted to a stuck newcomer once the delay has elapsed.",
        ),
        ConfigField(
            name="promotion_delay_hours",
            type="int",
            required=False,
            default=48,
            label="Promotion delay (hours)",
            help=(
                "Promote a newcomer whose join is older than this many hours. "
                "Match the timed onboarding handler's delay (48 = two days)."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="reconcile-sweep",
            name="onboarding-reconcile",
            trigger_type="schedule",
            description=(
                "Daily: promote members stuck in the newcomer role past the delay "
                "(add the full role, remove the newcomer role)"
            ),
            script_file="reconcile_sweep.monty",
            # Runs at 04:00 UTC (a low-traffic hour). allowed_role_ids is the
            # host-enforced grant allowlist read before the fire — it carries the
            # two role ids the script mutates. channel_scope stays empty: the
            # sweep sends no messages, so it needs no bound channel.
            settings={
                "daily_time": "04:00",
                "allowed_role_ids": [
                    "{{cfg.full_member_role_id}}",
                    "{{cfg.newcomer_role_id}}",
                ],
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
