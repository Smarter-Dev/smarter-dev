"""Custom Skrift role definitions for Smarter Dev.

Imported at controller load time so roles are registered before
the database sync occurs during app startup.
"""

from skrift.auth import register_role

# ── Sudo offerings ────────────────────────────────────────────

register_role(
    "sudo-hacker",
    "use-scan",
    "use-deep-scan",
    "view-answer-history",
    display_name="sudo Hacker",
    description="Hacker membership — every RunHacks challenge + Scan tools",
)

register_role(
    "sudo-founder",
    "use-scan",
    "use-deep-scan",
    "view-answer-history",
    display_name="sudo Founder",
    description="Founder membership — everything in Hacker plus the inside seat",
)

# ── Member ────────────────────────────────────────────────────

register_role(
    "member",
    "use-scan",
    display_name="Member",
    description="Community member with basic Scan access",
)

# ── Quests ───────────────────────────────────────────────────

register_role(
    "quests-manager",
    "manage-quests",
    display_name="Quests Manager",
    description="Can create, edit, schedule, and delete quests",
)

# ── Bot service key ──────────────────────────────────────────
# Permissions granted to the Discord bot's Skrift service key. The native
# ``/api`` controllers (docs/v2/legacy-sunset/04-api-rewrite.md) guard on
# ``bot-api``; admin-ish write paths additionally require ``bot-api-admin`` so a
# future narrow key can be minted without code change. The phase-01 runbook
# mints the key with these scoped_permissions.

register_role(
    "bot-service",
    "bot-api",
    "bot-api-admin",
    display_name="Bot Service",
    description="Discord bot service account — full native bot API access",
)
