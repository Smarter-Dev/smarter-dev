"""Discord OAuth service for admin authentication."""

from __future__ import annotations

import logging
import secrets
from typing import Dict, List, Optional, Any
from urllib.parse import urlencode

import httpx
from starlette.requests import Request

from smarter_dev.shared.config import get_settings
from smarter_dev.web.admin.discord import get_bot_guilds, DiscordGuild

logger = logging.getLogger(__name__)


class DiscordOAuthError(Exception):
    """Exception raised for Discord OAuth errors."""
    pass


class InsufficientPermissionsError(DiscordOAuthError):
    """Exception raised when user doesn't own any guilds where bot is present."""
    pass


class DiscordUser:
    """Discord user information from OAuth."""
    
    def __init__(self, data: Dict[str, Any]):
        self.id: str = data["id"]
        self.username: str = data["username"]
        self.discriminator: str = data["discriminator"]
        self.avatar: Optional[str] = data.get("avatar")
        self.email: Optional[str] = data.get("email")
        self.verified: bool = data.get("verified", False)
    
    @property
    def display_name(self) -> str:
        """Get user's display name."""
        if self.discriminator and self.discriminator != "0":
            return f"{self.username}#{self.discriminator}"
        return f"@{self.username}"
    
    @property
    def avatar_url(self) -> Optional[str]:
        """Get user's avatar URL."""
        if self.avatar:
            return f"https://cdn.discordapp.com/avatars/{self.id}/{self.avatar}.png"
        return None


class DiscordOAuthService:
    """Discord OAuth service for admin authentication."""
    
    def __init__(self):
        """Initialize Discord OAuth service."""
        self.settings = get_settings()
        self._validate_settings()
    
    def _validate_settings(self) -> None:
        """Validate OAuth settings."""
        if not self.settings.discord_client_id:
            raise DiscordOAuthError("Discord client ID not configured")
        
        if not self.settings.discord_client_secret:
            raise DiscordOAuthError("Discord client secret not configured")
    
    def get_authorization_url(self, request: Request) -> str:
        """Generate Discord OAuth authorization URL.
        
        Args:
            request: Starlette request object
            
        Returns:
            Authorization URL for Discord OAuth
        """
        # Generate and store state for CSRF protection
        state = secrets.token_urlsafe(32)
        request.session['oauth_state'] = state
        
        # Store the original path for redirect after login
        next_path = request.query_params.get('next', '/admin')
        request.session['oauth_next'] = next_path
        
        # Build Discord OAuth URL manually
        params = {
            'client_id': self.settings.discord_client_id,
            'redirect_uri': self.settings.effective_discord_redirect_uri,
            'response_type': 'code',
            'scope': 'identify email guilds',
            'state': state,
        }
        
        auth_url = f"https://discord.com/oauth2/authorize?{urlencode(params)}"
        return auth_url
    
    async def handle_callback(self, request: Request) -> DiscordUser:
        """Handle Discord OAuth callback.
        
        Args:
            request: Starlette request object with callback parameters
            
        Returns:
            Authenticated Discord user
            
        Raises:
            DiscordOAuthError: For various OAuth errors
            InsufficientPermissionsError: If user doesn't own required guilds
        """
        try:
            # Verify state to prevent CSRF attacks
            state = request.query_params.get('state')
            stored_state = request.session.get('oauth_state')
            
            if not state or not stored_state or state != stored_state:
                raise DiscordOAuthError("Invalid state parameter - possible CSRF attack")
            
            # Clean up state from session
            request.session.pop('oauth_state', None)
            
            # Get authorization code
            code = request.query_params.get('code')
            if not code:
                error = request.query_params.get('error', 'unknown_error')
                error_description = request.query_params.get('error_description', 'No description provided')
                raise DiscordOAuthError(f"OAuth error: {error} - {error_description}")
            
            # Exchange code for access token using direct HTTP request
            token_data = await self._exchange_code_for_token(code)
            access_token = token_data['access_token']
            
            # Get user information from Discord API
            user_data = await self._get_discord_user(access_token)
            user = DiscordUser(user_data)
            
            # Verify user has required permissions (owns guilds where bot is present)
            await self._verify_guild_ownership(user, access_token)
            
            logger.info(f"Discord OAuth successful for user: {user.display_name} (ID: {user.id})")
            return user
            
        except InsufficientPermissionsError:
            # Re-raise permission errors as-is so they can be handled specifically
            raise
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error during Discord API call: {e}")
            raise DiscordOAuthError(f"Discord API error: {e.response.status_code}")
        except httpx.RequestError as e:
            logger.error(f"Request error during Discord API call: {e}")
            raise DiscordOAuthError(f"Network error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during OAuth callback: {e}")
            raise DiscordOAuthError(f"Unexpected error: {e}")
    
    async def _exchange_code_for_token(self, code: str) -> Dict[str, Any]:
        """Exchange authorization code for access token.
        
        Args:
            code: Authorization code from Discord
            
        Returns:
            Token response data
        """
        data = {
            'client_id': self.settings.discord_client_id,
            'client_secret': self.settings.discord_client_secret,
            'grant_type': 'authorization_code',
            'code': code,
            'redirect_uri': self.settings.effective_discord_redirect_uri,
        }
        
        async with httpx.AsyncClient() as client:
            response = await client.post(
                'https://discord.com/api/oauth2/token',
                data=data,
                headers={'Content-Type': 'application/x-www-form-urlencoded'}
            )
            response.raise_for_status()
            return response.json()
    
    async def _get_discord_user(self, access_token: str) -> Dict[str, Any]:
        """Get Discord user information using access token.
        
        Args:
            access_token: OAuth access token
            
        Returns:
            User data from Discord API
        """
        async with httpx.AsyncClient() as client:
            headers = {'Authorization': f'Bearer {access_token}'}
            response = await client.get('https://discord.com/api/users/@me', headers=headers)
            response.raise_for_status()
            return response.json()
    
    async def _verify_guild_ownership(self, user: DiscordUser, access_token: str) -> None:
        """Verify user owns guilds where the bot is present.
        
        Args:
            user: Discord user to verify
            access_token: OAuth access token for API calls
            
        Raises:
            InsufficientPermissionsError: If user doesn't own required guilds
        """
        try:
            # Get user's guilds from Discord API
            async with httpx.AsyncClient() as client:
                headers = {'Authorization': f'Bearer {access_token}'}
                response = await client.get('https://discord.com/api/users/@me/guilds', headers=headers)
                response.raise_for_status()
                user_guilds_data = response.json()
            
            # Filter guilds where user is owner
            owned_guild_ids = set()
            for guild_data in user_guilds_data:
                # Check if user is owner (owner flag) or has admin permissions
                if guild_data.get('owner', False) or (int(guild_data.get('permissions', 0)) & 0x8) != 0:
                    owned_guild_ids.add(guild_data['id'])
            
            logger.debug(f"User {user.display_name} owns {len(owned_guild_ids)} guilds")
            
            # Get bot's guilds
            bot_guilds = await get_bot_guilds()
            bot_guild_ids = {guild.id for guild in bot_guilds}
            
            logger.debug(f"Bot is present in {len(bot_guild_ids)} guilds")
            
            # Check intersection - user must own at least one guild where bot is present
            common_guilds = owned_guild_ids & bot_guild_ids
            
            if not common_guilds:
                logger.warning(
                    f"User {user.display_name} does not own any guilds where bot is present. "
                    f"Owned guilds: {owned_guild_ids}, Bot guilds: {bot_guild_ids}"
                )
                raise InsufficientPermissionsError(
                    "Sorry, you don't have permission to access this admin panel. "
                    "You need to own a Discord server where our bot is installed."
                )
            
            logger.info(f"User {user.display_name} verified with access to {len(common_guilds)} common guilds")
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Error fetching user guilds: {e}")
            raise DiscordOAuthError("Failed to verify guild ownership")
        except InsufficientPermissionsError:
            raise
        except Exception as e:
            logger.error(f"Unexpected error verifying guild ownership: {e}")
            raise DiscordOAuthError("Failed to verify permissions")


# Global OAuth service instance
_oauth_service: Optional[DiscordOAuthService] = None


def get_discord_oauth_service() -> DiscordOAuthService:
    """Get the global Discord OAuth service instance.
    
    Returns:
        Configured Discord OAuth service
        
    Raises:
        DiscordOAuthError: If service configuration is invalid
    """
    global _oauth_service
    
    if _oauth_service is None:
        _oauth_service = DiscordOAuthService()
    
    return _oauth_service