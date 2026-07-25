"""Edit Ping Catch extension.

One guild-wide admin handler on the ``message_edit`` trigger, closing the
edit-based mass-ping evasion route: a member posts something innocuous, waits
for the message to settle, then edits ``@everyone``/``@here`` into it. See
``docs/v2/feature-parity/automated-and-command-moderation.md`` §4.1 "Handler C".

``message_edit`` IS channel-keyed (unlike the ``member_*`` triggers), so the fire
has a home channel and the notice needs no channel constant — but the handler is
installed guild-wide, so ``channel_scope`` stays empty (empty ``channel_ids``
means every channel). The only config value is the staff role, which is baked in
as a literal at install time and joins ``author_has_manage_messages`` as the
second half of the staff exemption.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import ConfigField
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.extensions.schema import HandlerTemplate

MANIFEST = ExtensionManifest(
    slug="edit-ping-catch",
    title="Edit Ping Catch",
    summary=(
        "Catches members who edit @everyone or @here into an already-posted "
        "message: the edit is deleted and the author is told why. Staff are "
        "exempt."
    ),
    version=1,
    config=[
        ConfigField(
            name="staff_role_id",
            type="role_id",
            label="Staff role",
            help=(
                "Members holding this role are exempt, on top of anyone with "
                "Manage Messages. Pick your moderator/staff role — the people "
                "who are allowed to mass-mention."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="edit-ping-catch",
            name="edit-ping-catch",
            trigger_type="message_edit",
            description=(
                "Deletes an edit that introduces @everyone/@here from a "
                "non-staff member and tells the author why"
            ),
            script_file="edit_ping_catch.monty",
            settings={},
            # Empty scope = every channel: ping evasion is not channel-specific.
            channel_scope=[],
        ),
    ],
    example_config={"staff_role_id": "222222222222222222"},
)
