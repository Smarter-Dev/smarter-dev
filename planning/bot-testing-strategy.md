# Discord Bot Testing Strategy

## Overview

Testing Discord bots requires a layered architecture where Discord-specific code is separated from business logic. This document outlines the testing approach for Hikari + Lightbulb bots.

## Architecture for Testability

### 1. Layered Design

```python
# Layer 1: Discord Event Handlers (thin wrapper)
@plugin.command
async def bytes(ctx: lightbulb.Context) -> None:
    service = ctx.bot.d.bytes_service
    result = await service.get_balance(str(ctx.guild_id), str(ctx.author.id))
    await ctx.respond(embed=result.to_embed())

# Layer 2: Service Layer (business logic - fully testable)
class BytesService:
    async def get_balance(self, guild_id: str, user_id: str) -> BytesBalance:
        # All business logic here
        # This can be tested without Discord

# Layer 3: Data Layer (API/Database access - mockable)
class APIClient:
    async def get(self, path: str) -> Response:
        # HTTP calls that can be mocked
```

### 2. Testing Approach

#### Unit Testing Services

Services contain all business logic and can be tested without any Discord dependencies:

```python
# tests/bot/test_bytes_service.py
import pytest
from unittest.mock import AsyncMock, Mock
from bot.services.bytes_service import BytesService

class TestBytesService:
    @pytest.fixture
    def mock_api(self):
        return AsyncMock()
    
    @pytest.fixture
    def service(self, mock_api):
        return BytesService(mock_api, AsyncMock())
    
    async def test_calculate_streak_multiplier(self, service):
        """Test pure business logic"""
        assert service._calculate_multiplier(0) == 1
        assert service._calculate_multiplier(7) == 2
        assert service._calculate_multiplier(14) == 4
    
    async def test_get_balance_with_caching(self, service, mock_api):
        """Test service behavior with mocked dependencies"""
        mock_api.get.return_value = Mock(
            status_code=200,
            json=lambda: {"balance": 100, "streak_count": 5}
        )
        
        # First call
        balance1 = await service.get_balance("123", "456")
        assert balance1.balance == 100
        assert mock_api.get.call_count == 1
        
        # Second call should use cache
        balance2 = await service.get_balance("123", "456")
        assert mock_api.get.call_count == 1  # No additional call
```

#### Integration Testing Commands

For testing command handlers, create mock contexts and verify the correct service methods are called:

```python
# tests/bot/test_commands.py
import pytest
from unittest.mock import AsyncMock, Mock, patch
import hikari
import lightbulb

def create_mock_context(
    guild_id: str = "123456789",
    author_id: str = "987654321",
    author_name: str = "TestUser"
) -> Mock:
    """Create a mock command context"""
    ctx = Mock(spec=lightbulb.SlashContext)
    ctx.guild_id = guild_id
    ctx.author = Mock(
        id=author_id,
        username=author_name,
        discriminator="0001",
        mention=f"<@{author_id}>"
    )
    ctx.respond = AsyncMock()
    ctx.get_guild = Mock(return_value=Mock(
        get_member=Mock(return_value=Mock(display_name=author_name))
    ))
    return ctx

class TestBytesCommands:
    @pytest.fixture
    def mock_service(self):
        service = Mock()
        service.get_balance = AsyncMock()
        service.claim_daily = AsyncMock()
        service.transfer_bytes = AsyncMock()
        return service
    
    async def test_balance_command(self, mock_service):
        """Test balance command calls service correctly"""
        from bot.plugins.bytes import balance
        
        # Setup mock context
        ctx = create_mock_context()
        ctx.bot = Mock()
        ctx.bot.d.bytes_service = mock_service
        
        # Mock service responses
        mock_service.get_balance.return_value = Mock(
            balance=100,
            streak_count=5,
            to_embed=Mock(return_value=Mock())
        )
        mock_service.claim_daily.return_value = Mock(
            success=False,
            reason="Already claimed"
        )
        
        # Execute command
        await balance(ctx)
        
        # Verify service was called correctly
        mock_service.get_balance.assert_called_once_with("123456789", "987654321")
        mock_service.claim_daily.assert_called_once()
        
        # Verify response
        ctx.respond.assert_called_once()
        assert ctx.respond.call_args[1]["embed"] is not None
```

#### Testing Views and Components

Interactive components can be tested by simulating user interactions:

```python
# tests/bot/test_views.py
class TestSquadViews:
    def test_squad_select_view_creation(self):
        """Test view creates correct components"""
        from bot.views.squad_views import SquadSelectView
        
        squads = [
            Mock(id="1", name="Alpha", switch_cost=0),
            Mock(id="2", name="Beta", switch_cost=50)
        ]
        
        view = SquadSelectView(
            squads=squads,
            current_squad=None,
            user_balance=100
        )
        
        # Verify select menu created
        assert len(view.children) == 1
        select_menu = view.children[0]
        assert len(select_menu.options) == 2
        assert select_menu.options[0].label == "Alpha"
        assert select_menu.options[1].label == "Beta"
```

#### Testing Event Listeners

Event listeners should also be thin wrappers around services:

```python
# bot/plugins/bytes.py
@plugin.listener(hikari.GuildMessageCreateEvent)
async def on_message(event: hikari.GuildMessageCreateEvent):
    if event.author.is_bot:
        return
    
    service = plugin.bot.d.activity_service
    await service.track_activity(
        str(event.guild_id),
        str(event.author_id)
    )

# tests/bot/test_listeners.py
async def test_message_listener(mock_service):
    """Test message listener calls service"""
    from bot.plugins.bytes import on_message
    
    # Create mock event
    event = Mock(spec=hikari.GuildMessageCreateEvent)
    event.guild_id = 123456789
    event.author = Mock(id=987654321, is_bot=False)
    
    # Mock plugin bot
    plugin.bot = Mock()
    plugin.bot.d.activity_service = mock_service
    
    # Execute listener
    await on_message(event)
    
    # Verify service called
    mock_service.track_activity.assert_called_once_with(
        "123456789",
        "987654321"
    )
```

### 3. Test Fixtures

Create reusable fixtures for common test scenarios:

```python
# tests/bot/conftest.py
import pytest
from unittest.mock import Mock, AsyncMock

@pytest.fixture
def mock_bot():
    """Create a mock bot instance"""
    bot = Mock()
    bot.d = Mock()
    bot.cache = Mock()
    return bot

@pytest.fixture
def mock_guild():
    """Create a mock guild with test data"""
    guild = Mock()
    guild.id = 123456789
    guild.name = "Test Guild"
    guild.get_role = Mock(side_effect=lambda role_id: Mock(
        id=role_id,
        name=f"Role {role_id}",
        color=0xFF0000
    ))
    return guild

@pytest.fixture
def mock_member():
    """Create a mock member"""
    return Mock(
        id=987654321,
        username="TestUser",
        discriminator="0001",
        display_name="TestUser",
        mention="<@987654321>"
    )
```

### 4. Testing Strategy Summary

1. **Separate Concerns**: Keep Discord-specific code minimal and put business logic in services
2. **Mock Discord Objects**: Create mock contexts, events, and Discord objects for testing
3. **Test Services Thoroughly**: Services should have comprehensive unit tests
4. **Test Integration Points**: Verify commands call services correctly
5. **Avoid Testing Discord Library**: Don't test Hikari/Lightbulb functionality, only your code

### 5. Example Test Structure

```
tests/
├── bot/
│   ├── conftest.py          # Bot-specific fixtures
│   ├── test_services/       # Service unit tests
│   │   ├── test_bytes_service.py
│   │   └── test_squads_service.py
│   ├── test_commands/       # Command integration tests
│   │   ├── test_bytes_commands.py
│   │   └── test_squads_commands.py
│   ├── test_listeners/      # Event listener tests
│   │   └── test_activity_tracking.py
│   └── test_views/          # Interactive component tests
│       └── test_squad_views.py
└── conftest.py              # Shared fixtures
```

This approach ensures high test coverage while keeping tests fast and maintainable. The key is treating Discord as an external dependency that can be mocked, similar to how you would mock a database or external API.