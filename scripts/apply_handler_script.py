"""Put a reference handler script live, through the bot API.

The scripts that run live on ``AdminHandler`` rows; ``scripts/handler_scripts``
holds reference copies of them (see the README there). Nothing applies a copy
on its own, and no page takes a pasted script — the Discord ``/adminhandler``
command hands a description to the author agent, which writes its own. What
exists is ``PUT /api/admin/handlers/{id}`` with the bot's API key, and that is
what this does: read the handler back (name, description, settings, channel
scope), show the diff between its live script and the file, and with
``--apply`` write the file as the handler's script and read it back to check.

The API stores the script as given, so the lint runs here first; the judge does
not run on this path, and a review of the change stands in for it. After an
apply, the next fire shows on ``/admin/handlers?guild_id=…&admin=1`` as a
``HandlerRun`` with outcome ``ok`` or an error.

    BOT_API_KEY=… .venv/bin/python scripts/apply_handler_script.py \\
        --guild-id 123 --handler scam-banner \\
        --file scripts/handler_scripts/scam-banner.monty [--apply]

``API_BASE_URL`` (default ``http://localhost:8000/api``) names the website.
The key is read from the environment and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from smarter_dev.web.handler_lint import lint_script


class ApplyError(Exception):
    """A reason the script was not applied; the message is for the operator."""


@dataclass(frozen=True)
class Plan:
    """What an apply would send, worked out from the live handler and the file."""

    handler_id: str
    body: dict
    diff: str

    @property
    def changed(self) -> bool:
        return bool(self.diff)


def plan(handlers: list[dict], name: str, script: str) -> Plan:
    """The PUT for ``name`` from the live list, or why there is none.

    ``handlers`` is the ``include_scripts`` list from the API. The body keeps
    the handler's description, settings and channel scope as they are: the
    PUT overwrites all of them, and only the script is meant to change.
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
        body={
            "description": live["description"],
            "script": script,
            "settings": live.get("settings") or {},
            "channel_ids": list(live.get("channel_ids") or []),
        },
        diff=diff,
    )


async def fetch_handlers(client: httpx.AsyncClient, guild_id: str) -> list[dict]:
    response = await client.get(
        "/admin/handlers", params={"guild_id": guild_id, "include_scripts": "true"}
    )
    response.raise_for_status()
    return response.json()


async def apply_plan(client: httpx.AsyncClient, guild_id: str, the_plan: Plan) -> None:
    """Send the PUT, then read the handler back and check the script took."""
    response = await client.put(
        f"/admin/handlers/{the_plan.handler_id}", json=the_plan.body
    )
    response.raise_for_status()
    after = await fetch_handlers(client, guild_id)
    stored = next(
        (h["script"] for h in after if h["handler_id"] == the_plan.handler_id), None
    )
    if stored != the_plan.body["script"]:
        raise ApplyError(
            "the API accepted the PUT but the script read back differs; "
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
        print(
            f"{error.request.method} {error.request.url.path} -> {error.response.status_code}: "
            f"{error.response.text[:300]}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
