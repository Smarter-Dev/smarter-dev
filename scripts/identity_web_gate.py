"""Check the existing verifier before starting an HTTP deletion writer."""

import asyncio
import os
import sys

from scripts.identity_worker_gate import release_ready
from scripts.migrate_and_pin_identity import pin_identity_key


def main() -> int:
    try:
        asyncio.run(pin_identity_key())
        release_ready()  # require an explicit valid operator phase
        os.execv(
            "/app/.venv/bin/hypercorn",
            [
                "/app/.venv/bin/hypercorn",
                "smarter_dev.web.identity_rollout_app:app",
                "--bind",
                "0.0.0.0:8000",
            ],
        )
    except Exception:
        print("Identity web writer blocked: key check or gate failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
