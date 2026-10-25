"""Differential fixtures for the site's "Delete all searches" and its count.

Runs smarter-dev's own code over an edge corpus and prints JSON to stdout,
for the site repo to commit as fixtures:

- ``cases``: sets of a member's searches, each run through the real
  ``delete_all_searches`` (with the site's ``keep``) and ``count_searches``
  against a throwaway SQLite database, built the way the tests build one;
- ``messages``: what the account page says after deleting all searches
  (``account_controller.deleted_message``);
- ``copy``: the account page's "Your data" row for searches, rendered from the
  real template (``themes/smarterdev/templates/account/security.html``).

Runs are named by their index in the case, never by id, so the output is the
same on every run. Rerun from the repo root, with the project's environment::

    python scripts/parity/site_search_delete_all.py \\
        > scripts/parity/site_search_delete_all.out.json
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from jinja2 import ChoiceLoader  # noqa: E402
from jinja2 import DictLoader  # noqa: E402
from jinja2 import Environment  # noqa: E402
from jinja2 import FileSystemLoader  # noqa: E402
from skrift.db.base import Base as SkriftBase  # noqa: E402
from skrift.db.models.user import User  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from smarter_dev.shared.database import Base  # noqa: E402
from smarter_dev.shared.database import async_sessionmaker  # noqa: E402
from smarter_dev.web.account_controller import deleted_message  # noqa: E402
from smarter_dev.web.models import WebSearchRun  # noqa: E402
from smarter_dev.web.models import WorkDispatch  # noqa: E402
from smarter_dev.web.user_content import STALLED_AFTER  # noqa: E402
from smarter_dev.web.user_content import DeleteAllResult  # noqa: E402
from smarter_dev.web.user_content import count_searches  # noqa: E402
from smarter_dev.web.user_content import delete_all_searches  # noqa: E402

STALL = int(STALLED_AFTER.total_seconds() // 60)
RECENT = 0
JUST_RECENT = STALL - 1
JUST_STALLED = STALL + 1
LONG_AGO = 24 * 60
RUNNING = ("queued", "planning", "searching", "ranking", "answering")


def run(status: str, changed: int = RECENT, keep: bool = False) -> dict:
    return {"status": status, "changed_minutes_ago": changed, "keep": keep}


# ``foreign_keep``: ids sent in keep that are not the member's searches, one of
# another member's and one of no search at all.
CORPUS = [
    {"name": "empty", "runs": []},
    {"name": "empty_with_foreign_keep", "runs": [], "foreign_keep": True},
    {
        "name": "all_finished",
        "runs": [run("complete"), run("error"), run("complete", LONG_AGO)],
    },
    {"name": "all_recent_running", "runs": [run(status) for status in RUNNING]},
    {
        "name": "all_stalled_running",
        "runs": [run(status, JUST_STALLED) for status in RUNNING],
    },
    {
        "name": "running_either_side_of_stalled",
        "runs": [
            run("searching", JUST_RECENT),
            run("searching", JUST_STALLED),
            run("answering", LONG_AGO),
        ],
    },
    {"name": "error_only", "runs": [run("error"), run("error", LONG_AGO)]},
    {"name": "kept_finished", "runs": [run("complete", keep=True), run("complete")]},
    {"name": "kept_error", "runs": [run("error", keep=True)]},
    {"name": "kept_running", "runs": [run("searching", keep=True), run("complete")]},
    {
        "name": "kept_stalled",
        "runs": [
            run("searching", JUST_STALLED, keep=True),
            run("searching", STALL + 5),
        ],
    },
    {
        "name": "every_run_kept",
        "runs": [run("complete", keep=True), run("error", keep=True)],
    },
    {"name": "foreign_id_in_keep", "runs": [run("complete")], "foreign_keep": True},
    {
        "name": "mixed",
        "runs": [
            run("complete"),
            run("error"),
            run("planning", JUST_STALLED),
            run("ranking"),
            run("complete", keep=True),
            run("answering", keep=True),
            run("complete", LONG_AGO),
        ],
        "foreign_keep": True,
    },
]


async def _member(session) -> User:
    user = User(email=f"{uuid4().hex}@example.test", name="Member", is_active=True)
    session.add(user)
    await session.commit()
    return user


async def _search(session, owner_id, spec: dict) -> WebSearchRun:
    search = WebSearchRun(
        owner_user_id=owner_id,
        submission_key=uuid4().hex,
        request="parity corpus",
        status=spec["status"],
        queries=[],
        results=[],
        ranked=spec["status"] == "complete",
        usage={},
        version=1,
        attempt_count=1,
    )
    session.add(search)
    await session.flush()
    search.updated_at = datetime.now(UTC) - timedelta(
        minutes=spec["changed_minutes_ago"]
    )
    session.add(
        WorkDispatch(
            job_type="web_search.run",
            aggregate_id=search.id,
            payload={"search_id": str(search.id)},
            status="complete",
        )
    )
    await session.commit()
    return search


async def _case(maker, case: dict) -> dict:
    async with maker() as session:
        member = (await _member(session)).id
        other = (await _member(session)).id
        ids = [(await _search(session, member, spec)).id for spec in case["runs"]]
        keep = [rid for rid, spec in zip(ids, case["runs"]) if spec["keep"]]
        if case.get("foreign_keep"):
            keep += [(await _search(session, other, run("complete"))).id, uuid4()]
        before = await count_searches(session, member)
        result = await delete_all_searches(session, member, keep=keep)
        after = await count_searches(session, member)
        others = await count_searches(session, other)
        index = {rid: i for i, rid in enumerate(ids)}
        return {
            **case,
            "foreign_keep": bool(case.get("foreign_keep")),
            "count_before": before,
            "deleted": result.deleted,
            "running": result.running,
            "kept": len(result.kept),
            "kept_runs": sorted(index[rid] for rid in result.kept),
            "count_after": after,
            "other_member_count_after": others,
        }


async def _cases() -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp}/parity.db")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(
                    SkriftBase.metadata.create_all, tables=[User.__table__]
                )
            maker = async_sessionmaker(engine, expire_on_commit=False)
            return [await _case(maker, case) for case in CORPUS]
        finally:
            await engine.dispose()


def _messages() -> list[dict]:
    return [
        {
            "deleted": deleted,
            "running": running,
            "message": deleted_message(
                "search", DeleteAllResult(deleted=deleted, running=running)
            ),
        }
        for deleted in (0, 1, 2, 5)
        for running in (0, 1, 2)
    ]


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def _copy() -> list[dict]:
    """The searches row of "Your data", as the account page renders it."""
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader(
                    {
                        "account/_layout.html": "{% block account_content %}{% endblock %}"
                    }
                ),
                FileSystemLoader(REPO / "themes/smarterdev/templates"),
            ]
        )
    )
    env.globals.update(csp_nonce=lambda: "n", csrf_field=lambda: "")
    template = env.get_template("account/security.html")
    rows = []
    for count in (0, 1, 2, 5):
        html = template.render(
            sign_in_methods=["discord"],
            passkey_sign_in=False,
            passkey_available=False,
            linked_accounts=[],
            passkeys=[],
            your_data={"chats": 0, "questions": 0, "searches": count},
        )
        row = next(
            part
            for part in html.split('<div class="sec-item">')
            if re.search(r'sec-item-name">\s*Searches\s*<', part)
        )
        meta = re.search(r'sec-item-meta">(.*?)</div>', row, re.S)
        confirm = re.search(r"<p>(.*?)</p>", row, re.S)
        button = re.search(r'<button type="submit"[^>]*>(.*?)</button>', row, re.S)
        rows.append(
            {
                "count": count,
                "meta": _text(meta.group(1)),
                "offers_delete": "/account/data/searches/delete" in row,
                "confirm": _text(confirm.group(1)) if confirm else None,
                "button": _text(button.group(1)) if button else None,
            }
        )
    return rows


def main() -> None:
    fixtures = {
        "stalled_after_minutes": STALL,
        "cases": asyncio.run(_cases()),
        "messages": _messages(),
        "copy": _copy(),
    }
    json.dump(fixtures, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
