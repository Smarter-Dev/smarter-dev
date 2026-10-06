"""The bot must not stay up without a gateway (#94).

On 2026-10-06 Discord closed shard 0 with 4003 (Not authenticated). hikari
does not reconnect after that code, so the bot heard nothing for two hours,
while a ``pgrep`` liveness probe kept passing. Two things now catch it:
``run_bot`` exits when ``bot.join()`` returns or raises, and ``/live`` fails
while the shard is disconnected so the liveness probe restarts the pod.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import aiohttp
import hikari
import pytest
import yaml
from aiohttp import web
from hikari.impl import config as hikari_config
from hikari.impl import shard as shard_impl

from smarter_dev.bot import client
from smarter_dev.bot import leadership

REPO_ROOT = Path(__file__).resolve().parents[2]


# --- a real hikari shard against a fake gateway that closes with 4003 ---


class FakeGateway:
    """Says HELLO, answers IDENTIFY with READY, then closes with ``close_code``."""

    def __init__(self, close_code: int) -> None:
        self.close_code = close_code
        self.close_now = asyncio.Event()
        self.identifies = 0
        self.url = ""
        self._runner: web.AppRunner | None = None

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"op": 10, "d": {"heartbeat_interval": 45_000}})
        async for message in ws:
            payload = message.json()
            if payload["op"] != 2:
                continue
            self.identifies += 1
            await ws.send_json(
                {
                    "op": 0,
                    "t": "READY",
                    "s": 1,
                    "d": {
                        "v": 10,
                        "session_id": f"session-{self.identifies}",
                        "resume_gateway_url": self.url,
                        "user": {"id": "1", "username": "bot", "discriminator": "0"},
                        "guilds": [],
                    },
                }
            )
            await self.close_now.wait()
            await ws.close(code=self.close_code, message=b"Not authenticated.")
        return ws

    async def __aenter__(self) -> FakeGateway:
        app = web.Application()
        app.router.add_get("/", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.url = f"ws://127.0.0.1:{port}/"
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self._runner is not None
        await self._runner.cleanup()


def real_shard(url: str) -> shard_impl.GatewayShardImpl:
    return shard_impl.GatewayShardImpl(
        compression=None,
        intents=hikari.Intents.GUILDS,
        http_settings=hikari_config.HTTPSettings(),
        proxy_settings=hikari_config.ProxySettings(),
        event_manager=Mock(dispatch=AsyncMock(), consume_raw_event=Mock()),
        event_factory=Mock(),
        token="token",
        url=url,
    )


async def close_shard(shard: shard_impl.GatewayShardImpl) -> None:
    try:
        await shard.close()
    except Exception:  # noqa: BLE001 - the dead keep-alive task re-raises here
        pass


async def test_hikari_stops_the_shard_for_good_on_4003() -> None:
    """What happened in production, on the hikari version we pin.

    The shard stays ``is_alive`` (its keep-alive task object is still set),
    which is why ``is_alive`` alone could never tell; only ``is_connected``
    drops, and ``join()`` raises with the close code.
    """
    async with FakeGateway(close_code=4003) as gateway:
        shard = real_shard(gateway.url)
        await shard.start()
        bot = SimpleNamespace(shards={0: shard})
        assert client.gateway_connected(bot)

        gateway.close_now.set()
        with pytest.raises(hikari.errors.GatewayServerClosedConnectionError) as raised:
            await asyncio.wait_for(shard.join(), timeout=5)
        assert raised.value.code == 4003
        assert not raised.value.can_reconnect
        assert shard.is_alive
        assert not shard.is_connected
        assert not client.gateway_connected(bot)
        await asyncio.sleep(0.3)
        assert gateway.identifies == 1, "hikari must not have reconnected"
        await close_shard(shard)


async def test_hikari_reconnects_after_a_resumable_close() -> None:
    """Negative control: a close code hikari retries does not end ``join()``."""
    async with FakeGateway(close_code=4000) as gateway:
        shard = real_shard(gateway.url)
        await shard.start()
        join = asyncio.ensure_future(shard.join())
        gateway.close_now.set()
        await asyncio.sleep(0.5)
        assert not join.done(), "a retried close must not look like a dead gateway"
        assert gateway.identifies >= 1
        join.cancel()
        await close_shard(shard)


# --- /live and /ready ---


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fake_bot(*, connected: bool) -> Mock:
    shard = Mock(is_alive=True, is_connected=connected)
    return Mock(
        shards={0: shard},
        d={"_services": {"s": object()}},
        application=object(),
        is_alive=True,
    )


async def probe(
    bot: Mock, monkeypatch: pytest.MonkeyPatch, *, redis_ok: bool = True
) -> dict[str, int]:
    monkeypatch.setattr(leadership, "can_take_over", lambda: redis_ok)
    port = free_port()
    runner = await client.start_health_server(bot, port)
    try:
        async with aiohttp.ClientSession() as session:
            statuses = {}
            for path in ("/live", "/ready"):
                async with session.get(f"http://127.0.0.1:{port}{path}") as response:
                    statuses[path] = response.status
            return statuses
    finally:
        await runner.cleanup()


async def test_live_and_ready_pass_with_a_connected_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await probe(fake_bot(connected=True), monkeypatch) == {
        "/live": 200,
        "/ready": 200,
    }


async def test_live_and_ready_fail_with_a_disconnected_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await probe(fake_bot(connected=False), monkeypatch) == {
        "/live": 503,
        "/ready": 503,
    }


async def test_live_ignores_what_only_readiness_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis being down keeps the old bot in a roll; it must not restart this one."""
    statuses = await probe(fake_bot(connected=True), monkeypatch, redis_ok=False)
    assert statuses == {"/live": 200, "/ready": 503}


async def test_live_fails_on_the_real_shard_after_4003(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with FakeGateway(close_code=4003) as gateway:
        shard = real_shard(gateway.url)
        await shard.start()
        bot = Mock(
            shards={0: shard}, d={"_services": {"s": object()}}, application=object()
        )
        assert (await probe(bot, monkeypatch))["/live"] == 200
        gateway.close_now.set()
        with pytest.raises(hikari.errors.GatewayServerClosedConnectionError):
            await asyncio.wait_for(shard.join(), timeout=5)
        assert (await probe(bot, monkeypatch))["/live"] == 503
        await close_shard(shard)


# --- run_bot exits when the gateway is gone ---


class JoiningBot:
    """A bot whose ``join()`` is supplied: the real shard's, or a stand-in."""

    def __init__(self, join) -> None:
        self._join = join
        self.calls: list[str] = []

    def listen(self, *_args, **_kwargs):
        return lambda func: func

    async def start(self) -> None:
        self.calls.append("start")

    async def join(self) -> None:
        await self._join()

    async def close(self) -> None:
        self.calls.append("close")


class FakeCoordinator:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    async def run(self) -> None:
        await asyncio.Event().wait()

    async def stop_acting(self, _handled=()) -> None:
        self.calls.append("stop_acting")


def patch_run_bot(monkeypatch: pytest.MonkeyPatch, bot: JoiningBot) -> None:
    settings = SimpleNamespace(
        discord_bot_token="t",
        discord_application_id="1",
        bot_api_key="k",
        bot_health_port=0,
    )

    async def fake_setup(_bot) -> None:
        return None

    async def fake_health(_bot, _port):
        return Mock(cleanup=AsyncMock())

    async def fake_drain(_gate, _budget) -> None:
        bot.calls.append("drain")

    monkeypatch.setattr(client, "get_settings", lambda: settings)
    monkeypatch.setattr(client, "create_bot", lambda _settings: bot)
    monkeypatch.setattr(client, "setup_bot_services", fake_setup)
    monkeypatch.setattr(client, "load_plugins", lambda _bot: None)
    monkeypatch.setattr(client, "start_health_server", fake_health)
    monkeypatch.setattr(
        client,
        "create_coordination",
        lambda _bot, _settings: (FakeCoordinator(bot.calls), Mock(recent_handled=list)),
    )
    monkeypatch.setattr(client, "drain_accepted_work", fake_drain)


async def remove_handlers() -> None:
    loop = asyncio.get_running_loop()
    loop.remove_signal_handler(signal.SIGTERM)
    loop.remove_signal_handler(signal.SIGINT)


async def test_run_bot_exits_when_hikari_stops_the_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with FakeGateway(close_code=4003) as gateway:
        shard = real_shard(gateway.url)
        await shard.start()
        bot = JoiningBot(shard.join)
        patch_run_bot(monkeypatch, bot)
        gateway.close_now.set()
        try:
            with pytest.raises(hikari.errors.GatewayServerClosedConnectionError):
                await asyncio.wait_for(client.run_bot(), timeout=5)
        finally:
            await remove_handlers()
            await close_shard(shard)
    # Shut down the same way as on SIGTERM, then the error makes the exit non-zero.
    assert bot.calls == ["start", "stop_acting", "drain", "close"]


async def test_run_bot_exits_when_join_returns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def join_returns() -> None:
        return None

    bot = JoiningBot(join_returns)
    patch_run_bot(monkeypatch, bot)
    try:
        with pytest.raises(client.GatewayLostError):
            await asyncio.wait_for(client.run_bot(), timeout=5)
    finally:
        await remove_handlers()
    assert bot.calls[-1] == "close"


async def test_run_bot_keeps_running_while_the_gateway_is_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative control: a healthy gateway runs until SIGTERM, which is not an error."""

    async def join_forever() -> None:
        await asyncio.Event().wait()

    bot = JoiningBot(join_forever)
    patch_run_bot(monkeypatch, bot)

    async def sigterm_later() -> None:
        await asyncio.sleep(0.5)
        assert "close" not in bot.calls, "the bot stopped with its gateway up"
        os.kill(os.getpid(), signal.SIGTERM)

    killer = asyncio.create_task(sigterm_later())
    try:
        await asyncio.wait_for(client.run_bot(), timeout=5)
    finally:
        await killer
        await remove_handlers()
    assert bot.calls[-1] == "close"


# --- the manifest ---


def bot_container() -> dict:
    documents = yaml.safe_load_all((REPO_ROOT / "k8s" / "deploy-bot.yaml").read_text())
    (deployment,) = [doc for doc in documents if doc["kind"] == "Deployment"]
    return deployment["spec"]["template"]["spec"]["containers"][0]


def test_liveness_probe_is_http_on_the_gateway_endpoint() -> None:
    probe = bot_container()["livenessProbe"]
    assert "exec" not in probe, (
        "a process check passed for two hours with a dead gateway"
    )
    assert probe["httpGet"] == {"path": "/live", "port": 8080}
    window = probe["periodSeconds"] * probe["failureThreshold"]
    assert 60 <= window <= 120, "long enough for a resume, short enough to matter"


def test_startup_probe_waits_for_the_gateway() -> None:
    container = bot_container()
    probe = container["startupProbe"]
    assert "exec" not in probe
    assert probe["httpGet"] == {"path": "/live", "port": 8080}
    assert probe["periodSeconds"] * probe["failureThreshold"] >= 420, (
        "a throttled start took ~7 min"
    )


def test_readiness_probe_is_unchanged() -> None:
    probe = bot_container()["readinessProbe"]
    assert probe["httpGet"] == {"path": "/ready", "port": 8080}
    assert (probe["periodSeconds"], probe["failureThreshold"]) == (5, 2)
