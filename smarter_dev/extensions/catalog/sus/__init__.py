"""Sus extension.

One guild-wide admin handler on the ``message`` trigger, porting the legacy
beginner.py ``!sus`` joke command per
``docs/v2/feature-parity/member-lifecycle-and-role-automation.md`` §4.3.

``!sus`` grants the configured 🚨sus🚨 role and arms a one-shot ``schedule_timer``
to take it back a day later; the same handler serves that refire by branching on
``context["trigger_type"]``. A member holding the configured moderator role may
flag mentioned members instead of themselves — privilege is that role id, NOT a
Discord permission bit — and a non-privileged member who mentions people is
simply flagging themselves. ``!list_sus`` renders who is currently flagged.

The handler mutates the REAL role, so every other surface that reads it (the
moderation ``!history`` / ``!whois`` "Are They Sus?" field, role pickers, audit
logs) stays ground-truth accurate. The memory expiry map is a convenience index
used for exactly two things — rendering ``!list_sus`` and answering "is this
member already flagged?" so a re-sus does not extend the timer — and it is
pruned of past-due entries and trimmed to a hard entry cap on every fire, so it
stays far inside the 16 KB per-handler memory ceiling. Drift between the map and
the role (a moderator hand-removing the role, say) is therefore confined to
``!list_sus`` output and cannot mislead a moderation surface.

Installed guild-wide (``channel_scope == []``) so the command works anywhere; the
first thing the script does on a message fire is a cheap prefix guard, so
ordinary chatter costs a string comparison and nothing else — no reads, no
memory write.
"""

from __future__ import annotations

from smarter_dev.extensions.schema import ConfigField
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.extensions.schema import HandlerTemplate

MANIFEST = ExtensionManifest(
    slug="sus",
    title="Sus",
    summary=(
        "The !sus joke command: flag yourself (or, as a moderator, anyone you "
        "mention) with a sus role for a day. !list_sus shows who is currently "
        "flagged."
    ),
    version=1,
    config=[
        ConfigField(
            name="sus_role_id",
            type="role_id",
            label="Sus role",
            help=(
                "The role granted by !sus and removed automatically a day later. "
                "Make it cosmetic — the bot's top role must sit above it."
            ),
        ),
        ConfigField(
            name="moderator_role_id",
            type="role_id",
            label="Moderator role",
            help=(
                "Members holding this role may flag others by mentioning them. "
                "Everyone else can only flag themselves. This role is never "
                "granted or removed by the handler."
            ),
        ),
    ],
    handlers=[
        HandlerTemplate(
            key="sus",
            name="sus",
            trigger_type="message",
            description=(
                "Answers !sus by granting the sus role for a day (moderators may "
                "flag mentioned members) and !list_sus by listing who is flagged"
            ),
            # Only the sus role is ever granted; the moderator role is a
            # read-only privilege gate and is deliberately kept off the
            # host-enforced allowlist.
            settings={"allowed_role_ids": ["{{cfg.sus_role_id}}"]},
            script_file="sus.monty",
            # Empty scope = every channel: the joke is not channel-specific.
            channel_scope=[],
        ),
    ],
    example_config={
        "sus_role_id": "111111111111111111",
        "moderator_role_id": "222222222222222222",
    },
)
