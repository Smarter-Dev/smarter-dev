"""
API Client for interacting with the Smarter Dev API.

This module provides a client for interacting with the Smarter Dev API,
handling authentication, token refresh, and providing typed interfaces
for all API endpoints.
"""

import os
import time
import json
import asyncio
from typing import Dict, List, Optional, Any, TypeVar, Generic, Union, cast
from dataclasses import dataclass, field, asdict
from datetime import datetime
import httpx
from httpx import Limits, TransportError

from .api_models import (
    Guild, DiscordUser, GuildMember, Kudos, UserNote, UserWarning,
    ModerationCase, PersistentRole, TemporaryRole, ChannelLock,
    BumpStat, CommandUsage, Bytes, BytesConfig, BytesRole, BytesCooldown
)

# Type variable for generic response handling
T = TypeVar('T')

@dataclass
class TokenResponse:
    """Response from the token endpoint"""
    token: str
    expires_in: int
    expires_at: float = field(init=False)

    def __post_init__(self):
        # Calculate the expiration timestamp
        self.expires_at = time.time() + self.expires_in - 60  # Subtract 60 seconds for safety margin

@dataclass
class APIResponse(Generic[T]):
    """Generic API response wrapper"""
    data: T
    status_code: int
    success: bool

class APIClient:
    """
    Client for interacting with the Smarter Dev API.

    This client handles authentication, token refresh, and provides
    typed interfaces for all API endpoints.
    """

    def __init__(self, base_url: str, api_key: str):
        """
        Initialize the API client.

        Args:
            base_url: The base URL of the API
            api_key: The API key to use for authentication
        """
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.token: Optional[TokenResponse] = None

        # Configure client with connection pooling and limits
        limits = Limits(max_connections=5, max_keepalive_connections=5)
        self.client = httpx.AsyncClient(
            timeout=30.0,
            limits=limits,
            http2=False  # Disable HTTP/2 to avoid connection pool issues
        )

        # Semaphore to limit concurrent requests
        self._request_semaphore = asyncio.Semaphore(3)  # Max 3 concurrent requests

    async def close(self):
        """Close the underlying HTTP client"""
        await self.client.aclose()

    async def get_token(self) -> TokenResponse:
        """
        Get a valid token, refreshing if necessary.

        Returns:
            A valid token response
        """
        # If we have a token and it's not expired, return it
        if self.token and time.time() < self.token.expires_at:
            return self.token

        # Otherwise, get a new token
        url = f"{self.base_url}/api/auth/token"
        response = await self.client.post(
            url,
            json={"api_key": self.api_key}
        )

        if response.status_code != 200:
            raise Exception(f"Failed to get token: {response.text}")

        data = await self._get_json(response)
        self.token = TokenResponse(
            token=data["token"],
            expires_in=data["expires_in"]
        )

        return self.token

    async def _request(
        self,
        method: str,
        path: str,
        data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 3
    ) -> httpx.Response:
        """
        Make an authenticated request to the API.

        Args:
            method: The HTTP method to use
            path: The API path (without the base URL)
            data: Optional JSON data to send
            params: Optional query parameters
            max_retries: Maximum number of retries for transient errors

        Returns:
            The HTTP response
        """
        # Use semaphore to limit concurrent requests
        async with self._request_semaphore:
            # Get a valid token
            token = await self.get_token()

            # Build the URL
            url = f"{self.base_url}{path}"

            # Make the request with the token
            headers = {
                "Authorization": f"Bearer {token.token}",
                "Content-Type": "application/json"
            }

            # Implement retry logic
            retry_count = 0
            while True:
                try:
                    response = await self.client.request(
                        method=method,
                        url=url,
                        json=data,
                        params=params,
                        headers=headers
                    )

                    # Check for error status codes
                    if response.status_code >= 400:
                        # Handle both async and sync cases for text
                        if callable(response.text):
                            try:
                                error_text = await response.text()
                            except Exception:
                                error_text = response.text()
                        else:
                            error_text = response.text

                        # For 5xx errors, retry if we haven't exceeded max_retries
                        if response.status_code >= 500 and retry_count < max_retries:
                            retry_count += 1
                            # Exponential backoff
                            await asyncio.sleep(2 ** retry_count)
                            continue

                        raise Exception(f"API request failed with status {response.status_code}: {error_text}")

                    return response

                except (httpx.ReadTimeout, httpx.ConnectTimeout, TransportError) as e:
                    # Retry transient errors
                    if retry_count < max_retries:
                        retry_count += 1
                        # Exponential backoff
                        await asyncio.sleep(2 ** retry_count)
                        continue
                    raise Exception(f"Request failed after {max_retries} retries: {str(e)}")

    # Helper methods for converting between API responses and dataclasses
    def _parse_datetime(self, dt_str: Optional[str]) -> Optional[datetime]:
        """Parse a datetime string from the API"""
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str)
        except ValueError:
            return None

    def _model_from_dict(self, model_class, data: Dict[str, Any]):
        """Create a model instance from a dictionary"""
        # Handle datetime fields
        for key, value in list(data.items()):
            if key.endswith('_at') and value:
                data[key] = self._parse_datetime(value)

        # Create the model instance
        return model_class(**data)

    def _dict_from_model(self, model):
        """Convert a model to a dictionary for API requests"""
        data = asdict(model)

        # Convert datetime objects to ISO format strings
        for key, value in list(data.items()):
            if isinstance(value, datetime):
                # Convert datetime objects to ISO format strings for JSON serialization
                data[key] = value.isoformat()
            elif value is None and key not in ['id', 'case_number']:  # Keep id and case_number even if None
                del data[key]  # Remove None values

        return data

    async def _get_json(self, response):
        """Get JSON from a response, handling both async and sync cases"""
        if callable(response.json):
            try:
                return await response.json()
            except Exception as e:
                # If it fails as a coroutine, try as a regular method
                return response.json()
        else:
            # It's a property or mock
            return response.json

    # Guild endpoints
    async def get_guilds(self) -> List[Guild]:
        """Get all guilds"""
        response = await self._request("GET", "/api/guilds")
        data = await self._get_json(response)
        return [self._model_from_dict(Guild, guild) for guild in data["guilds"]]

    async def get_guild(self, guild_id: int) -> Guild:
        """Get a guild by ID"""
        response = await self._request("GET", f"/api/guilds/{guild_id}")
        data = await self._get_json(response)
        return self._model_from_dict(Guild, data)

    async def create_guild(self, guild: Guild) -> Guild:
        """Create a new guild"""
        data = self._dict_from_model(guild)
        response = await self._request("POST", "/api/guilds", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(Guild, result)

    async def update_guild(self, guild: Guild) -> Guild:
        """Update a guild"""
        data = self._dict_from_model(guild)
        response = await self._request("PUT", f"/api/guilds/{guild.id}", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(Guild, result)

    # User endpoints
    async def get_users(self) -> List[DiscordUser]:
        """Get all users"""
        response = await self._request("GET", "/api/users")
        data = await self._get_json(response)
        return [self._model_from_dict(DiscordUser, user) for user in data["users"]]

    async def get_user(self, user_id: int) -> DiscordUser:
        """Get a user by ID"""
        response = await self._request("GET", f"/api/users/{user_id}")
        data = await self._get_json(response)
        return self._model_from_dict(DiscordUser, data)

    async def create_user(self, user: DiscordUser) -> DiscordUser:
        """Create a new user"""
        data = self._dict_from_model(user)
        response = await self._request("POST", "/api/users", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(DiscordUser, result)

    async def update_user(self, user: DiscordUser) -> DiscordUser:
        """Update a user"""
        data = self._dict_from_model(user)
        response = await self._request("PUT", f"/api/users/{user.id}", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(DiscordUser, result)

    async def batch_create_users(self, users: List[DiscordUser]) -> Dict[str, Any]:
        """Create multiple users in a single request

        Args:
            users: List of DiscordUser objects to create or update

        Returns:
            Dictionary with results including created users and counts
        """
        if not users:
            return {"users": [], "created": 0, "updated": 0, "total": 0}

        # Convert all users to dictionaries
        data = [self._dict_from_model(user) for user in users]

        # Make the batch request
        response = await self._request("POST", "/api/users/batch", data=data)
        result = await self._get_json(response)

        # Convert the returned users to DiscordUser objects
        result["users"] = [self._model_from_dict(DiscordUser, user) for user in result["users"]]

        return result

    # Kudos endpoints (legacy)
    async def get_kudos(self, guild_id: Optional[int] = None, user_id: Optional[int] = None,
                      giver_id: Optional[int] = None, receiver_id: Optional[int] = None) -> List[Kudos]:
        """Get kudos with optional filtering"""
        params = {}
        if guild_id:
            params["guild_id"] = guild_id
        if user_id:
            params["user_id"] = user_id
        if giver_id:
            params["giver_id"] = giver_id
        if receiver_id:
            params["receiver_id"] = receiver_id

        response = await self._request("GET", "/api/kudos", params=params)
        data = await self._get_json(response)
        return [self._model_from_dict(Kudos, k) for k in data["kudos"]]

    async def get_kudos_detail(self, kudos_id: int) -> Kudos:
        """Get kudos details"""
        response = await self._request("GET", f"/api/kudos/{kudos_id}")
        data = await self._get_json(response)
        return self._model_from_dict(Kudos, data)

    async def create_kudos(self, kudos: Kudos) -> Kudos:
        """Create new kudos"""
        data = self._dict_from_model(kudos)
        response = await self._request("POST", "/api/kudos", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(Kudos, result)

    # Bytes endpoints
    async def get_bytes(self, guild_id: Optional[int] = None, user_id: Optional[int] = None,
                      giver_id: Optional[int] = None, receiver_id: Optional[int] = None) -> List[Bytes]:
        """Get bytes with optional filtering"""
        params = {}
        if guild_id:
            params["guild_id"] = guild_id
        if user_id:
            params["user_id"] = user_id
        if giver_id:
            params["giver_id"] = giver_id
        if receiver_id:
            params["receiver_id"] = receiver_id

        response = await self._request("GET", "/api/bytes", params=params)
        data = await self._get_json(response)
        return [self._model_from_dict(Bytes, b) for b in data["bytes"]]

    async def get_bytes_detail(self, bytes_id: int) -> Bytes:
        """Get bytes details"""
        response = await self._request("GET", f"/api/bytes/{bytes_id}")
        data = await self._get_json(response)
        return self._model_from_dict(Bytes, data)

    async def create_bytes(self, bytes_obj: Bytes) -> Bytes:
        """Create new bytes transaction"""
        data = self._dict_from_model(bytes_obj)
        response = await self._request("POST", "/api/bytes", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(Bytes, result)

    # Bytes Config endpoints
    async def get_bytes_config(self, guild_id: int) -> BytesConfig:
        """Get bytes configuration for a guild"""
        response = await self._request("GET", f"/api/bytes/config/{guild_id}")
        data = await self._get_json(response)
        return self._model_from_dict(BytesConfig, data)

    async def create_bytes_config(self, config: BytesConfig) -> BytesConfig:
        """Create or update bytes configuration"""
        data = self._dict_from_model(config)
        response = await self._request("POST", "/api/bytes/config", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(BytesConfig, result)

    async def update_bytes_config(self, config: BytesConfig) -> BytesConfig:
        """Update bytes configuration"""
        data = self._dict_from_model(config)
        response = await self._request("PUT", f"/api/bytes/config/{config.guild_id}", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(BytesConfig, result)

    # Bytes Role endpoints
    async def get_bytes_roles(self, guild_id: int) -> List[BytesRole]:
        """Get bytes roles for a guild"""
        response = await self._request("GET", f"/api/bytes/roles/{guild_id}")
        data = await self._get_json(response)
        return [self._model_from_dict(BytesRole, r) for r in data["roles"]]

    async def create_bytes_role(self, role: BytesRole) -> BytesRole:
        """Create a new bytes role"""
        data = self._dict_from_model(role)
        response = await self._request("POST", "/api/bytes/roles", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(BytesRole, result)

    async def update_bytes_role(self, role: BytesRole) -> BytesRole:
        """Update a bytes role"""
        data = self._dict_from_model(role)
        response = await self._request("PUT", f"/api/bytes/roles/{role.id}", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(BytesRole, result)

    async def delete_bytes_role(self, role_id: int) -> Dict[str, Any]:
        """Delete a bytes role"""
        response = await self._request("DELETE", f"/api/bytes/roles/{role_id}")
        return await self._get_json(response)

    # Bytes Cooldown endpoints
    async def get_bytes_cooldown(self, user_id: int, guild_id: int) -> Optional[BytesCooldown]:
        """Get bytes cooldown for a user in a guild"""
        try:
            response = await self._request("GET", f"/api/bytes/cooldown/{user_id}/{guild_id}")
            data = await self._get_json(response)
            return self._model_from_dict(BytesCooldown, data)
        except Exception:
            return None

    # Warning endpoints
    async def get_warnings(self, guild_id: Optional[int] = None, user_id: Optional[int] = None,
                         mod_id: Optional[int] = None) -> List[UserWarning]:
        """Get warnings with optional filtering"""
        params = {}
        if guild_id:
            params["guild_id"] = guild_id
        if user_id:
            params["user_id"] = user_id
        if mod_id:
            params["mod_id"] = mod_id

        response = await self._request("GET", "/api/warnings", params=params)
        data = await self._get_json(response)
        return [self._model_from_dict(UserWarning, w) for w in data["warnings"]]

    async def get_warning_detail(self, warning_id: int) -> UserWarning:
        """Get warning details"""
        response = await self._request("GET", f"/api/warnings/{warning_id}")
        data = await self._get_json(response)
        return self._model_from_dict(UserWarning, data)

    async def create_warning(self, warning: UserWarning) -> UserWarning:
        """Create a new warning"""
        data = self._dict_from_model(warning)
        response = await self._request("POST", "/api/warnings", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(UserWarning, result)

    # Moderation case endpoints
    async def get_moderation_cases(self, guild_id: Optional[int] = None, user_id: Optional[int] = None,
                                mod_id: Optional[int] = None, action: Optional[str] = None,
                                resolved: Optional[bool] = None) -> List[ModerationCase]:
        """Get moderation cases with optional filtering"""
        params = {}
        if guild_id:
            params["guild_id"] = guild_id
        if user_id:
            params["user_id"] = user_id
        if mod_id:
            params["mod_id"] = mod_id
        if action:
            params["action"] = action
        if resolved is not None:
            params["resolved"] = str(resolved).lower()

        response = await self._request("GET", "/api/moderation-cases", params=params)
        data = await self._get_json(response)
        return [self._model_from_dict(ModerationCase, c) for c in data["cases"]]

    async def get_moderation_case(self, case_id: int) -> ModerationCase:
        """Get moderation case details"""
        response = await self._request("GET", f"/api/moderation-cases/{case_id}")
        data = await self._get_json(response)
        return self._model_from_dict(ModerationCase, data)

    async def create_moderation_case(self, case: ModerationCase) -> ModerationCase:
        """Create a new moderation case"""
        data = self._dict_from_model(case)
        response = await self._request("POST", "/api/moderation-cases", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(ModerationCase, result)

    async def update_moderation_case(self, case: ModerationCase) -> ModerationCase:
        """Update a moderation case (e.g., to resolve it)"""
        data = self._dict_from_model(case)
        response = await self._request("PUT", f"/api/moderation-cases/{case.id}", data=data)
        result = await self._get_json(response)
        return self._model_from_dict(ModerationCase, result)
