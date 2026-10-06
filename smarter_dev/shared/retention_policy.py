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

# The age at which a store the hourly retention job sweeps
# (k8s/cron-retention-sweep.yaml) makes in-flight text due. One hour inside
# IN_FLIGHT_MAX because the job runs at the top of each hour: text due just
# after a run is removed by the next, so nothing outlives IN_FLIGHT_MAX.
IN_FLIGHT_SWEEP_WINDOW: timedelta = IN_FLIGHT_MAX - timedelta(hours=1)

# Operational data: rate limits, caches, dedupe claims, sign-in sessions,
# failed-job records, logs and monitoring. A counter expires with its own
# window, well inside this.
OPERATIONAL_MAX: timedelta = timedelta(days=30)

# Agent working memory: the chat agent's history, notes and topic keys expire
# this long after their last write, and the proactive agent's history is
# folded to its memory note alone (no verbatim message) once it has not been
# written for this long. One number so the two agents cannot drift apart.
AGENT_VERBATIM_IDLE_WINDOW: timedelta = timedelta(hours=2)

# How often the bot looks for proactive histories past the idle window. The
# bound is the window plus one tick.
PROACTIVE_IDLE_SWEEP_TICK: timedelta = timedelta(minutes=1)
