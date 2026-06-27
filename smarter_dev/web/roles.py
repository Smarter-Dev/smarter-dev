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
