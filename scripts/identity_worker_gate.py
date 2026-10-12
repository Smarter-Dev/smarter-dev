#!/usr/bin/env python
"""Verify the pinned key and wait for operator release before claiming any job."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from scripts.migrate_and_pin_identity import pin_identity_key

PHASE = Path("/var/run/site-identity-rollout/phase")
READY = Path("/tmp/site-identity-key-ready")


def release_ready() -> bool:
    # Missing/invalid control always fails closed; never default to consuming.
    phase = PHASE.read_text().strip()
    if phase not in {"held", "complete"}:
        raise RuntimeError("Invalid rollout phase")
    return phase == "complete"


def main() -> int:
    READY.unlink(missing_ok=True)
    try:
        asyncio.run(pin_identity_key())  # check existing pin; may not initialize
        release_ready()  # validate mounted control before declaring Ready
        READY.touch(mode=0o600)
        while not release_ready():
            time.sleep(1)
        if len(sys.argv) < 2:
            raise RuntimeError("Worker command required")
        os.execv(sys.argv[1], sys.argv[1:])
    except Exception:
        READY.unlink(missing_ok=True)
        print(
            "Identity worker blocked: key check or transition gate failed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
