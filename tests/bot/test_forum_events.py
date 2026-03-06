"""Tests for forum thread event handlers.

This module tests Discord event handling for forum post monitoring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest


class MockDiscordChannel:
    """Mock Discord channel for testing."""

    def __init__(self, id: str = "123456789", type=None, name: str = "test-forum"):
        self.id = int(id)
        self.type = type  # Will be set to forum channel type
        self.name = name


class MockDiscordThread:
    """Mock Discord thread for testing."""

    def __init__(self, id: str = "987654321", name: str = "Test Thread",
                 parent_id: str = "123456789"):
        self.id = int(id)
        self.name = name
        self.parent_id = int(parent_id) if parent_id else None
        self.applied_tags = []
        self.applied_tag_ids = []


class MockDiscordUser:
    """Mock Discord user for testing."""

    def __init__(self, id: str = "111111111", display_name: str = "TestUser",
                 username: str = "testuser"):
        self.id = int(id)
        self.display_name = display_name
        self.username = username


class MockDiscordMessage:
    """Mock Discord message for testing."""

    def __init__(self, id: str = "555555555", content: str = "Test message content",
                 author=None, thread=None):
        self.id = int(id)
        self.content = content
        self.author = author or MockDiscordUser()
        self.thread = thread
        self.attachments = []
        self.embeds = []


class MockThreadCreateEvent:
    """Mock hikari thread create event."""

    def __init__(self, guild_id: str = "999999999", thread=None, is_forum_thread=True):
        self.guild_id = int(guild_id) if guild_id else None
        self.thread = thread or MockDiscordThread()
        self.is_forum_thread = is_forum_thread


class MockMessageCreateEvent:
    """Mock hikari message create event."""

    def __init__(self, guild_id: str = "999999999", message=None, is_bot: bool = False,
                 channel_id: str = "123456789"):
        self.guild_id = int(guild_id) if guild_id else None
        self.message = message or MockDiscordMessage()
        self.is_bot = is_bot
        self.channel_id = int(channel_id)
        self.author = self.message.author


class TestForumEventHandlers:
    """Test forum event handling functionality."""

    @pytest.fixture
    def mock_bot(self):
        """Create a mock Discord bot."""
        bot = Mock()
        bot.d = {
            'forum_agent_service': AsyncMock(),
            '_services': {
                'forum_agent_service': AsyncMock()
            }
        }
        bot.rest = AsyncMock()
        return bot

    @pytest.fixture
    def mock_forum_agent_service(self):
        """Create a mock ForumAgentService."""
        service = AsyncMock()
        service.process_forum_post = AsyncMock(return_value=[])
        service.process_forum_post_with_tagging = AsyncMock(return_value=([], {}))
        return service

    async def test_thread_create_event_forum_thread(self, mock_bot, mock_forum_agent_service):
        """Test handling of forum thread creation events."""
        mock_bot.d['forum_agent_service'] = mock_forum_agent_service

        # Mock thread with forum post characteristics
        thread = MockDiscordThread(name="How to fix Python import error?")
        event = MockThreadCreateEvent(thread=thread)

        # Mock response from service (process_forum_post_with_tagging returns (responses, topic_user_map))
        mock_responses = [
            {
                'agent_id': str(uuid4()),
                'agent_name': 'Python Helper',
                'should_respond': True,
                'response_content': 'Try checking your PYTHONPATH...',
                'confidence': 0.85
            }
        ]
        mock_forum_agent_service.process_forum_post_with_tagging.return_value = (mock_responses, {})

        # Mock fetch_messages to return the initial forum post message
        mock_initial_message = MockDiscordMessage(
            content="I'm getting ImportError when trying to import my module",
            author=MockDiscordUser(display_name="NewDeveloper")
        )
        mock_bot.rest.fetch_messages = AsyncMock(return_value=[mock_initial_message])

        # Import and test the handler
        from smarter_dev.bot.client import handle_forum_thread_create

        await handle_forum_thread_create(mock_bot, event)

        # Verify post was processed using process_forum_post_with_tagging
        mock_forum_agent_service.process_forum_post_with_tagging.assert_called_once()
        call_args = mock_forum_agent_service.process_forum_post_with_tagging.call_args
        assert call_args[0][0] == str(event.guild_id)

        # Verify responses were posted via rest.create_message
        mock_bot.rest.create_message.assert_called_once()

    async def test_thread_create_event_not_forum_thread(self, mock_bot, mock_forum_agent_service):
        """Test ignoring non-forum thread creation events."""
        mock_bot.d['forum_agent_service'] = mock_forum_agent_service

        event = MockThreadCreateEvent(is_forum_thread=False)

        from smarter_dev.bot.client import handle_forum_thread_create

        await handle_forum_thread_create(mock_bot, event)

        # Should not process non-forum threads
        mock_forum_agent_service.process_forum_post_with_tagging.assert_not_called()

    async def test_thread_create_event_no_guild(self, mock_bot, mock_forum_agent_service):
        """Test ignoring thread creation events without guild context."""
        mock_bot.d['forum_agent_service'] = mock_forum_agent_service

        event = MockThreadCreateEvent(guild_id=None)

        from smarter_dev.bot.client import handle_forum_thread_create

        await handle_forum_thread_create(mock_bot, event)

        # Should not process threads without guild
        mock_forum_agent_service.process_forum_post_with_tagging.assert_not_called()

    async def test_thread_create_event_no_service(self, mock_bot):
        """Test handling when forum agent service is not available."""
        mock_bot.d = {}  # No services available

        event = MockThreadCreateEvent()

        from smarter_dev.bot.client import handle_forum_thread_create

        # Should not crash when service is unavailable
        await handle_forum_thread_create(mock_bot, event)

    async def test_extract_forum_post_data(self, mock_bot):
        """Test extraction of forum post data from Discord objects."""
        from smarter_dev.bot.client import extract_forum_post_data

        # Mock Discord thread with initial message
        thread = MockDiscordThread(name="Test Forum Post Title")
        initial_message = MockDiscordMessage(
            content="This is the forum post content",
            author=MockDiscordUser(display_name="ForumUser")
        )
        initial_message.attachments = [Mock(filename="image.png"), Mock(filename="doc.pdf")]

        # extract_forum_post_data takes (bot, thread, initial_message)
        # and uses applied_tag_ids (not applied_tags with .name)
        post_data = await extract_forum_post_data(mock_bot, thread, initial_message)

        assert post_data.title == "Test Forum Post Title"
        assert post_data.content == "This is the forum post content"
        assert post_data.author_display_name == "ForumUser"
        assert "image.png" in post_data.attachments
        assert "doc.pdf" in post_data.attachments
        assert post_data.channel_id == str(thread.parent_id)
        assert post_data.thread_id == str(thread.id)

    async def test_extract_forum_post_data_no_message(self, mock_bot):
        """Test extraction when initial message is not available."""
        from smarter_dev.bot.client import extract_forum_post_data

        thread = MockDiscordThread(name="Title Only Post")

        post_data = await extract_forum_post_data(mock_bot, thread, None)

        assert post_data.title == "Title Only Post"
        assert post_data.content == ""
        assert post_data.author_display_name == "Unknown"
        assert post_data.tags == []
        assert post_data.attachments == []

    async def test_post_agent_responses_with_responses(self, mock_bot):
        """Test posting agent responses to Discord thread."""
        from smarter_dev.bot.client import post_agent_responses

        thread_id = 987654321
        responses = [
            {
                'agent_name': 'Python Helper',
                'should_respond': True,
                'response_content': 'Here is how to fix your Python import issue...',
                'confidence': 0.9
            },
            {
                'agent_name': 'Code Reviewer',
                'should_respond': True,
                'response_content': 'Also consider checking your file structure...',
                'confidence': 0.8
            }
        ]

        result = await post_agent_responses(mock_bot, thread_id, responses)

        # Should have posted 2 responses
        assert mock_bot.rest.create_message.call_count == 2
        assert result is True

        # The actual implementation posts raw response_content (no agent name prefix)
        first_call = mock_bot.rest.create_message.call_args_list[0]
        assert first_call[0][0] == thread_id  # channel/thread ID
        assert 'fix your Python import' in first_call[1]['content']

    async def test_post_agent_responses_no_responses(self, mock_bot):
        """Test posting when no agents should respond."""
        from smarter_dev.bot.client import post_agent_responses

        thread_id = 987654321
        responses = [
            {
                'agent_name': 'Spam Filter',
                'should_respond': False,
                'response_content': '',
                'confidence': 0.1
            }
        ]

        result = await post_agent_responses(mock_bot, thread_id, responses)

        # Should not post any messages
        mock_bot.rest.create_message.assert_not_called()
        assert result is False

    async def test_post_agent_responses_error_handling(self, mock_bot):
        """Test error handling when posting responses fails."""
        from smarter_dev.bot.client import post_agent_responses

        mock_bot.rest.create_message.side_effect = Exception("Discord API error")

        thread_id = 987654321
        responses = [
            {
                'agent_name': 'Helper',
                'should_respond': True,
                'response_content': 'This should fail to post',
                'confidence': 0.8
            }
        ]

        # Should not raise exception, but handle gracefully
        result = await post_agent_responses(mock_bot, thread_id, responses)

        # Should have attempted to post
        mock_bot.rest.create_message.assert_called_once()

    async def test_is_forum_channel(self):
        """Test forum channel type detection."""
        import hikari
        from smarter_dev.bot.client import is_forum_channel

        forum_channel = Mock()
        forum_channel.type = hikari.ChannelType.GUILD_FORUM

        regular_channel = Mock()
        regular_channel.type = hikari.ChannelType.GUILD_TEXT

        assert is_forum_channel(forum_channel) is True
        assert is_forum_channel(regular_channel) is False

    async def test_message_create_in_forum_thread(self, mock_bot, mock_forum_agent_service):
        """Test message creation in forum threads (follow-up messages)."""
        mock_bot.d['forum_agent_service'] = mock_forum_agent_service

        # Create message in a thread (follow-up, not initial post)
        thread = MockDiscordThread()
        message = MockDiscordMessage(thread=thread, content="Thanks for the help!")
        event = MockMessageCreateEvent(message=message, is_bot=False)

        from smarter_dev.bot.client import handle_forum_message_create

        # For now, we might not process follow-up messages
        await handle_forum_message_create(mock_bot, event)

        # Currently, we only process initial forum posts (thread creation)
        # so this should not trigger agent processing
        mock_forum_agent_service.process_forum_post.assert_not_called()

    async def test_forum_post_data_structure(self):
        """Test that forum post data has the expected structure."""
        from smarter_dev.bot.client import ForumPostData

        # Test data class structure
        post = ForumPostData(
            title="Test Title",
            content="Test Content",
            author_display_name="TestAuthor",
            tags=["tag1", "tag2"],
            attachments=["file.txt"],
            channel_id="123456789",
            thread_id="987654321",
            guild_id="555555555"
        )

        assert post.title == "Test Title"
        assert post.content == "Test Content"
        assert post.author_display_name == "TestAuthor"
        assert post.tags == ["tag1", "tag2"]
        assert post.attachments == ["file.txt"]
        assert post.channel_id == "123456789"
        assert post.thread_id == "987654321"
        assert post.guild_id == "555555555"

    async def test_event_handler_integration(self, mock_bot, mock_forum_agent_service):
        """Test complete event handler integration flow."""
        mock_bot.d['forum_agent_service'] = mock_forum_agent_service

        # Mock complete flow from thread creation to response posting
        thread = MockDiscordThread(name="Integration Test Question")
        event = MockThreadCreateEvent(thread=thread)

        # Mock service response (process_forum_post_with_tagging returns tuple)
        mock_responses = [
            {
                'agent_id': str(uuid4()),
                'agent_name': 'Integration Helper',
                'should_respond': True,
                'response_content': 'Here is the integration test response',
                'confidence': 0.88
            }
        ]
        mock_forum_agent_service.process_forum_post_with_tagging.return_value = (mock_responses, {})

        # Mock fetch_messages for initial message
        mock_initial_message = MockDiscordMessage(
            content="This is an integration test",
            author=MockDiscordUser(display_name="Tester")
        )
        mock_bot.rest.fetch_messages = AsyncMock(return_value=[mock_initial_message])

        # Test the complete flow
        from smarter_dev.bot.client import handle_forum_thread_create

        await handle_forum_thread_create(mock_bot, event)

        # Verify the complete flow
        mock_forum_agent_service.process_forum_post_with_tagging.assert_called_once()
        mock_bot.rest.create_message.assert_called_once()

        # Verify response content (raw response_content, no agent name prefix)
        call_args = mock_bot.rest.create_message.call_args
        assert 'integration test response' in call_args[1]['content']
