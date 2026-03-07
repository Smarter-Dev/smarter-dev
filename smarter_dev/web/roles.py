"""Custom Skrift role definitions for Smarter Dev.

Imported at controller load time so roles are registered before
the database sync occurs during app startup.
"""

from skrift.auth import register_role

# ── Sudo tiers ────────────────────────────────────────────────

register_role(
    "sudo-r",
    "use-scan",
    "use-deep-scan",
    display_name="sudo r--",
    description="Read-only sudo access to Scan tools",
)

register_role(
    "sudo-rw",
    "use-scan",
    "use-deep-scan",
    display_name="sudo rw-",
    description="Read-write sudo access to Scan tools",
)

register_role(
    "sudo-rwx",
    "use-scan",
    "use-deep-scan",
    display_name="sudo rwx",
    description="Full sudo access to Scan tools",
)

# ── Member ────────────────────────────────────────────────────

register_role(
    "member",
    "use-scan",
    display_name="Member",
    description="Community member with basic Scan access",
)
