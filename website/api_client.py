import httpx
from typing import Optional, Dict, Any

class APIClient:
    def __init__(self, base_url: str = "http://localhost:8000/api", api_token: Optional[str] = None):
        self.base_url = base_url
        self.api_token = api_token
        self.client = httpx.AsyncClient()

    def _get_headers(self) -> Dict[str, str]:
        """Get headers with API token if available"""
        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    async def get_file_extension_rules(self, guild_id: int) -> httpx.Response:
        """Get file extension rules for a guild"""
        return await self.client.get(
            f"{self.base_url}/automod/file-extensions/{guild_id}",
            headers=self._get_headers()
        )

    async def get_rate_limits(self, guild_id: int) -> httpx.Response:
        """Get rate limits for a guild"""
        return await self.client.get(
            f"{self.base_url}/automod/rate-limits",
            params={"guild_id": guild_id},
            headers=self._get_headers()
        )

    async def get_regex_rules(self, guild_id: int) -> httpx.Response:
        """Get regex rules for a guild"""
        return await self.client.get(
            f"{self.base_url}/automod/regex-rules",
            params={"guild_id": guild_id},
            headers=self._get_headers()
        )

    async def create_file_extension_rule(self, data: Dict[str, Any]) -> httpx.Response:
        """Create a new file extension rule"""
        return await self.client.post(
            f"{self.base_url}/automod/file-extensions",
            json=data,
            headers=self._get_headers()
        )

    async def update_file_extension_rule(self, rule_id: int, data: Dict[str, Any]) -> httpx.Response:
        """Update a file extension rule"""
        return await self.client.put(
            f"{self.base_url}/automod/file-extensions/{rule_id}",
            json=data,
            headers=self._get_headers()
        )

    async def delete_file_extension_rule(self, rule_id: int) -> httpx.Response:
        """Delete a file extension rule"""
        return await self.client.delete(
            f"{self.base_url}/automod/file-extensions/{rule_id}",
            headers=self._get_headers()
        )

    async def close(self):
        """Close the HTTP client"""
        await self.client.aclose() 