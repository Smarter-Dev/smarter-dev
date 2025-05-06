"""
Example usage of the API client.

This script demonstrates how to use the API client to interact with the Smarter Dev API.
"""

import os
import sys
import asyncio
from datetime import datetime

# Add the project root to the path so we can import the bot package
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.api_client import APIClient
from bot.api_models import Guild, DiscordUser, UserWarning, ModerationCase

async def main():
    """Main function to demonstrate API client usage"""
    # Set the environment variable for local development
    os.environ["SMARTER_DEV_LOCAL"] = "1"

    # Create an API client
    client = APIClient("http://localhost:8000", "TESTING")

    try:
        # Create a guild
        guild = Guild(
            id=None,  # No ID for creation
            discord_id=123456789,
            name="Example Guild",
            icon_url="https://example.com/icon.png"
        )

        print("Creating guild...")
        created_guild = await client.create_guild(guild)
        print(f"Created guild: {created_guild.name} (ID: {created_guild.id})")

        # Create a user
        user = DiscordUser(
            id=None,  # No ID for creation
            discord_id=987654321,
            username="ExampleUser",
            discriminator="1234",
            avatar_url="https://example.com/avatar.png"
        )

        print("Creating user...")
        created_user = await client.create_user(user)
        print(f"Created user: {created_user.username} (ID: {created_user.id})")

        # Create a moderator user
        mod = DiscordUser(
            id=None,  # No ID for creation
            discord_id=555555555,
            username="ModUser",
            discriminator="5555",
            avatar_url="https://example.com/mod_avatar.png"
        )

        print("Creating moderator...")
        created_mod = await client.create_user(mod)
        print(f"Created moderator: {created_mod.username} (ID: {created_mod.id})")



        # Create a warning for the user
        warning = UserWarning(
            user_id=created_user.id,
            mod_id=created_mod.id,
            guild_id=created_guild.id,
            reason="Example warning"
        )

        print("Creating warning...")
        created_warning = await client.create_warning(warning)
        print(f"Created warning: {created_warning.id}")

        # Create a moderation case
        case = ModerationCase(
            guild_id=created_guild.id,
            user_id=created_user.id,
            mod_id=created_mod.id,
            action="timeout",
            reason="Example timeout",
            duration_sec=3600  # 1 hour
        )

        print("Creating moderation case...")
        created_case = await client.create_moderation_case(case)
        print(f"Created moderation case: #{created_case.case_number} (ID: {created_case.id})")

        # Resolve the moderation case
        print("Resolving moderation case...")
        try:
            created_case.resolved_at = datetime.now()
            created_case.resolution_note = "Example resolution"
            updated_case = await client.update_moderation_case(created_case)
            print(f"Resolved moderation case: #{updated_case.case_number}")
        except Exception as e:
            print(f"Failed to resolve moderation case: {str(e)}")
            print("This is expected in the example due to server limitations with datetime handling.")

        # Get all guilds
        print("Getting all guilds...")
        guilds = await client.get_guilds()
        print(f"Found {len(guilds)} guilds")

        # Get all users
        print("Getting all users...")
        users = await client.get_users()
        print(f"Found {len(users)} users")


        # Get all warnings for the user
        print(f"Getting warnings for user {created_user.username}...")
        user_warnings = await client.get_warnings(user_id=created_user.id)
        print(f"Found {len(user_warnings)} warnings for user")

        # Get all moderation cases for the user
        print(f"Getting moderation cases for user {created_user.username}...")
        user_cases = await client.get_moderation_cases(user_id=created_user.id)
        print(f"Found {len(user_cases)} moderation cases for user")

    finally:
        # Close the client
        await client.close()

if __name__ == "__main__":
    asyncio.run(main())
