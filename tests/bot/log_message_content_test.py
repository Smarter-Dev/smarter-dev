"""Log lines must identify messages, never quote them.

Every site here used to write verbatim Discord message text into the bot's
logs. Under the message-content policy a log line may carry ids and character
counts only, so these tests assert both halves: the text is gone and the
identifying detail an operator needs is still there.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from smarter_dev.bot.client import extract_forum_post_data
from smarter_dev.bot.client import handle_forum_thread_create
from smarter_dev.bot.plugins import mod_monitor
from smarter_dev.bot.services.forum_agent_service import ForumAgentService
from smarter_dev.bot.utils.messages import ConversationContextBuilder
from smarter_dev.bot.utils.messages import gather_message_context

MEMBER_TEXT = "the-verbatim-member-text-nobody-may-log"


def _logged_text(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def _fake_message(
    message_id: int,
    content: str,
    author_id: int = 5001,
    is_bot: bool = False,
) -> SimpleNamespace:
    author = SimpleNamespace(
        id=author_id,
        username=f"member{author_id}",
        display_name=f"Member {author_id}",
        is_bot=is_bot,
    )
    return SimpleNamespace(
        id=message_id,
        content=content,
        author=author,
        attachments=[],
        created_at=datetime.now(UTC),
        referenced_message=None,
        guild_id=777,
    )


def _rest_returning(messages: list[SimpleNamespace]) -> MagicMock:
    async def iterate():
        for message in messages:
            yield message

    paginator = MagicMock()
    paginator.limit = MagicMock(return_value=iterate())
    rest = MagicMock()
    rest.fetch_messages = MagicMock(return_value=paginator)
    return rest


def _bot_returning(messages: list[SimpleNamespace], bot_user_id: int = 9999) -> MagicMock:
    bot = MagicMock()
    bot.rest = _rest_returning(messages)
    bot.get_me = MagicMock(return_value=SimpleNamespace(id=bot_user_id))
    return bot


@pytest.fixture
def message_context_cache():
    with patch("smarter_dev.bot.utils.messages.bot_cache") as cache:
        cache.get_channel_info = AsyncMock(return_value={})
        cache.get_guild_roles = AsyncMock(return_value={})
        yield cache


class TestGatherMessageContextLogs:
    """gather_message_context logs ids and lengths, never bodies."""

    async def test_skipped_short_message_logs_its_id_and_length(
        self, caplog, message_context_cache
    ):
        short = _fake_message(11111, "tiny-secret-body")
        keeper = _fake_message(22222, MEMBER_TEXT)

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.utils.messages"):
            gathered = await gather_message_context(
                _bot_returning([keeper, short]),
                channel_id=1,
                limit=5,
                skip_short_messages=True,
                min_message_length=20,
            )

        assert len(gathered) == 1
        logged = _logged_text(caplog)
        assert "tiny-secret-body" not in logged
        assert "11111" in logged
        assert f"{len('tiny-secret-body')} chars" in logged

    async def test_skip_log_reports_the_length_the_filter_compared(
        self, caplog, message_context_cache
    ):
        padded = _fake_message(12121, " " * 20 + "abcde")

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.utils.messages"):
            gathered = await gather_message_context(
                _bot_returning([padded]),
                channel_id=1,
                limit=5,
                skip_short_messages=True,
                min_message_length=20,
            )

        assert gathered == []
        logged = _logged_text(caplog)
        assert "12121 (5 chars)" in logged

    async def test_selected_messages_log_ids_not_bodies(
        self, caplog, message_context_cache
    ):
        message = _fake_message(33333, MEMBER_TEXT, author_id=4242)

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.utils.messages"):
            gathered = await gather_message_context(
                _bot_returning([message]), channel_id=1, limit=5
            )

        assert len(gathered) == 1
        logged = _logged_text(caplog)
        assert MEMBER_TEXT not in logged
        assert "33333" in logged
        assert "4242" in logged
        assert f"{len(MEMBER_TEXT)} chars" in logged


class TestConversationContextBuilderLogs:
    """The skipped-bot-message lines identify the message instead of quoting it."""

    async def test_base_fetch_logs_skipped_tool_message_by_id(self, caplog):
        tool_message = _fake_message(
            44444, f"-# {MEMBER_TEXT}", author_id=9999, is_bot=True
        )
        builder = ConversationContextBuilder(_bot_returning([tool_message]))

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.utils.messages"):
            kept = await builder._fetch_base_messages(channel_id=1, limit=5)

        assert kept == []
        logged = _logged_text(caplog)
        assert MEMBER_TEXT not in logged
        assert "44444" in logged
        assert f"{len(tool_message.content)} chars" in logged

    async def test_since_fetch_logs_skipped_tool_message_by_id(self, caplog):
        tool_message = _fake_message(
            55555, f"-# {MEMBER_TEXT}", author_id=9999, is_bot=True
        )
        builder = ConversationContextBuilder(_bot_returning([tool_message]))

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.utils.messages"):
            kept = await builder._fetch_messages_since(
                channel_id=1, since_message_id=1, limit=5
            )

        assert kept == []
        logged = _logged_text(caplog)
        assert MEMBER_TEXT not in logged
        assert "55555" in logged
        assert f"{len(tool_message.content)} chars" in logged


class TestModMonitorTriggerLog:
    """The moderation trigger line names the message, not its text."""

    @pytest.fixture
    def monitored_guild(self):
        mod_monitor._guild_configs["777"] = {
            "monitored_role_ids": {"31337"},
            "instructions": "",
            "enabled_tools": ["timeout"],
            "context_message_limit": 25,
            "response_channel_id": None,
        }
        yield "777"
        mod_monitor._guild_configs.pop("777", None)

    def _event(self, content: str | None) -> SimpleNamespace:
        message = _fake_message(66666, content, author_id=8484)
        message.role_mention_ids = [31337]
        return SimpleNamespace(message=message, guild_id=777, channel_id=1234)

    async def test_trigger_logs_ids_and_length(self, caplog, monitored_guild):
        with caplog.at_level(logging.INFO, logger="smarter_dev.bot.plugins.mod_monitor"), patch.object(
            mod_monitor, "_handle_moderation", new=AsyncMock()
        ):
            await mod_monitor.on_message_create(self._event(MEMBER_TEXT))

        logged = _logged_text(caplog)
        assert MEMBER_TEXT not in logged
        assert "66666" in logged
        assert "8484" in logged
        assert f"{len(MEMBER_TEXT)} chars" in logged

    async def test_trigger_without_text_still_logs(self, caplog, monitored_guild):
        with caplog.at_level(logging.INFO, logger="smarter_dev.bot.plugins.mod_monitor"), patch.object(
            mod_monitor, "_handle_moderation", new=AsyncMock()
        ):
            await mod_monitor.on_message_create(self._event(None))

        logged = _logged_text(caplog)
        assert "66666" in logged
        assert "0 chars" in logged


class TestForumAgentServiceLogs:
    """Forum post bodies are logged as a length, and the noisy debug drops to DEBUG."""

    @pytest.fixture
    def api_client(self):
        client = AsyncMock()
        recorded = MagicMock()
        recorded.status_code = 200
        recorded.json = MagicMock(return_value={"id": "recorded-id"})
        client.post = AsyncMock(return_value=recorded)
        return client

    @pytest.fixture
    def service(self, api_client):
        return ForumAgentService(api_client)

    def _post(self) -> SimpleNamespace:
        return SimpleNamespace(
            channel_id="123",
            thread_id="456",
            guild_id="777",
            title="A post title",
            content=MEMBER_TEXT,
            author_display_name="Member",
            tags=[],
            attachments=[],
        )

    async def test_record_response_logs_content_length_only(self, caplog, service):
        agent = {"id": "agent-1", "name": "Agent", "guild_id": "777"}

        with caplog.at_level(
            logging.DEBUG, logger="smarter_dev.bot.services.forum_agent_service"
        ):
            await service.record_response(
                agent, self._post(), "reasoned", 0.9, "answer", 10, 20, True
            )

        logged = _logged_text(caplog)
        assert MEMBER_TEXT not in logged
        assert f"{len(MEMBER_TEXT)} chars" in logged

    async def test_record_response_survives_a_post_with_no_text(self, caplog, service):
        agent = {"id": "agent-1", "name": "Agent", "guild_id": "777"}
        post = self._post()
        post.content = None

        with caplog.at_level(
            logging.DEBUG, logger="smarter_dev.bot.services.forum_agent_service"
        ):
            await service.record_response(
                agent, post, "reasoned", 0.9, "answer", 10, 20, True
            )

        assert "Post content: 0 chars" in _logged_text(caplog)

    async def test_classification_debug_omits_post_content(self, caplog, service):
        agent = {
            "id": "agent-1",
            "name": "Agent",
            "guild_id": "777",
            "monitored_forums": ["123"],
            "system_prompt": "prompt",
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "enable_responses": True,
            "enable_user_tagging": True,
        }
        evaluator = MagicMock()
        evaluator.evaluate_post_combined = AsyncMock(
            return_value=("reasoned", 0.1, "", ["topic"], 10)
        )

        with caplog.at_level(
            logging.DEBUG, logger="smarter_dev.bot.services.forum_agent_service"
        ), patch.object(
            service, "load_guild_agents", new=AsyncMock(return_value=[agent])
        ), patch.object(
            service, "check_rate_limit", new=AsyncMock(return_value=True)
        ), patch.object(
            service, "record_response", new=AsyncMock(return_value="recorded-id")
        ), patch.object(
            service, "get_notification_topics", new=AsyncMock(return_value=["topic"])
        ), patch(
            "smarter_dev.bot.agents.forum_agent.ForumMonitorAgent",
            return_value=evaluator,
        ):
            await service.process_forum_post_with_tagging(
                "777", self._post(), user_subscriptions=[]
            )

        assert MEMBER_TEXT not in _logged_text(caplog)

    async def test_classification_debug_is_not_logged_as_an_error(self, caplog, service):
        agent = {
            "id": "agent-1",
            "name": "Agent",
            "guild_id": "777",
            "monitored_forums": ["123"],
            "system_prompt": "prompt",
            "response_threshold": 0.7,
            "max_responses_per_hour": 5,
            "enable_responses": True,
            "enable_user_tagging": True,
        }
        evaluator = MagicMock()
        evaluator.evaluate_post_combined = AsyncMock(
            return_value=("reasoned", 0.1, "", ["topic"], 10)
        )

        with caplog.at_level(
            logging.DEBUG, logger="smarter_dev.bot.services.forum_agent_service"
        ), patch.object(
            service, "load_guild_agents", new=AsyncMock(return_value=[agent])
        ), patch.object(
            service, "check_rate_limit", new=AsyncMock(return_value=True)
        ), patch.object(
            service, "record_response", new=AsyncMock(return_value="recorded-id")
        ), patch.object(
            service, "get_notification_topics", new=AsyncMock(return_value=["topic"])
        ), patch(
            "smarter_dev.bot.agents.forum_agent.ForumMonitorAgent",
            return_value=evaluator,
        ):
            await service.process_forum_post_with_tagging(
                "777", self._post(), user_subscriptions=[]
            )

        classification_levels = {
            record.levelno
            for record in caplog.records
            if "for classification" in record.getMessage()
            or "operation_mode=combined, matching_topics=" in record.getMessage()
        }
        assert classification_levels == {logging.DEBUG}


class TestForumExtractLogs:
    """Forum post extraction logs a length, never the post body."""

    def _thread(self) -> SimpleNamespace:
        return SimpleNamespace(name="A post title", id=456, parent_id=123)

    def _initial_message(self, content: str | None) -> SimpleNamespace:
        message = _fake_message(77777, content)
        message.attachments = []
        return message

    async def test_extract_logs_content_length_not_body(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.client"):
            extracted = await extract_forum_post_data(
                MagicMock(), self._thread(), self._initial_message(MEMBER_TEXT)
            )

        assert extracted.content == MEMBER_TEXT
        logged = _logged_text(caplog)
        assert MEMBER_TEXT not in logged
        assert f"{len(MEMBER_TEXT)} chars" in logged

    async def test_extract_survives_a_post_with_no_text(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.client"):
            extracted = await extract_forum_post_data(
                MagicMock(), self._thread(), self._initial_message(None)
            )

        assert extracted.content is None
        assert "0 chars" in _logged_text(caplog)

    async def test_thread_create_survives_a_post_with_no_text(self, caplog):
        forum_agent_service = MagicMock()
        forum_agent_service.process_forum_post_with_tagging = AsyncMock(
            return_value=([], {})
        )
        bot = MagicMock()
        bot.d = {"forum_agent_service": forum_agent_service}
        bot.rest.fetch_messages = AsyncMock(
            return_value=[self._initial_message(None)]
        )
        event = SimpleNamespace(thread=self._thread(), guild_id=777)

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.client"):
            await handle_forum_thread_create(bot, event)

        forum_agent_service.process_forum_post_with_tagging.assert_awaited_once()
        logged = _logged_text(caplog)
        assert "Could not fetch initial message" not in logged
        assert "Content length: 0" in logged


class TestGatherMessageContextHandlesTextlessMessages:
    """A message with no text (attachment, embed or sticker only) is ordinary."""

    async def test_textless_message_does_not_drop_the_batch(
        self, caplog, message_context_cache
    ):
        textless = _fake_message(88888, None)
        keeper = _fake_message(22222, MEMBER_TEXT)

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.utils.messages"):
            gathered = await gather_message_context(
                _bot_returning([keeper, textless]), channel_id=1, limit=5
            )

        assert [message.message_id for message in gathered] == ["88888", "22222"]
        assert "Failed to gather message context" not in _logged_text(caplog)

    async def test_textless_message_is_skipped_as_zero_chars_when_filtering(
        self, caplog, message_context_cache
    ):
        textless = _fake_message(88888, None)
        keeper = _fake_message(22222, MEMBER_TEXT)

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.utils.messages"):
            gathered = await gather_message_context(
                _bot_returning([keeper, textless]),
                channel_id=1,
                limit=5,
                skip_short_messages=True,
                min_message_length=10,
            )

        assert [message.message_id for message in gathered] == ["22222"]
        assert "88888 (0 chars)" in _logged_text(caplog)


class TestBotToolUsagePredicate:
    """The predicate answers a question about a message and does nothing else."""

    def _builder(self) -> ConversationContextBuilder:
        return ConversationContextBuilder(_bot_returning([]))

    def test_predicate_writes_no_log_line(self, caplog):
        tool_message = _fake_message(
            44444, f"-# {MEMBER_TEXT}", author_id=9999, is_bot=True
        )

        with caplog.at_level(logging.DEBUG, logger="smarter_dev.bot.utils.messages"):
            assert self._builder()._is_bot_tool_usage_message(tool_message) is True

        assert caplog.records == []

    def test_predicate_rejects_a_members_tool_shaped_message(self):
        member_message = _fake_message(44445, "-# not from the bot", author_id=5001)

        assert self._builder()._is_bot_tool_usage_message(member_message) is False

    def test_predicate_rejects_a_textless_bot_message(self):
        textless_bot_message = _fake_message(44446, None, author_id=9999, is_bot=True)

        assert (
            self._builder()._is_bot_tool_usage_message(textless_bot_message) is False
        )


class TestConversationContextBuilderFetching:
    """Both fetch paths share one loop: same caching, skipping and ordering."""

    async def test_base_fetch_caches_every_message_including_skipped_ones(self):
        tool_message = _fake_message(
            44444, f"-# {MEMBER_TEXT}", author_id=9999, is_bot=True
        )
        keeper = _fake_message(22222, MEMBER_TEXT)
        builder = ConversationContextBuilder(_bot_returning([tool_message, keeper]))

        kept = await builder._fetch_base_messages(channel_id=1, limit=5)

        assert [message.id for message in kept] == [22222]
        assert set(builder._fetched_messages) == {44444, 22222}

    async def test_base_fetch_stops_at_the_limit_in_chronological_order(self):
        newest_first = [
            _fake_message(30003, MEMBER_TEXT),
            _fake_message(20002, MEMBER_TEXT),
            _fake_message(10001, MEMBER_TEXT),
        ]
        bot = _bot_returning(newest_first)
        builder = ConversationContextBuilder(bot)

        kept = await builder._fetch_base_messages(channel_id=1, limit=2)

        assert [message.id for message in kept] == [20002, 30003]
        bot.rest.fetch_messages.assert_called_once_with(1)

    async def test_since_fetch_caches_skips_and_orders_the_same_way(self):
        tool_message = _fake_message(
            55555, f"-# {MEMBER_TEXT}", author_id=9999, is_bot=True
        )
        newer = _fake_message(30003, MEMBER_TEXT)
        older = _fake_message(20002, MEMBER_TEXT)
        bot = _bot_returning([newer, tool_message, older])
        builder = ConversationContextBuilder(bot)

        kept = await builder._fetch_messages_since(
            channel_id=1, since_message_id=99, limit=5
        )

        assert [message.id for message in kept] == [20002, 30003]
        assert set(builder._fetched_messages) == {30003, 55555, 20002}
        bot.rest.fetch_messages.assert_called_once_with(1, after=99)
