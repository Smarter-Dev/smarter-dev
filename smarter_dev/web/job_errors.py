"""Keep message text out of the errors Skrift records for a failed job.

Skrift stores a failed job's ``str(exc)`` and full traceback in the job state,
its attempt history, the dead letter and the lifecycle event, and keeps them up
to 7 days. A handler fire holds a member's message while it runs, and an error
raised around it can quote that text: a database error lists the values it was
writing, a Discord API error the body it sent. So a fire's exception is
replaced at the job boundary by one that carries only the original's types and
frames.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from skrift.workers.runtime import PermanentFailure

from smarter_dev.shared.message_content import exception_trace


class RedactedJobError(Exception):
    """A job failure whose message is the original's types and frames, no text."""


class RedactedPermanentFailure(PermanentFailure):
    """A :class:`PermanentFailure` with its text removed; still never retried."""


@contextmanager
def redacted_job_errors() -> Iterator[None]:
    """Re-raise any exception from the block without its message text.

    The retry decision is kept: a permanent failure stays permanent, and every
    other exception is retried as before. Cancellation is not an ``Exception``
    and passes through untouched.
    """
    try:
        yield
    except PermanentFailure as exc:
        raise RedactedPermanentFailure(exception_trace(exc)) from None
    except Exception as exc:
        raise RedactedJobError(exception_trace(exc)) from None
