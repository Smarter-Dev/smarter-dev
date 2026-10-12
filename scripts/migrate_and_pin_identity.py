#!/usr/bin/env python
"""Trusted deploy entrypoint: validate key presence, migrate, pin/check, then exit.

The Kubernetes Job must complete before any writer deployment is applied. Errors
are deliberately fixed strings: never print settings, keys or verifier digests.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


async def pin_identity_key(*, allow_initialize: bool = False) -> None:
    from sqlalchemy import func
    from sqlalchemy import select

    from smarter_dev.shared.database import get_session_maker
    from smarter_dev.web.site_identity import verify_identity_key
    from smarter_dev.web.site_identity_models import SiteIdentityKey

    async with get_session_maker()() as session:
        if not allow_initialize and await session.get(SiteIdentityKey, 1) is None:
            raise RuntimeError("Identity verifier has not been pinned")
        await verify_identity_key(session)
        if await session.scalar(select(func.count()).select_from(SiteIdentityKey)) != 1:
            raise RuntimeError("Invalid identity key registry")
        await session.commit()


async def assert_claims_clear() -> None:
    from sqlalchemy import text

    from smarter_dev.shared.database import get_session_maker

    async with get_session_maker()() as session:
        count = await session.scalar(
            text(
                "SELECT count(*) FROM skrift.worker_queue "
                "WHERE queue = 'agents' AND claim_token IS NOT NULL "
                "AND (claim_expires_at IS NULL OR claim_expires_at > clock_timestamp())"
            )
        )
        if count != 0:
            raise RuntimeError("Old agents claims have not cleared")


def run_migrations() -> None:
    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/migrate.py"),
            "--only",
            "skrift-main",
            "--only",
            "main",
        ],
        cwd=REPO_ROOT,
        check=True,
        # Migration exceptions can render settings; keep subprocess output out
        # of public deployment logs. Report only the fixed failure below.
        capture_output=True,
    )


def main() -> int:
    key = os.environ.get("SITE_IDENTITY_FENCE_KEY")
    if not key or len(key) < 32:
        print(
            "Identity rollout blocked: dedicated fence key is unavailable.",
            file=sys.stderr,
        )
        return 1
    phase = os.environ.get("SITE_IDENTITY_ROLLOUT_PHASE")
    if phase not in {"held", "complete"}:
        print(
            "Identity rollout blocked: operator transition gate is unavailable.",
            file=sys.stderr,
        )
        return 1
    try:
        if phase == "held":
            asyncio.run(assert_claims_clear())
        run_migrations()
        asyncio.run(pin_identity_key(allow_initialize=phase == "held"))
    except Exception:
        print(
            "Identity rollout blocked: migration or key pin/check failed.",
            file=sys.stderr,
        )
        return 1
    print("Migrations complete; identity fence key pin/check succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
