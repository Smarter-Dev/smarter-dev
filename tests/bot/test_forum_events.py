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
        return bot

    @pytest.fixture  
    def mock_forum_agent_service(self):
        """Create a mock ForumAgentService."""
        service = AsyncMock()
        service.process_forum_post = AsyncMock(return_value=[])
        return service

    async def test_thread_create_event_forum_thread(self, mock_bot, mock_forum_agent_service):
        """Test handling of forum thread creation events."""
        mock_bot.d['forum_agent_service'] = mock_forum_agent_service
        
        # Mock thread with forum post characteristics
        thread = MockDiscordThread(name="How to fix Python import error?")
        event = MockThreadCreateEvent(thread=thread)
        
        # Mock response from service
        mock_responses = [
            {
                'agent_id': str(uuid4()),
                'agent_name': 'Python Helper',
                'should_respond': True,
                'response_content': 'Try checking your PYTHONPATH...',
                'confidence': 0.85
            }
        ]
        mock_forum_agent_service.process_forum_post.return_value = mock_responses
        
        # Import and test the handler
        from smarter_dev.bot.client import handle_forum_thread_create
        
        with patch('smarter_dev.bot.client.extract_forum_post_data') as mock_extract:
            mock_post = Mock()
            mock_post.title = "How to fix Python import error?"
            mock_post.content = "I'm getting ImportError when trying to import my module"
            mock_post.author_display_name = "NewDeveloper"
            mock_post.tags = ["python", "error"]
            mock_post.attachments = []
            mock_extract.return_value = mock_post
            
            with patch('smarter_dev.bot.client.post_agent_responses') as mock_post_responses:
                await handle_forum_thread_create(mock_bot, event)
                
                # Verify post was processed
                mock_forum_agent_service.process_forum_post.assert_called_once_with(
                    str(event.guild_id), mock_post
                )
                
                # Verify responses were posted
                mock_post_responses.assert_called_once_with(
                    mock_bot, event.thread.id, mock_responses
                )

    async def test_thread_create_event_not_forum_thread(self, mock_bot, mock_forum_agent_service):
        """Test ignoring non-forum thread creation events."""
        mock_bot.d['forum_agent_service'] = mock_forum_agent_service
        
        event = MockThreadCreateEvent(is_forum_thread=False)
        
        from smarter_dev.bot.client import handle_forum_thread_create
        
        await handle_forum_thread_create(mock_bot, event)
        
        # Should not process non-forum threads
        mock_forum_agent_service.process_forum_post.assert_not_called()

    async def test_thread_create_event_no_guild(self, mock_bot, mock_forum_agent_service):
        """Test ignoring thread creation events without guild context."""
        mock_bot.d['forum_agent_service'] = mock_forum_agent_service
        
        event = MockThreadCreateEvent(guild_id=None)
        
        from smarter_dev.bot.client import handle_forum_thread_create
        
        await handle_forum_thread_create(mock_bot, event)
        
        # Should not process threads without guild
        mock_forum_agent_service.process_forum_post.assert_not_called()

    async def test_thread_create_event_no_service(self, mock_bot):
        """Test handling when forum agent service is not available."""
        mock_bot.d = {}  # No services available
        
        event = MockThreadCreateEvent()
        
        from smarter_dev.bot.client import handle_forum_thread_create
        
        # Should not crash when service is unavailable
        await handle_forum_thread_create(mock_bot, event)

    async def test_extract_forum_post_data(self):
        """Test extraction of forum post data from Discord objects."""
        from smarter_dev.bot.client import extract_forum_post_data
        
        # Mock Discord thread with initial message
        thread = MockDiscordThread(name="Test Forum Post Title")
        initial_message = MockDiscordMessage(
            content="This is the forum post content",
            author=MockDiscordUser(display_name="ForumUser")
        )
        initial_message.attachments = [Mock(filename="image.png"), Mock(filename="doc.pdf")]
        
        # Create mock tags with proper name attributes
        mock_python_tag = Mock()
        mock_python_tag.name = "python"
        mock_help_tag = Mock() 
        mock_help_tag.name = "help"
        
        # Mock thread tags
        with patch.object(thread, 'applied_tags', [mock_python_tag, mock_help_tag]):
            post_data = extract_forum_post_data(thread, initial_message)
            
            assert post_data.title == "Test Forum Post Title"
            assert post_data.content == "This is the forum post content"
            assert post_data.author_display_name == "ForumUser"
            assert "python" in post_data.tags
            assert "help" in post_data.tags
            assert "image.png" in post_data.attachments
            assert "doc.pdf" in post_data.attachments
            assert post_data.channel_id == str(thread.parent_id)
            assert post_data.thread_id == str(thread.id)

    async def test_extract_forum_post_data_no_message(self):
        """Test extraction when initial message is not available."""
        from smarter_dev.bot.client import extract_forum_post_data
        
        thread = MockDiscordThread(name="Title Only Post")
        
        post_data = extract_forum_post_data(thread, None)
        
        assert post_data.title == "Title Only Post"
        assert post_data.content == ""
        assert post_data.author_display_name == "Unknown"
        assert post_data.tags == []
        assert post_data.attachments == []

    async def test_post_agent_responses_with_responses(self, mock_bot):
        """Test posting agent responses to Discord thread."""
        from smarter_dev.bot.client import post_agent_responses
        
        # Mock REST client
        mock_rest = AsyncMock()
        mock_bot.rest = mock_rest
        
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
        
        await post_agent_responses(mock_bot, thread_id, responses)
        
        # Should have posted 2 responses
        assert mock_rest.create_message.call_count == 2
        
        # Check first response
        first_call = mock_rest.create_message.call_args_list[0]
        assert first_call[0][0] == thread_id  # channel/thread ID
        assert 'Python Helper' in first_call[1]['content']
        assert 'fix your Python import' in first_call[1]['content']
        
        # Check second response
        second_call = mock_rest.create_message.call_args_list[1]
        assert second_call[0][0] == thread_id
        assert 'Code Reviewer' in second_call[1]['content']
        assert 'file structure' in second_call[1]['content']

    async def test_post_agent_responses_no_responses(self, mock_bot):
        """Test posting when no agents should respond."""
        from smarter_dev.bot.client import post_agent_responses
        
        mock_rest = AsyncMock()
        mock_bot.rest = mock_rest
        
        thread_id = 987654321
        responses = [
            {
                'agent_name': 'Spam Filter',
                'should_respond': False,
                'response_content': '',
                'confidence': 0.1
            }
        ]
        
        await post_agent_responses(mock_bot, thread_id, responses)
        
        # Should not post any messages
        mock_rest.create_message.assert_not_called()

    async def test_post_agent_responses_error_handling(self, mock_bot):
        """Test error handling when posting responses fails."""
        from smarter_dev.bot.client import post_agent_responses
        
        mock_rest = AsyncMock()
        mock_rest.create_message.side_effect = Exception("Discord API error")
        mock_bot.rest = mock_rest
        
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
        await post_agent_responses(mock_bot, thread_id, responses)
        
        # Should have attempted to post
        mock_rest.create_message.assert_called_once()

    async def test_is_forum_channel(self):
        """Test forum channel type detection."""
        from smarter_dev.bot.client import is_forum_channel
        
        # Mock hikari channel types
        with patch('hikari.ChannelType.GUILD_FORUM', 15):  # Hypothetical enum value
            forum_channel = Mock()
            forum_channel.type = 15
            
            regular_channel = Mock() 
            regular_channel.type = 0  # Text channel
            
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
        
        # Mock service response
        mock_responses = [
            {
                'agent_id': str(uuid4()),
                'agent_name': 'Integration Helper',
                'should_respond': True,
                'response_content': 'Here is the integration test response',
                'confidence': 0.88
            }
        ]
        mock_forum_agent_service.process_forum_post.return_value = mock_responses
        
        # Mock REST client for response posting
        mock_rest = AsyncMock()
        mock_bot.rest = mock_rest
        
        # Test the complete flow
        from smarter_dev.bot.client import handle_forum_thread_create
        
        with patch('smarter_dev.bot.client.extract_forum_post_data') as mock_extract:
            mock_post = Mock()
            mock_post.title = "Integration Test Question"
            mock_post.content = "This is an integration test"
            mock_extract.return_value = mock_post
            
            await handle_forum_thread_create(mock_bot, event)
            
            # Verify the complete flow
            mock_forum_agent_service.process_forum_post.assert_called_once()
            mock_rest.create_message.assert_called_once()
            
            # Verify response content
            call_args = mock_rest.create_message.call_args
            assert 'Integration Helper' in call_args[1]['content']
            assert 'integration test response' in call_args[1]['content']