"""
Discord REST API module for interacting with the Discord API.
This module provides functions to interact with Discord's API using Hikari's REST client.
"""
import os
import hikari
from typing import List, Dict, Any, Optional

async def get_guild_roles(guild_id: int) -> List[Dict[str, Any]]:
    """
    Fetch roles for a guild using Hikari's REST API.

    Args:
        guild_id: The Discord ID of the guild.

    Returns:
        A list of role dictionaries with id, name, color, and position.
    """
    # Get the bot token from environment variables
    token = os.environ.get("SMARTER_DEV_BOT_TOKEN")
    if not token:
        print("ERROR: SMARTER_DEV_BOT_TOKEN environment variable not set")
        return []

    roles = []

    # Initialize the REST application
    rest_app = hikari.RESTApp()
    try:
        # Start the REST app
        await rest_app.start()

        # Acquire a REST client with the bot token
        async with rest_app.acquire(token, "Bot") as rest:
            try:
                # Fetch all roles in the guild
                discord_roles = await rest.fetch_roles(guild_id)

                # Convert to a format suitable for templates
                roles = [
                    {
                        "id": str(role.id),
                        "name": role.name,
                        "color": f"#{role.color.hex_code}" if role.color and not role.color.hex_code.startswith('#') else (role.color.hex_code if role.color else "#000000"),
                        "position": role.position,
                        "icon_url": str(role.icon_url) if role.icon_url else None
                    }
                    for role in discord_roles
                ]

                # Sort roles by position (highest first)
                roles.sort(key=lambda r: r["position"], reverse=True)

            except Exception as e:
                print(f"Error fetching roles for guild {guild_id}: {e}")
                import traceback
                traceback.print_exc()
    except Exception as e:
        print(f"Error setting up REST app: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Close the REST app
        await rest_app.close()

    return roles

async def get_role_name(guild_id: int, role_id: int) -> Optional[str]:
    """
    Fetch the name of a role using Hikari's REST API.

    Args:
        guild_id: The Discord ID of the guild.
        role_id: The Discord ID of the role.

    Returns:
        The name of the role, or None if not found.
    """
    # Get the bot token from environment variables
    token = os.environ.get("SMARTER_DEV_BOT_TOKEN")
    if not token:
        print("ERROR: SMARTER_DEV_BOT_TOKEN environment variable not set")
        return None

    # Initialize the REST application
    rest_app = hikari.RESTApp()
    try:
        # Start the REST app
        await rest_app.start()

        # Acquire a REST client with the bot token
        async with rest_app.acquire(token, "Bot") as rest:
            try:
                # Fetch all roles in the guild
                discord_roles = await rest.fetch_roles(guild_id)

                # Find the role with the matching ID
                for role in discord_roles:
                    if role.id == role_id:
                        return role.name

            except Exception as e:
                print(f"Error fetching roles for guild {guild_id}: {e}")
    except Exception as e:
        print(f"Error setting up REST app: {e}")
    finally:
        # Close the REST app
        await rest_app.close()

    return None
