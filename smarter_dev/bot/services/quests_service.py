"""Squad management service for Discord bot.

This module implements the complete business logic for the squad system,
including squad listing, membership management, and join/leave operations.
All operations are fully testable and Discord-agnostic.
"""

from __future__ import annotations
import logging
import asyncio
from datetime import UTC, timezone
from datetime import datetime
from typing import Any

import hikari
from smarter_dev.bot.services.api_client import APIClient
from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.cache_manager import CacheManager
from smarter_dev.bot.services.models import ServiceHealth

logger = logging.getLogger(__name__)

class QuestService(BaseService):
    """Service for managing quest announcements and release scheduling."""

    def __init__(
        self,
        api_client: APIClient,
        cache_manager: CacheManager | None,
        bot: hikari.BotApp,
    ):
        super().__init__(api_client, cache_manager)
        self._bot = bot
        self._announcement_task: asyncio.Task | None = None
        self._running = False
        self._queued_quests: set[str] = set()

    async def initialize(self) -> None:
        await super().initialize()
        await self.start_announcement_scheduler()
        logger.info("Quest service initialized with announcement scheduler")

    async def cleanup(self) -> None:
        await self.stop_announcement_scheduler()
        await super().cleanup()
        logger.info("Quest service cleaned up")

    async def health_check(self) -> ServiceHealth:
        try:
            scheduler_status = "running" if self._running and self._announcement_task else "stopped"
            return ServiceHealth(
                service_name="QuestService",
                is_healthy=True,
                details={
                    "scheduler_status": scheduler_status,
                    "bot_connected": self._bot.is_alive if hasattr(self._bot, "is_alive") else True,
                },
            )
        except Exception as e:
            logger.error(f"Quest service health check failed: {e}")
            return ServiceHealth(
                service_name="QuestService",
                is_healthy=False,
                details={"error": str(e)},
            )

    async def start_announcement_scheduler(self) -> None:
        if self._running:
            return
        self._running = True
        self._announcement_task = asyncio.create_task(self._announcement_loop())
        logger.info("Started quest announcement scheduler")

    async def stop_announcement_scheduler(self) -> None:
        self._running = False
        if self._announcement_task:
            self._announcement_task.cancel()
            try:
                await self._announcement_task
            except asyncio.CancelledError:
                pass
            self._announcement_task = None
        logger.info("Stopped quest announcement scheduler")

    async def _announcement_loop(self) -> None:
        while self._running:
            try:
                await self._check_and_queue_quests()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in quest announcement loop: {e}")

            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                break

    async def _check_and_queue_quests(self) -> None:
        try:
            response = await self._api_client.get("/quests/upcoming-announcements?seconds=45")
            quests = response.json().get("quests", [])

            for quest in quests:
                quest_id = quest.get("id")
                if quest_id and quest_id not in self._queued_quests:
                    self._queued_quests.add(quest_id)
                    asyncio.create_task(self._queue_and_announce_quest(quest))
        except Exception as e:
            logger.error(f"Failed checking upcoming quests: {e}")

    async def _queue_and_announce_quest(self, quest: dict[str, Any]) -> None:
        quest_id = quest.get("id")
        try:
            release_time = datetime.fromisoformat(
                quest["release_time"].replace("Z", "+00:00")
            )

            # 🔧 FIX: normalize to UTC if naive
            if release_time.tzinfo is None:
                release_time = release_time.replace(tzinfo=timezone.utc)

            delay = (release_time - datetime.now(timezone.utc)).total_seconds()

            if delay > 0:
                await asyncio.sleep(delay)

            await self._announce_quest(quest)

        finally:
            if quest_id:
                self._queued_quests.discard(quest_id)

    async def _get_squad_channels(self, guild_id: str) -> dict[str, dict[str, Any]]:
        try:
            response = await self._api_client.get(f"/guilds/{guild_id}/squads/")
            squads = response.json()

            channels = {}
            for squad in squads:
                if squad.get("is_active") and squad.get("announcement_channel"):
                    channels[squad["announcement_channel"]] = {
                        "name": squad.get("name"),
                        "role_id": squad.get("role_id"),
                    }
            return channels
        except Exception as e:
            logger.error(f"Failed to get squad channels for guild {guild_id}: {e}")
            return {}

    async def _announce_quest(self, quest: dict[str, Any]) -> None:
        quest_id = quest["id"]
        title = quest.get("title", "Daily Quest")
        description = quest.get("description", "")
        guild_id = quest["guild_id"]

        squad_channels = await self._get_squad_channels(guild_id)
        if not squad_channels:
            logger.warning(f"No squad channels found for quest {quest_id}")
            return

        for channel_id, squad_info in squad_channels.items():
            message = self._format_quest_announcement(
                title, description, squad_info.get("role_id")
            )
            await self._send_quest_message(channel_id, message, quest_id)

        try:
            await self._api_client.post(f"/quests/{quest_id}/mark-announced")
            await self._api_client.post(f"/quests/{quest_id}/mark-active")
        except Exception as e:
            logger.error(f"Failed to mark quest {quest_id} announced/active: {e}")

    def _format_quest_announcement(
        self,
        title: str,
        description: str,
        role_id: str | None,
    ) -> str:
        mention = f"<@&{role_id}>\n\n" if role_id else ""
        msg = f"{mention}# 🧭 {title}\n{description}"

        if len(msg) > 2000:
            msg = msg[:1997] + "..."
        return msg

    async def _send_quest_message(
        self,
        channel_id: str,
        message: str,
        quest_id: str,
    ) -> None:
        channel_id_int = int(channel_id)

        get_input_button = hikari.impl.InteractiveButtonBuilder(
            style=hikari.ButtonStyle.PRIMARY,
            custom_id=f"get_daily_quest_input:{quest_id}",
            emoji="📥",
            label="Get Input",
        )

        submit_button = hikari.impl.InteractiveButtonBuilder(
            style=hikari.ButtonStyle.SUCCESS,
            custom_id=f"submit_daily_quest:{quest_id}",
            emoji="📤",
            label="Submit",
        )

        row = hikari.impl.MessageActionRowBuilder()
        row.add_component(get_input_button)
        row.add_component(submit_button)

        msg = await self._bot.rest.create_message(
            channel_id_int,
            content=message,
            components=[row],
            role_mentions=True,
        )

        await self._pin_message_with_retry(channel_id_int, msg.id)

    async def _pin_message_with_retry(
        self,
        channel_id: int,
        message_id: int,
        max_retries: int = 3,
    ) -> None:
        for attempt in range(max_retries + 1):
            try:
                await self._bot.rest.pin_message(channel_id, message_id)
                return
            except hikari.ForbiddenError:
                return
            except Exception:
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
