"""Put a reference handler script live, through the bot API.

The scripts that run live on ``AdminHandler`` rows; ``scripts/handler_scripts``
holds reference copies of them (see the README there). Nothing applies a copy
on its own, and no page takes a pasted script — the Discord ``/adminhandler``
command hands a description to the author agent, which writes its own. What
exists is ``PUT /api/admin/handlers/{id}/script`` with the bot's API key, and
that is what this does: read the handler back, show the diff between its live
script and the file, and with ``--apply`` send the file as the script, then
read it back to check.

That route changes the script and nothing else — not the description, the
settings, the channel scope, nor whether the handler is enabled (a disabled
handler stays disabled, and this says so) — and it refuses when the script is
no longer the one that was read, so an edit made meanwhile is never written
over.

The API stores the script as given, so the lint runs here first; the judge does
not run on this path, and a review of the change stands in for it. After an
apply, the next fire shows on ``/admin/handlers?guild_id=…&admin=1`` as a
``HandlerRun`` with outcome ``ok`` or an error.

    BOT_API_KEY=… .venv/bin/python scripts/apply_handler_script.py \\
        --guild-id 123 --handler scam-banner \\
        --file scripts/handler_scripts/scam-banner.monty [--apply]

``API_BASE_URL`` (default ``http://localhost:8000/api``) names the website.
The key is read from the environment and never printed: error output carries
the request and status, never a response body.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import sys
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path

import httpx

from smarter_dev.web.handler_lint import lint_script


class ApplyError(Exception):
    """A reason the script was not applied; the message is for the operator."""


@dataclass(frozen=True)
class Plan:
    """What an apply would send, worked out from the live handler and the file."""

    handler_id: str
    enabled: bool
    body: dict
    diff: str

    @property
    def changed(self) -> bool:
        return bool(self.diff)


def plan(handlers: list[dict], name: str, script: str) -> Plan:
    """The write for ``name`` from the live list, or why there is none.

    ``handlers`` is the ``include_scripts`` list from the API. The body names
    the live script as what is expected, so the API refuses the write if
    the script has moved since.
    """
    reason = lint_script(script)
    if reason is not None:
        raise ApplyError(f"the file does not pass the handler lint: {reason}")
    matches = [h for h in handlers if h.get("name") == name]
    if not matches:
        names = ", ".join(sorted(h.get("name", "?") for h in handlers)) or "none"
        raise ApplyError(
            f"no admin handler named {name!r} in this guild (have: {names})"
        )
    if len(matches) > 1:
        raise ApplyError(
            f"{len(matches)} admin handlers named {name!r}; refusing to pick"
        )
    live = matches[0]
    diff = "".join(
        difflib.unified_diff(
            live["script"].splitlines(keepends=True),
            script.splitlines(keepends=True),
            fromfile=f"live:{name}",
            tofile="file",
        )
    )
    return Plan(
        handler_id=live["handler_id"],
        enabled=bool(live.get("enabled", True)),
        body={"script": script, "expected_script": live["script"]},
        diff=diff,
    )


async def fetch_handlers(client: httpx.AsyncClient, guild_id: str) -> list[dict]:
    response = await client.get(
        "/admin/handlers", params={"guild_id": guild_id, "include_scripts": "true"}
    )
    response.raise_for_status()
    return response.json()


async def apply_plan(client: httpx.AsyncClient, guild_id: str, the_plan: Plan) -> None:
    """Send the script, then read the handler back and check it took."""
    response = await client.put(
        f"/admin/handlers/{the_plan.handler_id}/script", json=the_plan.body
    )
    if response.status_code == 409:
        raise ApplyError(
            "the handler's script changed after it was read; nothing was written. "
            "Run again to see the new diff"
        )
    response.raise_for_status()
    after = await fetch_handlers(client, guild_id)
    stored = next(
        (h["script"] for h in after if h["handler_id"] == the_plan.handler_id), None
    )
    if stored != the_plan.body["script"]:
        raise ApplyError(
            "the API accepted the write but the script read back differs; "
            "check the handler on /admin/handlers before relying on it"
        )


async def run(args: argparse.Namespace) -> int:
    key = os.environ.get("BOT_API_KEY", "")
    if not key:
        print("BOT_API_KEY is not set", file=sys.stderr)
        return 2
    script = Path(args.file).read_text(encoding="utf-8")
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    ) as client:
        the_plan = plan(
            await fetch_handlers(client, args.guild_id), args.handler, script
        )
        if not the_plan.enabled:
            print(
                f"note: {args.handler} is disabled and stays disabled; the script "
                "will not fire until the handler is enabled"
            )
        if not the_plan.changed:
            print(f"{args.handler} ({the_plan.handler_id}) already has this script")
            return 0
        print(the_plan.diff, end="")
        if not args.apply:
            print(
                f"\nnot applied: run again with --apply to write {args.file} to {args.handler}"
            )
            return 0
        await apply_plan(client, args.guild_id, the_plan)
    print(
        f"applied {args.file} to {args.handler} ({the_plan.handler_id}); the next fire "
        f"shows on /admin/handlers?guild_id={args.guild_id}&admin=1"
    )
    return 0


def _status_line(status_code: int) -> str:
    try:
        return f"{status_code} {HTTPStatus(status_code).phrase}"
    except ValueError:
        return str(status_code)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--guild-id", required=True)
    parser.add_argument("--handler", required=True, help="the admin handler's name")
    parser.add_argument("--file", required=True, help="the .monty reference copy")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("API_BASE_URL", "http://localhost:8000/api"),
    )
    parser.add_argument(
        "--apply", action="store_true", help="write it; default shows the diff"
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run(args))
    except ApplyError as error:
        print(str(error), file=sys.stderr)
        return 1
    except httpx.HTTPStatusError as error:
        # The status and the request, nothing the server wrote: a reflected
        # header in the body or the reason phrase would put the key on the
        # terminal. The phrase printed is this machine's for the code.
        print(
            f"{error.request.method} {error.request.url.path} -> "
            f"{_status_line(error.response.status_code)}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
