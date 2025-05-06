# Smarter Dev Discord Bot

This directory contains both the Discord bot implementation and the API client for the Smarter Dev API, which is used by the Discord bot to interact with the website's API.

## Discord Bot

The Discord bot is implemented using Hikari and Hikari Lightbulb. It provides a simple interface for users to interact with the Smarter Dev community through Discord.

### Running the Bot

To run the bot, you need to set the `SMARTER_DEV_BOT_TOKEN` environment variable with your Discord bot token:

```bash
# Set the bot token
export SMARTER_DEV_BOT_TOKEN="your_discord_bot_token"

# Run the bot with optimization level 1 (recommended for production)
python -O -m bot.run_bot
```

### Privileged Intents

This bot uses privileged intents (`GUILD_MEMBERS` and `GUILD_PRESENCES`) to track user joins, updates, and presence changes. You need to enable these intents in the Discord Developer Portal:

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. Select your application
3. Go to the "Bot" tab
4. Scroll down to "Privileged Gateway Intents"
5. Enable "SERVER MEMBERS INTENT" and "PRESENCE INTENT"
6. Save changes

### Available Commands

- `!ping`: Checks if the bot is alive and responds with the latency
- `!sync`: Manually syncs the current guild and its members with the API (bot owner only)

### API Synchronization

The bot automatically synchronizes Discord guilds and users with the website API:

- When the bot joins a new guild, the guild is added to the website
- When a guild is updated (name, icon), the changes are synced to the website
- When a user joins a guild, their information is added to the website
- When a user updates their profile (username, avatar), the changes are synced to the website

#### Batch Processing

The bot uses batch processing to efficiently sync large numbers of users:

- When joining a new guild, all members are synced in batches of 100 users
- The `!sync` command also processes members in batches of 100 for better performance
- Progress updates are provided during batch processing

### Environment Variables

- `SMARTER_DEV_BOT_TOKEN`: Discord bot token
- `SMARTER_DEV_API_URL`: URL of the Smarter Dev API (default: http://localhost:8000)
- `SMARTER_DEV_API_KEY`: API key for authentication
- `SMARTER_DEV_LOCAL`: Set to "1" for local development mode (uses "TESTING" as API key)

## API Client Features

- Automatic token management with refresh
- Typed interfaces for all API endpoints
- Connection pooling and concurrency limits
- Retry logic for transient errors
- Dataclass models for all API resources

## Usage

### Basic Usage

```python
import asyncio
from bot.api_client import APIClient
from bot.api_models import Guild, DiscordUser

async def main():
    # Create an API client
    client = APIClient("http://localhost:8000", "YOUR_API_KEY")

    try:
        # Get all guilds
        guilds = await client.get_guilds()
        print(f"Found {len(guilds)} guilds")

        # Get all users
        users = await client.get_users()
        print(f"Found {len(users)} users")
    finally:
        # Always close the client
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
```

### Environment Variables

When `SMARTER_DEV_LOCAL` is set to `1`, the API client will accept the API key `"TESTING"` for local development.

```bash
SMARTER_DEV_LOCAL=1 python your_script.py
```

### Available Models

- `Guild`: Discord Guild model
- `DiscordUser`: Discord User model
- `GuildMember`: Guild Member model
- `UserNote`: User Note model
- `UserWarning`: User Warning model
- `ModerationCase`: Moderation Case model
- `PersistentRole`: Persistent Role model
- `TemporaryRole`: Temporary Role model
- `ChannelLock`: Channel Lock model
- `BumpStat`: Bump Stat model
- `CommandUsage`: Command Usage model

### Available Endpoints

#### Guilds
- `get_guilds()`: Get all guilds
- `get_guild(guild_id)`: Get a guild by ID
- `create_guild(guild)`: Create a new guild
- `update_guild(guild)`: Update a guild

#### Users
- `get_users()`: Get all users
- `get_user(user_id)`: Get a user by ID
- `create_user(user)`: Create a new user
- `update_user(user)`: Update a user


#### Warnings
- `get_warnings(guild_id, user_id, mod_id)`: Get warnings with optional filtering
- `get_warning_detail(warning_id)`: Get warning details
- `create_warning(warning)`: Create a new warning

#### Moderation Cases
- `get_moderation_cases(guild_id, user_id, mod_id, action, resolved)`: Get moderation cases with optional filtering
- `get_moderation_case(case_id)`: Get moderation case details
- `create_moderation_case(case)`: Create a new moderation case
- `update_moderation_case(case)`: Update a moderation case (e.g., to resolve it)

## Example

See `example.py` for a complete example of how to use the API client.

## Testing

Run the tests with:

```bash
pytest tests/test_bot_api_client.py tests/test_bot_api_models.py tests/test_bot_api_endpoints.py -v
```

## Implementation Details

### Token Management

The API client automatically manages tokens and refreshes them when they expire. Tokens are cached in memory and reused until they expire.

### Connection Pooling

The API client uses connection pooling to limit the number of concurrent connections to the server. This helps prevent overloading the server with too many requests.

### Retry Logic

The API client implements retry logic for transient errors, such as network timeouts or server errors. It uses exponential backoff to avoid overwhelming the server.

### Dataclass Models

The API client uses dataclasses to represent API resources. This provides type hints and makes it easier to work with the API.

### Error Handling

The API client raises exceptions for API errors, with detailed error messages from the server.
