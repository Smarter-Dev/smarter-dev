"""Putting a reference handler script live through the bot API.

The script is the one way a reviewed ``.monty`` copy reaches the handler's
row: read the handler back, diff, send the file through the script-only
route, read it back. These tests run it against a fake of the two routes that
behaves as the real ones do: the script route refuses a stale expectation and
touches nothing but the script.
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
KEY = "sk_test_secret_key_value"


def _api(rows: list[dict], *, between=None) -> tuple[httpx.MockTransport, list]:
    """The list and script routes over ``rows``; every write is recorded.

    ``between`` runs once, after the first list and before the write: the
    edit somebody else makes while this script is looking at the diff.
    """
    writes: list = []
    gets: list = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/admin/handlers":
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            assert request.url.params["include_scripts"] == "true"
            gets.append(1)
            response = httpx.Response(200, json=[dict(r) for r in rows])
            if between is not None and len(gets) == 1:
                between()
            return response
        if request.method == "PUT" and request.url.path.endswith("/script"):
            handler_id = request.url.path.rsplit("/", 2)[1]
            body = json.loads(request.content)
            assert set(body) == {"script", "expected_script"}
            writes.append((handler_id, body))
            for row in rows:
                if row["handler_id"] == handler_id:
                    if row["script"] != body["expected_script"]:
                        return httpx.Response(
                            409, json={"detail": "changed since read"}
                        )
                    row["script"] = body["script"]
                    return httpx.Response(
                        200, json={k: v for k, v in row.items() if k != "script"}
                    )
            return httpx.Response(404, json={"detail": "admin handler not found"})
        if request.method == "PUT":
            raise AssertionError("the full update route must not be used")
        return httpx.Response(404)

    return httpx.MockTransport(handle), writes


def _client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://web/api",
        headers={"Authorization": f"Bearer {KEY}"},
    )


def _with_transport(transport: httpx.MockTransport):
    """An ``AsyncClient.__init__`` that routes to the fake API."""
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = transport
        real_init(self, *args, **kwargs)

    return init


def _argv(file, *extra) -> list[str]:
    return [
        "--guild-id", "G1", "--handler", "scam-banner",
        "--file", str(file), "--base-url", "http://web/api", *extra,
    ]  # fmt: skip


@pytest.fixture
def script_file(tmp_path):
    file = tmp_path / "scam-banner.monty"
    file.write_text(NEW_SCRIPT)
    return file


# -- the plan -------------------------------------------------------------------


def test_the_plan_sends_the_script_and_what_it_expects_to_replace():
    the_plan = plan([LIVE], "scam-banner", NEW_SCRIPT)

    assert the_plan.handler_id == "h-1"
    assert the_plan.body == {"script": NEW_SCRIPT, "expected_script": LIVE["script"]}
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


# -- the write ------------------------------------------------------------------


async def test_apply_writes_the_script_and_reads_it_back():
    rows = [dict(LIVE)]
    transport, writes = _api(rows)
    async with _client(transport) as client:
        the_plan = plan(await fetch_handlers(client, "G1"), "scam-banner", NEW_SCRIPT)
        await apply_plan(client, "G1", the_plan)

    assert writes == [("h-1", the_plan.body)]
    assert rows[0]["script"] == NEW_SCRIPT


async def test_an_edit_made_meanwhile_to_anything_else_survives():
    # /adminhandler changed the description and scope while the diff was
    # on screen. The script-only route leaves them as they now are.
    rows = [dict(LIVE)]

    def edit():
        rows[0]["description"] = "Bans scammers, politely"
        rows[0]["channel_ids"] = ["C9"]
        rows[0]["settings"] = {"bot_optin": True}

    transport, _writes = _api(rows, between=edit)
    async with _client(transport) as client:
        the_plan = plan(await fetch_handlers(client, "G1"), "scam-banner", NEW_SCRIPT)
        await apply_plan(client, "G1", the_plan)

    assert rows[0]["script"] == NEW_SCRIPT
    assert rows[0]["description"] == "Bans scammers, politely"
    assert rows[0]["channel_ids"] == ["C9"]
    assert rows[0]["settings"] == {"bot_optin": True}


async def test_a_script_edited_meanwhile_is_not_written_over():
    rows = [dict(LIVE)]

    def edit():
        rows[0]["script"] = 'await send_message("theirs")\n'

    transport, writes = _api(rows, between=edit)
    async with _client(transport) as client:
        the_plan = plan(await fetch_handlers(client, "G1"), "scam-banner", NEW_SCRIPT)
        with pytest.raises(ApplyError, match="changed after it was read"):
            await apply_plan(client, "G1", the_plan)

    assert len(writes) == 1
    assert rows[0]["script"] == 'await send_message("theirs")\n'


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
        headers={"Authorization": f"Bearer {KEY}"},
    ) as client:
        the_plan = plan(await fetch_handlers(client, "G1"), "scam-banner", NEW_SCRIPT)
        with pytest.raises(ApplyError, match="read back differs"):
            await apply_plan(client, "G1", the_plan)

    assert rows[0]["script"] == LIVE["script"]


# -- the command ----------------------------------------------------------------


def test_without_apply_the_diff_is_shown_and_nothing_is_written(
    script_file, monkeypatch, capsys
):
    rows = [dict(LIVE)]
    transport, writes = _api(rows)
    monkeypatch.setenv("BOT_API_KEY", KEY)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _with_transport(transport))

    code = main(_argv(script_file))

    out = capsys.readouterr().out
    assert code == 0
    assert '+await send_message("new")' in out
    assert "not applied" in out
    assert writes == []


def test_with_apply_the_script_is_written(script_file, monkeypatch, capsys):
    rows = [dict(LIVE)]
    transport, writes = _api(rows)
    monkeypatch.setenv("BOT_API_KEY", KEY)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _with_transport(transport))

    code = main(_argv(script_file, "--apply"))

    out = capsys.readouterr().out
    assert code == 0
    assert "applied" in out and "admin=1" in out
    assert "note:" not in out
    assert len(writes) == 1
    assert rows[0]["script"] == NEW_SCRIPT


def test_a_disabled_handler_stays_disabled_and_the_operator_is_told(
    script_file, monkeypatch, capsys
):
    rows = [{**LIVE, "enabled": False}]
    transport, _writes = _api(rows)
    monkeypatch.setenv("BOT_API_KEY", KEY)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _with_transport(transport))

    code = main(_argv(script_file, "--apply"))

    out = capsys.readouterr().out
    assert code == 0
    assert "is disabled and stays disabled" in out
    assert rows[0]["enabled"] is False
    assert rows[0]["script"] == NEW_SCRIPT


def test_a_stale_script_exits_with_the_reason(script_file, monkeypatch, capsys):
    rows = [dict(LIVE)]

    def edit():
        rows[0]["script"] = 'await send_message("theirs")\n'

    transport, _writes = _api(rows, between=edit)
    monkeypatch.setenv("BOT_API_KEY", KEY)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _with_transport(transport))

    code = main(_argv(script_file, "--apply"))

    assert code == 1
    assert "changed after it was read" in capsys.readouterr().err


def test_an_error_response_never_puts_the_key_on_the_terminal(
    script_file, monkeypatch, capsys
):
    # A proxy or the API reflecting the Authorization header in its error.
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, text=f"Rejected credential: {request.headers['Authorization']}"
        )

    monkeypatch.setenv("BOT_API_KEY", KEY)
    monkeypatch.setattr(
        httpx.AsyncClient, "__init__", _with_transport(httpx.MockTransport(handle))
    )

    code = main(_argv(script_file, "--apply"))

    captured = capsys.readouterr()
    assert code == 1
    assert "GET /api/admin/handlers -> 401" in captured.err
    assert KEY not in captured.err and KEY not in captured.out


def test_a_reason_phrase_never_puts_the_key_on_the_terminal(
    script_file, monkeypatch, capsys
):
    # A proxy that writes the credential into the status line's reason
    # phrase rather than the body.
    def handle(request: httpx.Request) -> httpx.Response:
        phrase = f"Rejected credential {request.headers['Authorization']}"
        return httpx.Response(
            401, text="", extensions={"reason_phrase": phrase.encode()}
        )

    monkeypatch.setenv("BOT_API_KEY", KEY)
    monkeypatch.setattr(
        httpx.AsyncClient, "__init__", _with_transport(httpx.MockTransport(handle))
    )

    code = main(_argv(script_file, "--apply"))

    captured = capsys.readouterr()
    assert code == 1
    assert "GET /api/admin/handlers -> 401 Unauthorized" in captured.err
    assert KEY not in captured.err and KEY not in captured.out


def test_a_missing_key_stops_before_any_request(script_file, monkeypatch, capsys):
    monkeypatch.delenv("BOT_API_KEY", raising=False)

    code = main(
        ["--guild-id", "G1", "--handler", "scam-banner", "--file", str(script_file)]
    )

    assert code == 2
    assert "BOT_API_KEY" in capsys.readouterr().err
