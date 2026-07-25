"""Tests for the moderation wiring inside :mod:`smarter_dev.bot.client`.

These tests cover the integration point only: that the ``/history`` plugin is
loaded, that the content-filter and spam-engine listeners are registered on the
guild-message-create event in the right order, that one failing check cannot
prevent the other from running, and that the module no longer performs inline
imports inside its event listeners.

The moderation modules themselves are covered by ``content_filter_test.py`` and
``spam_engine_test.py``; here they are stubbed except in the two no-op tests
that deliberately exercise the real "no config row" path.
"""

from __future__ import annotations

import ast
import contextlib
import logging
import typing
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import Mock

import hikari
import pytest

from smarter_dev.bot import client
from smarter_dev.bot import content_filter
from smarter_dev.bot import spam_engine
from smarter_dev.web.models import ModerationFilterConfig

HISTORY_PLUGIN_PATH = "smarter_dev.bot.plugins.history"


class ListenerRecordingBot:
    """Minimal ``lightbulb.BotApp`` stand-in that records ``@bot.listen()``."""

    def __init__(self) -> None:
        self.registered_listeners: list = []
        self.rest = AsyncMock()

    def listen(self, *event_types):
        def register(callback):
            self.registered_listeners.append(callback)
            return callback

        return register

    def get_me(self):
        return Mock(id=999)


def listener_event_type(callback) -> type:
    """The hikari event type a ``@bot.listen()`` callback is annotated with.

    ``client.py`` uses postponed annotations, so the raw ``__annotations__``
    entry is a string; lightbulb resolves it the same way this does.
    """
    return typing.get_type_hints(callback)["event"]


def make_plugin_loading_bot() -> Mock:
    """A bot stand-in whose ``d`` mapping ``load_plugins`` can inspect."""
    bot = Mock()
    bot.d = {}
    return bot


def make_guild_message_event(
    *,
    content: str = "hello world",
    is_bot: bool = False,
    guild_id: int | None = 111,
) -> Mock:
    """Build a stand-in ``hikari.GuildMessageCreateEvent``."""
    event = Mock()
    event.is_bot = is_bot
    event.guild_id = guild_id
    event.channel_id = 222
    event.message_id = 333
    event.content = content
    event.author = Mock(id=444, is_bot=is_bot)
    event.member = None
    event.message = Mock(id=333, content=content, attachments=[])
    event.get_guild = Mock(return_value=None)
    event.get_channel = Mock(return_value=None)
    return event


def client_source_tree() -> ast.Module:
    """Parse client.py so structural guarantees can be asserted on it."""
    return ast.parse(Path(client.__file__).read_text(encoding="utf-8"))


def handler_only_logs(handler: ast.ExceptHandler) -> bool:
    """Whether an ``except`` block does nothing but emit log lines."""
    return all(
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Attribute)
        and isinstance(statement.value.func.value, ast.Name)
        and statement.value.func.value.id == "logger"
        for statement in handler.body
    )


def find_function_node(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not defined in client.py")


@pytest.fixture
def recording_bot() -> ListenerRecordingBot:
    return ListenerRecordingBot()


def listeners_for_event(
    recording_bot: ListenerRecordingBot, event_type: type
) -> list:
    """Every registered listener annotated for ``event_type``."""
    return [
        listener
        for listener in recording_bot.registered_listeners
        if listener_event_type(listener) is event_type
    ]


@pytest.fixture
def moderation_listener(recording_bot: ListenerRecordingBot):
    """The single registered guild-message moderation listener."""
    client.register_message_moderation_listeners(recording_bot)
    message_listeners = listeners_for_event(
        recording_bot, hikari.GuildMessageCreateEvent
    )
    assert len(message_listeners) == 1
    return message_listeners[0]


@pytest.fixture
def guild_leave_listener(recording_bot: ListenerRecordingBot):
    """The registered guild-leave listener that releases spam-engine state."""
    client.register_message_moderation_listeners(recording_bot)
    leave_listeners = listeners_for_event(recording_bot, hikari.GuildLeaveEvent)
    assert len(leave_listeners) == 1
    return leave_listeners[0]


class TestHistoryPluginLoading:
    """The ``/history`` plugin is loaded alongside warn and purge."""

    def test_load_plugins_loads_the_history_plugin(self) -> None:
        bot = make_plugin_loading_bot()
        client.load_plugins(bot)
        loaded = [call.args[0] for call in bot.load_extensions.call_args_list]
        assert HISTORY_PLUGIN_PATH in loaded

    def test_history_plugin_is_loaded_next_to_warn_and_purge(self) -> None:
        bot = make_plugin_loading_bot()
        client.load_plugins(bot)
        loaded = [call.args[0] for call in bot.load_extensions.call_args_list]
        assert "smarter_dev.bot.plugins.warn" in loaded
        assert "smarter_dev.bot.plugins.purge" in loaded
        assert loaded.index(HISTORY_PLUGIN_PATH) > loaded.index(
            "smarter_dev.bot.plugins.purge"
        )

    def test_history_plugin_module_exposes_load_and_unload(self) -> None:
        history_module = __import__(HISTORY_PLUGIN_PATH, fromlist=["load", "unload"])
        assert callable(history_module.load)
        assert callable(history_module.unload)


class TestModerationListenerRegistration:
    """The moderation checks are wired onto GuildMessageCreateEvent."""

    def test_registers_a_single_guild_message_create_listener(
        self, recording_bot: ListenerRecordingBot
    ) -> None:
        client.register_message_moderation_listeners(recording_bot)

        message_listeners = listeners_for_event(
            recording_bot, hikari.GuildMessageCreateEvent
        )
        assert len(message_listeners) == 1

    def test_run_bot_registers_the_moderation_listeners(self) -> None:
        run_bot_node = find_function_node(client_source_tree(), "run_bot")
        called_names = {
            node.func.id
            for node in ast.walk(run_bot_node)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "register_message_moderation_listeners" in called_names


class TestModerationListenerOrdering:
    """Content filters run before the spam engine, and both always run."""

    async def test_content_filter_runs_before_spam_engine(
        self, monkeypatch: pytest.MonkeyPatch, moderation_listener
    ) -> None:
        call_order: list[str] = []

        async def record_content_filter(bot, event):
            call_order.append("content_filter")
            return Mock(message_deleted=False)

        async def record_spam_engine(bot, event):
            call_order.append("spam_engine")
            return Mock()

        monkeypatch.setattr(client, "check_content_filters", record_content_filter)
        monkeypatch.setattr(client, "check_spam_engine", record_spam_engine)

        await moderation_listener(make_guild_message_event())

        assert call_order == ["content_filter", "spam_engine"]

    async def test_spam_engine_still_runs_when_content_filter_deleted_message(
        self, monkeypatch: pytest.MonkeyPatch, moderation_listener
    ) -> None:
        deleting_content_filter = AsyncMock(return_value=Mock(message_deleted=True))
        spam_check = AsyncMock()
        monkeypatch.setattr(client, "check_content_filters", deleting_content_filter)
        monkeypatch.setattr(client, "check_spam_engine", spam_check)

        await moderation_listener(make_guild_message_event())

        spam_check.assert_awaited_once()

    async def test_spam_engine_runs_when_content_filter_fails(
        self, monkeypatch: pytest.MonkeyPatch, moderation_listener
    ) -> None:
        failing_content_filter = AsyncMock(side_effect=RuntimeError("boom"))
        spam_check = AsyncMock()
        monkeypatch.setattr(client, "check_content_filters", failing_content_filter)
        monkeypatch.setattr(client, "check_spam_engine", spam_check)

        with pytest.raises(ExceptionGroup):
            await moderation_listener(make_guild_message_event())

        spam_check.assert_awaited_once()

    async def test_content_filter_runs_before_a_failing_spam_engine_surfaces(
        self, monkeypatch: pytest.MonkeyPatch, moderation_listener
    ) -> None:
        content_check = AsyncMock(return_value=Mock(message_deleted=False))
        monkeypatch.setattr(client, "check_content_filters", content_check)
        monkeypatch.setattr(
            client, "check_spam_engine", AsyncMock(side_effect=RuntimeError("boom"))
        )

        with pytest.raises(ExceptionGroup):
            await moderation_listener(make_guild_message_event())

        content_check.assert_awaited_once()


class TestUnexpectedModerationFailuresAreLoud:
    """A bug in a moderation stage must never be swallowed into a log line.

    Hikari's ``EventManagerBase._invoke_callback`` catches ``Exception`` out of a
    listener, logs it at ERROR with the traceback and dispatches an
    ``ExceptionEvent``; it neither kills the gateway connection nor stops the
    other listeners registered for the same event. Letting an unexpected
    exception propagate is therefore both safe and strictly louder than
    swallowing it, so the wiring only has to add the guild/channel/message
    context hikari cannot know about.
    """

    async def test_unexpected_content_filter_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch, moderation_listener
    ) -> None:
        misconfiguration = ValueError("mute duration must be positive")
        monkeypatch.setattr(
            client, "check_content_filters", AsyncMock(side_effect=misconfiguration)
        )
        monkeypatch.setattr(client, "check_spam_engine", AsyncMock())

        with pytest.raises(ExceptionGroup) as raised:
            await moderation_listener(make_guild_message_event())

        assert raised.value.exceptions == (misconfiguration,)

    async def test_unexpected_spam_engine_failure_propagates(
        self, monkeypatch: pytest.MonkeyPatch, moderation_listener
    ) -> None:
        misconfiguration = ValueError("mute duration must be positive")
        monkeypatch.setattr(
            client,
            "check_content_filters",
            AsyncMock(return_value=Mock(message_deleted=False)),
        )
        monkeypatch.setattr(
            client, "check_spam_engine", AsyncMock(side_effect=misconfiguration)
        )

        with pytest.raises(ExceptionGroup) as raised:
            await moderation_listener(make_guild_message_event())

        assert raised.value.exceptions == (misconfiguration,)

    async def test_both_stage_failures_are_reported_together(
        self, monkeypatch: pytest.MonkeyPatch, moderation_listener
    ) -> None:
        content_failure = ValueError("bad filter pattern")
        spam_failure = ValueError("bad mute duration")
        monkeypatch.setattr(
            client, "check_content_filters", AsyncMock(side_effect=content_failure)
        )
        monkeypatch.setattr(
            client, "check_spam_engine", AsyncMock(side_effect=spam_failure)
        )

        with pytest.raises(ExceptionGroup) as raised:
            await moderation_listener(make_guild_message_event())

        assert raised.value.exceptions == (content_failure, spam_failure)

    async def test_unexpected_failure_is_logged_with_message_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        moderation_listener,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            client, "check_content_filters", AsyncMock(side_effect=ValueError("boom"))
        )
        monkeypatch.setattr(client, "check_spam_engine", AsyncMock())

        with caplog.at_level(logging.ERROR, logger=client.logger.name):
            with pytest.raises(ExceptionGroup):
                await moderation_listener(make_guild_message_event())

        failure_records = [
            record for record in caplog.records if record.levelno >= logging.ERROR
        ]
        assert failure_records, "the unexpected failure was not logged"
        logged = failure_records[0]
        assert logged.exc_info is not None, "the traceback was not logged"
        assert "111" in logged.getMessage()
        assert "222" in logged.getMessage()
        assert "333" in logged.getMessage()


class TestExpectedDiscordFailuresStayContained:
    """Discord being unreachable or refusing a call is an operational outcome."""

    async def test_discord_http_error_does_not_propagate(
        self, monkeypatch: pytest.MonkeyPatch, moderation_listener
    ) -> None:
        spam_check = AsyncMock()
        monkeypatch.setattr(
            client,
            "check_content_filters",
            AsyncMock(side_effect=hikari.HTTPError("connection reset")),
        )
        monkeypatch.setattr(client, "check_spam_engine", spam_check)

        await moderation_listener(make_guild_message_event())

        spam_check.assert_awaited_once()

    async def test_discord_http_error_is_logged_with_message_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        moderation_listener,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(
            client,
            "check_content_filters",
            AsyncMock(return_value=Mock(message_deleted=False)),
        )
        monkeypatch.setattr(
            client,
            "check_spam_engine",
            AsyncMock(side_effect=hikari.HTTPError("connection reset")),
        )

        with caplog.at_level(logging.WARNING, logger=client.logger.name):
            await moderation_listener(make_guild_message_event())

        assert any(
            "333" in record.getMessage() and "connection reset" in record.getMessage()
            for record in caplog.records
        )


class TestModerationListenerSkips:
    """Messages the moderation stack must never look at."""

    @pytest.mark.parametrize(
        ("is_bot", "guild_id"),
        [(True, 111), (False, None)],
        ids=["bot_author", "no_guild"],
    )
    async def test_listener_skips_bot_and_non_guild_messages(
        self,
        monkeypatch: pytest.MonkeyPatch,
        moderation_listener,
        is_bot: bool,
        guild_id: int | None,
    ) -> None:
        content_check = AsyncMock()
        spam_check = AsyncMock()
        monkeypatch.setattr(client, "check_content_filters", content_check)
        monkeypatch.setattr(client, "check_spam_engine", spam_check)

        await moderation_listener(
            make_guild_message_event(is_bot=is_bot, guild_id=guild_id)
        )

        content_check.assert_not_awaited()
        spam_check.assert_not_awaited()


class TestModerationListenerIsInertWithoutConfig:
    """With no config row both real checks are no-ops that touch no REST call."""

    async def test_no_config_row_produces_no_discord_calls(
        self, monkeypatch: pytest.MonkeyPatch, recording_bot: ListenerRecordingBot
    ) -> None:
        @contextlib.asynccontextmanager
        async def fake_db_session_context():
            yield Mock()

        missing_content_filter_config = AsyncMock(return_value=None)
        missing_spam_engine_settings = AsyncMock(return_value=None)
        monkeypatch.setattr(
            content_filter, "get_db_session_context", fake_db_session_context
        )
        monkeypatch.setattr(
            content_filter.moderation_filter_config_ops,
            "get_config",
            missing_content_filter_config,
        )
        monkeypatch.setattr(
            spam_engine, "load_spam_engine_settings", missing_spam_engine_settings
        )
        client.register_message_moderation_listeners(recording_bot)

        await recording_bot.registered_listeners[0](make_guild_message_event())

        # Both real checks were reached and both bailed out before Discord.
        missing_content_filter_config.assert_awaited_once()
        missing_spam_engine_settings.assert_awaited_once()
        assert recording_bot.rest.mock_calls == []


class TestNoInlineImportsInListeners:
    """The known inline-import debt in client.py's listeners is paid off."""

    @pytest.mark.parametrize(
        "checker_name",
        [
            "check_attachment_filter",
            "check_content_filters",
            "check_spam_engine",
        ],
    )
    def test_moderation_checks_are_imported_at_module_top(
        self, checker_name: str
    ) -> None:
        assert callable(getattr(client, checker_name))

    @pytest.mark.parametrize(
        "listener_name",
        [
            "register_message_moderation_listeners",
            # The two older per-message listeners swallowed every exception into
            # a log line. A silent failure in a moderation-adjacent listener is
            # exactly how a misconfiguration decays into "the bot just stopped
            # enforcing" with nothing to page on.
            "on_attachment_filter",
            "on_message_create",
        ],
    )
    def test_listener_has_no_log_and_continue_exception_handler(
        self, listener_name: str
    ) -> None:
        listener_node = find_function_node(client_source_tree(), listener_name)
        blanket_handlers_that_only_log = [
            handler
            for handler in ast.walk(listener_node)
            if isinstance(handler, ast.ExceptHandler)
            and isinstance(handler.type, ast.Name)
            and handler.type.id == "Exception"
            and handler_only_logs(handler)
        ]
        assert blanket_handlers_that_only_log == []


class TestGuildLeaveReleasesSpamState:
    """Leaving a guild drops its spam-engine state immediately."""

    def test_registers_a_guild_leave_listener(
        self, recording_bot: ListenerRecordingBot
    ) -> None:
        client.register_message_moderation_listeners(recording_bot)
        assert len(listeners_for_event(recording_bot, hikari.GuildLeaveEvent)) == 1

    async def test_departing_a_guild_releases_its_buffered_state(
        self, guild_leave_listener
    ) -> None:
        spam_engine.reset_spam_states()
        thresholds = spam_engine.SpamThresholds.from_config(
            ModerationFilterConfig(guild_id="777")
        )
        spam_engine.get_guild_spam_state("777", thresholds, now=1000.0)
        assert spam_engine.tracked_guild_count() == 1

        await guild_leave_listener(Mock(guild_id=777))

        assert spam_engine.tracked_guild_count() == 0

    async def test_departing_an_untracked_guild_is_a_no_op(
        self, guild_leave_listener
    ) -> None:
        spam_engine.reset_spam_states()

        await guild_leave_listener(Mock(guild_id=888))

        assert spam_engine.tracked_guild_count() == 0

    @pytest.mark.parametrize(
        "function_name",
        ["run_bot", "register_message_moderation_listeners"],
    )
    def test_listener_functions_contain_no_inline_imports(
        self, function_name: str
    ) -> None:
        function_node = find_function_node(client_source_tree(), function_name)
        inline_imports = [
            node
            for node in ast.walk(function_node)
            if isinstance(node, ast.Import | ast.ImportFrom)
        ]
        assert inline_imports == []
