"""Log a handled exception as its types and frames, never its text.

The bot reads members' messages and builds prompts from them, and any
exception raised along the way can quote one: a provider error echoing the
prompt, a Discord API error carrying the body it was sent, a validation error
quoting its input. ``logger.exception`` and ``exc_info=True`` print that
message, so on those paths a failure is logged with :func:`log_exception`
instead, which keeps what a reader needs to find the fault — every chained
exception's type and stack frames — and drops what it said.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from smarter_dev.shared.message_content import exception_trace


def log_exception(
    log: logging.Logger,
    message: str,
    *args: object,
    level: int = logging.ERROR,
    **kwargs: Any,
) -> None:
    """Log ``message`` with the exception being handled, minus its text.

    Call it where ``logger.exception`` would go. ``args`` format ``message``
    lazily, as with any logging call; the trace is appended on its own lines.
    Outside an ``except`` block it logs ``message`` alone.
    """
    error = sys.exc_info()[1]
    if error is None:
        log.log(level, message, *args, stacklevel=2, **kwargs)
    elif args:
        log.log(level, message + "\n%s", *args, exception_trace(error), stacklevel=2, **kwargs)
    else:
        log.log(level, "%s\n%s", message, exception_trace(error), stacklevel=2, **kwargs)
