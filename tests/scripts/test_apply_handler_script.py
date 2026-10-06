"""Putting a reference handler script live through the bot API.

The script is the one way a reviewed ``.monty`` copy reaches the handler's
row: read the handler back, diff, PUT the file as its script, read it back.
These tests run it against a fake of the two API routes it uses.
"""

from __future__ import annotations

import json

import httpx
import pytest

from scripts.apply_handler_script import ApplyError
from scripts.apply_handler_script import apply_plan
from scripts.apply_handler_script import fetch_handlers
from scripts.apply_handler_script import main
from scripts.apply_handler_script import plan

LIVE = {
    "handler_id": "h-1",
    "guild_id": "G1",
    "name": "scam-banner",
    "trigger_type": "message",
    "channel_ids": ["C1", "C2"],
    "settings": {"bot_optin": False},
    "description": "Bans scammers",
    "enabled": True,
    "script": 'await send_message("old")\n',
}
NEW_SCRIPT = 'await send_message("new")\n'


def _api(rows: list[dict]) -> tuple[httpx.MockTransport, list]:
    """The list and update routes over ``rows``; every write is recorded."""
    writes: list = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/admin/handlers":
            assert request.headers["Authorization"] == "Bearer sk_test"
            assert request.url.params["include_scripts"] == "true"
            return httpx.Response(200, json=rows)
        if request.method == "PUT" and request.url.path.startswith(
            "/api/admin/handlers/"
        ):
            handler_id = request.url.path.rsplit("/", 1)[1]
            body = json.loads(request.content)
            writes.append((handler_id, body))
            for row in rows:
                if row["handler_id"] == handler_id:
                    row.update(body)
                    return httpx.Response(
                        200, json={k: v for k, v in row.items() if k != "script"}
                    )
            return httpx.Response(404, json={"detail": "admin handler not found"})
        return httpx.Response(404)

    return httpx.MockTransport(handle), writes


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://web/api",
        headers={"Authorization": "Bearer sk_test"},
    )


def test_the_plan_keeps_everything_but_the_script():
    the_plan = plan([LIVE], "scam-banner", NEW_SCRIPT)

    assert the_plan.handler_id == "h-1"
    assert the_plan.body == {
        "description": "Bans scammers",
        "script": NEW_SCRIPT,
        "settings": {"bot_optin": False},
        "channel_ids": ["C1", "C2"],
    }
    assert '-await send_message("old")' in the_plan.diff
    assert '+await send_message("new")' in the_plan.diff


def test_an_unchanged_script_has_nothing_to_apply():
    assert plan([LIVE], "scam-banner", LIVE["script"]).changed is False


def test_a_file_that_fails_the_lint_is_refused_before_any_request():
    blob = "QUJDREVG" * 30
    with pytest.raises(ApplyError, match="lint"):
        plan([LIVE], "scam-banner", f'data = "{blob}"\n')


def test_an_unknown_handler_name_is_refused_and_the_names_are_listed():
    with pytest.raises(
        ApplyError, match="no admin handler named 'scam-baner'.*scam-banner"
    ):
        plan([LIVE], "scam-baner", NEW_SCRIPT)


def test_two_handlers_with_one_name_are_not_guessed_between():
    with pytest.raises(ApplyError, match="2 admin handlers"):
        plan([LIVE, {**LIVE, "handler_id": "h-2"}], "scam-banner", NEW_SCRIPT)


async def test_apply_puts_the_script_and_reads_it_back():
    rows = [dict(LIVE)]
    transport, writes = _api(rows)
    async with _client(transport) as client:
        the_plan = plan(await fetch_handlers(client, "G1"), "scam-banner", NEW_SCRIPT)
        await apply_plan(client, "G1", the_plan)

    assert writes == [("h-1", the_plan.body)]
    assert rows[0]["script"] == NEW_SCRIPT


async def test_apply_fails_loudly_when_the_readback_differs():
    rows = [dict(LIVE)]
    transport, _writes = _api(rows)

    class _Silent(httpx.AsyncClient):
        async def put(self, url, **kwargs):
            # The API answered 200 and stored nothing.
            return httpx.Response(200, json={}, request=httpx.Request("PUT", url))

    async with _Silent(
        transport=transport,
        base_url="http://web/api",
        headers={"Authorization": "Bearer sk_test"},
    ) as client:
        the_plan = plan(await fetch_handlers(client, "G1"), "scam-banner", NEW_SCRIPT)
        with pytest.raises(ApplyError, match="read back differs"):
            await apply_plan(client, "G1", the_plan)

    assert rows[0]["script"] == LIVE["script"]


def test_without_apply_the_diff_is_shown_and_nothing_is_written(
    tmp_path, monkeypatch, capsys
):
    rows = [dict(LIVE)]
    transport, writes = _api(rows)
    monkeypatch.setenv("BOT_API_KEY", "sk_test")
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _with_transport(transport))
    file = tmp_path / "scam-banner.monty"
    file.write_text(NEW_SCRIPT)

    code = main(
        [
            "--guild-id",
            "G1",
            "--handler",
            "scam-banner",
            "--file",
            str(file),
            "--base-url",
            "http://web/api",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert '+await send_message("new")' in out
    assert "not applied" in out
    assert writes == []


def test_with_apply_the_script_is_written(tmp_path, monkeypatch, capsys):
    rows = [dict(LIVE)]
    transport, writes = _api(rows)
    monkeypatch.setenv("BOT_API_KEY", "sk_test")
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _with_transport(transport))
    file = tmp_path / "scam-banner.monty"
    file.write_text(NEW_SCRIPT)

    code = main(
        [
            "--guild-id",
            "G1",
            "--handler",
            "scam-banner",
            "--file",
            str(file),
            "--base-url",
            "http://web/api",
            "--apply",
        ]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "applied" in out and "admin=1" in out
    assert "sk_test" not in out
    assert len(writes) == 1
    assert rows[0]["script"] == NEW_SCRIPT


def test_a_missing_key_stops_before_any_request(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("BOT_API_KEY", raising=False)
    file = tmp_path / "x.monty"
    file.write_text(NEW_SCRIPT)

    code = main(["--guild-id", "G1", "--handler", "scam-banner", "--file", str(file)])

    assert code == 2
    assert "BOT_API_KEY" in capsys.readouterr().err


def _with_transport(transport: httpx.MockTransport):
    """An ``AsyncClient.__init__`` that routes to the fake API."""
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    return init
