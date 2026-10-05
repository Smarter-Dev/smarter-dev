"""The retention classes ``docs/data-retention.md`` states, as numbers code uses.

A store may keep data for less than its class allows when its function needs
it; it never keeps it for longer. User content, the permanent stores and the
usage and audit records have no number to name here: they are kept until a
deletion request, kept, or kept without message text.
"""

from __future__ import annotations

from datetime import timedelta

# In-flight work: message text held while the bot works on it is deleted when
# the work finishes, and is never kept past this backstop.
IN_FLIGHT_MAX: timedelta = timedelta(hours=6)
IN_FLIGHT_MAX_MILLISECONDS: int = int(IN_FLIGHT_MAX.total_seconds() * 1000)

# Operational data: rate limits, caches, dedupe claims, sign-in sessions,
# failed-job records, logs and monitoring. A counter expires with its own
# window, well inside this.
OPERATIONAL_MAX: timedelta = timedelta(days=30)
