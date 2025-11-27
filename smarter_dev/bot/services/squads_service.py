"""Squad management service for Discord bot.

This module implements the complete business logic for the squad system,
including squad listing, membership management, and join/leave operations.
All operations are fully testable and Discord-agnostic.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from uuid import UUID

from smarter_dev.bot.services.base import APIClientProtocol
from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.base import CacheManagerProtocol
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.exceptions import NotInSquadError
from smarter_dev.bot.services.exceptions import ResourceNotFoundError
from smarter_dev.bot.services.exceptions import ServiceError
from smarter_dev.bot.services.exceptions import ValidationError
from smarter_dev.bot.services.models import JoinSquadResult
from smarter_dev.bot.services.models import Squad
from smarter_dev.bot.services.models import SquadMember
from smarter_dev.bot.services.models import UserSquadResponse

logger = logging.getLogger(__name__)


class SquadsService(BaseService):
    """Production-grade squad management service.

    This service handles all squad-related operations including:
    - Squad listing and filtering
    - User squad membership tracking
    - Join/leave operations with cost validation
    - Squad member management
    - Admin squad operations

    Features:
    - Intelligent caching with configurable TTLs
    - Comprehensive error handling and validation
    - Balance integration for join costs
    - Performance optimizations for large guilds
    - Full observability and monitoring
    """

    # Cache TTL configurations (in seconds)
    CACHE_TTL_SQUADS = 300  # 5 minutes
    CACHE_TTL_USER_SQUAD = 180  # 3 minutes
    CACHE_TTL_SQUAD_MEMBERS = 120  # 2 minutes

    def __init__(
        self,
        api_client: APIClientProtocol,
        cache_manager: CacheManagerProtocol | None = None
    ):
        """Initialize squads service.

        Args:
            api_client: API client for backend communication
            cache_manager: Cache manager for performance optimization
        """
        super().__init__(api_client, cache_manager, "SquadsService")

        # Performance tracking
        self._squad_list_requests = 0
        self._join_attempts = 0
        self._leave_attempts = 0
        self._member_lookups = 0
        self._cache_hits = 0
        self._cache_misses = 0

    async def list_squads(
        self,
        guild_id: str,
        include_inactive: bool = False,
        use_cache: bool = True
    ) -> list[Squad]:
        """List available squads in a guild.

        Args:
            guild_id: Discord guild ID
            include_inactive: Whether to include inactive squads
            use_cache: Whether to use cache for this request

        Returns:
            List of squad objects ordered by name

        Raises:
            ValidationError: If guild_id is invalid
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")

        cache_key = self._build_cache_key("squads", guild_id, str(include_inactive))

        # Try cache first if enabled
        if use_cache and self.has_cache:
            cached_squads = await self._get_cached(cache_key)
            if cached_squads:
                self._cache_hits += 1
                self._logger.debug(f"Cache hit for squads list {guild_id}")
                return [self._parse_squad_data(squad) for squad in cached_squads]
            self._cache_misses += 1

        try:
            self._squad_list_requests += 1

            self._log_operation(
                "list_squads",
                guild_id=guild_id,
                include_inactive=include_inactive
            )

            # Fetch from API
            params = {}
            if include_inactive:
                params["include_inactive"] = "true"

            response = await self._api_client.get(
                f"/guilds/{guild_id}/squads",
                params=params,
                timeout=10.0
            )

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            squads_data = response.json()
            squads = [self._parse_squad_data(squad) for squad in squads_data]

            # Cache the result if caching is enabled
            if use_cache and self.has_cache:
                # Convert to serializable format
                cache_data = []
                for squad in squads:
                    squad_dict = squad.__dict__.copy()
                    # Convert datetime to ISO string for serialization
                    if squad_dict.get("created_at"):
                        squad_dict["created_at"] = squad_dict["created_at"].isoformat()
                    cache_data.append(squad_dict)

                await self._set_cached(
                    cache_key,
                    cache_data,
                    ttl=self.CACHE_TTL_SQUADS
                )

            return squads

        except (ValidationError, APIError):
            raise
        except Exception as e:
            self._log_error("list_squads", e, guild_id=guild_id)
            raise ServiceError(f"Failed to list squads: {e}") from e

    async def get_squad(
        self,
        guild_id: str,
        squad_id: UUID,
        use_cache: bool = True
    ) -> Squad:
        """Get detailed information about a specific squad.

        Args:
            guild_id: Discord guild ID
            squad_id: Squad UUID
            use_cache: Whether to use cache for this request

        Returns:
            Squad object with detailed information

        Raises:
            ValidationError: If inputs are invalid
            ResourceNotFoundError: If squad not found
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not squad_id:
            raise ValidationError("squad_id", "Squad ID is required")

        cache_key = self._build_cache_key("squad", guild_id, str(squad_id))

        # Try cache first if enabled
        if use_cache and self.has_cache:
            cached_squad = await self._get_cached(cache_key)
            if cached_squad:
                self._cache_hits += 1
                return self._parse_squad_data(cached_squad)
            self._cache_misses += 1

        try:
            self._log_operation("get_squad", guild_id=guild_id, squad_id=str(squad_id))

            response = await self._api_client.get(
                f"/guilds/{guild_id}/squads/{squad_id}",
                timeout=10.0
            )

            if response.status_code == 404:
                raise ResourceNotFoundError("squad", str(squad_id))

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            squad_data = response.json()
            squad = self._parse_squad_data(squad_data)

            # Cache the result
            if use_cache and self.has_cache:
                squad_dict = squad.__dict__.copy()
                if squad_dict.get("created_at"):
                    squad_dict["created_at"] = squad_dict["created_at"].isoformat()

                await self._set_cached(
                    cache_key,
                    squad_dict,
                    ttl=self.CACHE_TTL_SQUADS
                )

            return squad

        except APIError as e:
            # Convert 404 APIError to ResourceNotFoundError
            if e.status_code == 404:
                raise ResourceNotFoundError("squad", str(squad_id)) from e
            # Re-raise other API errors
            raise
        except (ValidationError, ResourceNotFoundError):
            raise
        except Exception as e:
            self._log_error("get_squad", e, guild_id=guild_id, squad_id=str(squad_id))
            raise ServiceError(f"Failed to get squad: {e}") from e

    async def get_user_squad(
        self,
        guild_id: str,
        user_id: str,
        use_cache: bool = True
    ) -> UserSquadResponse:
        """Get user's current squad membership information.

        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            use_cache: Whether to use cache for this request

        Returns:
            UserSquadResponse with squad info or None if not in any squad

        Raises:
            ValidationError: If inputs are invalid
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not user_id or not user_id.strip():
            raise ValidationError("user_id", "User ID is required")

        cache_key = self._build_cache_key("user_squad", guild_id, user_id)

        # Try cache first if enabled
        if use_cache and self.has_cache:
            cached_response = await self._get_cached(cache_key)
            if cached_response:
                self._cache_hits += 1
                # Parse cached squad data back to Squad object if present
                if cached_response.get("squad"):
                    cached_response["squad"] = self._parse_squad_data(cached_response["squad"])
                # Parse member_since if present
                if cached_response.get("member_since"):
                    from datetime import datetime
                    cached_response["member_since"] = datetime.fromisoformat(cached_response["member_since"])
                return UserSquadResponse(**cached_response)
            self._cache_misses += 1

        try:
            self._member_lookups += 1

            self._log_operation("get_user_squad", guild_id=guild_id, user_id=user_id)

            response = await self._api_client.get(
                f"/guilds/{guild_id}/squads/members/{user_id}",
                timeout=10.0
            )

            # User not in any squad
            if response.status_code == 404:
                result = UserSquadResponse(user_id=user_id, squad=None)
            elif response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)
            else:
                squad_data = response.json()
                squad = self._parse_squad_data(squad_data["squad"]) if squad_data.get("squad") else None
                member_since = None

                if squad_data.get("member_since"):
                    from datetime import datetime
                    member_since = datetime.fromisoformat(
                        squad_data["member_since"].replace("Z", "+00:00")
                    )

                result = UserSquadResponse(
                    user_id=user_id,
                    squad=squad,
                    member_since=member_since
                )

            # Cache the result
            if use_cache and self.has_cache:
                result_dict = result.__dict__.copy()
                if result_dict.get("squad"):
                    squad_dict = result_dict["squad"].__dict__.copy()
                    if squad_dict.get("created_at"):
                        squad_dict["created_at"] = squad_dict["created_at"].isoformat()
                    result_dict["squad"] = squad_dict
                if result_dict.get("member_since"):
                    result_dict["member_since"] = result_dict["member_since"].isoformat()

                await self._set_cached(
                    cache_key,
                    result_dict,
                    ttl=self.CACHE_TTL_USER_SQUAD
                )

            return result

        except APIError as e:
            # Handle 404 as "not in any squad" case
            if e.status_code == 404:
                result = UserSquadResponse(user_id=user_id, squad=None)

                # Cache the result
                if use_cache and self.has_cache:
                    result_dict = result.__dict__.copy()
                    # Convert member_since to string for caching
                    if result_dict.get("member_since"):
                        result_dict["member_since"] = result_dict["member_since"].isoformat()
                    await self._set_cached(
                        cache_key,
                        result_dict,
                        ttl=self.CACHE_TTL_USER_SQUAD
                    )

                return result
            # Re-raise other API errors
            raise
        except ValidationError:
            raise
        except Exception as e:
            self._log_error("get_user_squad", e, guild_id=guild_id, user_id=user_id)
            raise ServiceError(f"Failed to get user squad: {e}") from e

    async def _check_active_campaign(self, guild_id: str) -> bool:
        """Check if there's a running campaign preventing squad switches.

        A campaign is considered running from its start time until the end of the
        last challenge (which is cadence hours after the last challenge starts).
        The is_active field is a manual override - if False, campaign is ignored.

        Args:
            guild_id: Discord guild ID

        Returns:
            True if there's a running campaign, False otherwise
        """
        try:
            # Get current campaign status from scoreboard endpoint
            response = await self._api_client.get(
                "/challenges/scoreboard",
                params={"guild_id": guild_id},
                timeout=10.0
            )

            if response.status_code != 200:
                logger.warning(f"Failed to check campaigns: {response.status_code}")
                return False  # Allow switch if we can't verify

            data = response.json()
            campaign = data.get("campaign")

            # No campaign or manually disabled
            if not campaign or not campaign.get("is_active", False):
                return False

            # Parse campaign timing data
            start_time_str = campaign.get("start_time")
            num_challenges = campaign.get("num_challenges", 0)
            cadence_hours = campaign.get("release_cadence_hours", 24)

            if not start_time_str or num_challenges <= 0:
                return False

            try:
                # Parse the start time
                start_time = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))

                # Calculate when the campaign truly ends:
                # Start time + (num_challenges * cadence_hours)
                # This accounts for the last challenge running for cadence hours
                campaign_duration_hours = num_challenges * cadence_hours
                campaign_end = start_time + timedelta(hours=campaign_duration_hours)

                # Check if we're currently within the campaign period
                now = datetime.now(UTC)
                if start_time <= now < campaign_end:
                    logger.info(f"Running campaign found for guild {guild_id}: {campaign.get('name')} "
                              f"(ends at {campaign_end.isoformat()})")
                    return True
                else:
                    logger.info(f"Campaign {campaign.get('name')} exists but is not currently running "
                              f"(period: {start_time.isoformat()} to {campaign_end.isoformat()})")
                    return False

            except (ValueError, TypeError) as e:
                logger.error(f"Error parsing campaign dates: {e}")
                return False

        except Exception as e:
            logger.error(f"Error checking for running campaigns: {e}")
            return False  # Allow switch if check fails

    async def join_squad(
        self,
        guild_id: str,
        user_id: str,
        squad_id: UUID,
        current_balance: int,
        username: str | None = None
    ) -> JoinSquadResult:
        """Join a squad with comprehensive validation.

        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            squad_id: Target squad UUID
            current_balance: User's current bytes balance

        Returns:
            JoinSquadResult with operation outcome

        Raises:
            ValidationError: If inputs are invalid
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not user_id or not user_id.strip():
            raise ValidationError("user_id", "User ID is required")
        if not squad_id:
            raise ValidationError("squad_id", "Squad ID is required")
        if current_balance < 0:
            raise ValidationError("current_balance", "Balance cannot be negative")

        try:
            self._join_attempts += 1

            self._log_operation(
                "join_squad",
                guild_id=guild_id,
                user_id=user_id,
                squad_id=str(squad_id),
                current_balance=current_balance
            )

            # Get user's current squad status
            user_squad_response = await self.get_user_squad(guild_id, user_id, use_cache=False)
            current_squad = user_squad_response.squad

            # Check for active campaigns that prevent squad joins/switches
            # Exception: Allow joining from default squad to competitive squads
            has_active_campaign = await self._check_active_campaign(guild_id)
            if has_active_campaign:
                # Allow moving from default squad to competitive squads
                if current_squad and not getattr(current_squad, "is_default", False):
                    # Switching between competitive squads - not allowed
                    reason = "Squad switching is disabled during active challenge campaigns to prevent spying on other squads."
                    return JoinSquadResult(
                        success=False,
                        reason=reason
                    )
                elif not current_squad:
                    # New members with no squad - not allowed
                    reason = "Squad joining is disabled during active challenge campaigns. Members must remain unaffiliated until the campaign ends."
                    return JoinSquadResult(
                        success=False,
                        reason=reason
                    )
                # If current_squad.is_default is True, allow them to proceed

            # Get target squad information
            try:
                target_squad = await self.get_squad(guild_id, squad_id, use_cache=True)
            except ResourceNotFoundError:
                return JoinSquadResult(
                    success=False,
                    reason="Squad not found!"
                )

            # Check if squad is active
            if not target_squad.is_active:
                return JoinSquadResult(
                    success=False,
                    reason=f"The {target_squad.name} squad is currently inactive."
                )

            # Prevent joining default squads manually
            if target_squad.is_default:
                return JoinSquadResult(
                    success=False,
                    reason=f"Cannot manually join the {target_squad.name} squad. This is the default squad - members are automatically assigned when they earn bytes."
                )

            # Check if user is already in this squad
            if current_squad and current_squad.id == squad_id:
                return JoinSquadResult(
                    success=False,
                    reason=f"You're already in the {target_squad.name} squad!"
                )

            # Check if squad is full
            if target_squad.is_full:
                return JoinSquadResult(
                    success=False,
                    reason=f"The {target_squad.name} squad is full! (Maximum: {target_squad.max_members} members)"
                )

            # Calculate join cost including any sale discounts
            # Determine if this is a join (first time) or switch operation
            is_switching = current_squad is not None
            join_cost = target_squad.current_switch_cost if is_switching else target_squad.current_join_cost

            # Check if user has sufficient balance
            if join_cost > current_balance:
                action_type = "Switching to" if is_switching else "Joining"
                cost_message = f"{join_cost:,} bytes"

                # Add sale information to cost message if applicable
                if is_switching and target_squad.has_switch_sale:
                    original_cost = target_squad.switch_cost
                    discount = target_squad.switch_discount_percent
                    cost_message = f"~~{original_cost:,}~~ **{join_cost:,}** bytes ({discount}% off sale!)"
                elif not is_switching and target_squad.has_join_sale:
                    original_cost = target_squad.switch_cost
                    discount = target_squad.join_discount_percent
                    cost_message = f"~~{original_cost:,}~~ **{join_cost:,}** bytes ({discount}% off sale!)"

                return JoinSquadResult(
                    success=False,
                    cost=join_cost,
                    reason=f"Insufficient bytes! {action_type} the {target_squad.name} squad costs {cost_message}, but you only have {current_balance:,} bytes."
                )

            # Attempt to join squad via API
            try:
                response = await self._api_client.post(
                    f"/guilds/{guild_id}/squads/{squad_id}/join",
                    json_data={"user_id": user_id, "username": username},
                    timeout=15.0
                )
            except APIError as api_error:
                # Handle the case where user is already in a squad
                if "already in squad" in str(api_error).lower():
                    # User is already in a squad, leave the current squad first
                    if current_squad:
                        try:
                            self._log_operation("leave_squad_for_switch", guild_id=guild_id, user_id=user_id, old_squad=current_squad.name)
                            await self.leave_squad(guild_id, user_id)

                            # Try joining the new squad again after leaving
                            response = await self._api_client.post(
                                f"/guilds/{guild_id}/squads/{squad_id}/join",
                                json_data={"user_id": user_id, "username": username},
                                timeout=15.0
                            )
                        except Exception as e:
                            logger.error(f"Failed to leave current squad {current_squad.name} before joining {target_squad.name}: {e}")
                            return JoinSquadResult(
                                success=False,
                                reason=f"Failed to leave {current_squad.name} before joining {target_squad.name}: {str(e)}"
                            )
                    else:
                        # Shouldn't happen, but handle gracefully
                        return JoinSquadResult(
                            success=False,
                            reason="You're already in a squad, but we couldn't identify it!"
                        )
                else:
                    # Re-raise other API errors
                    raise

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", "Failed to join squad")

                # Handle specific error cases
                if "squad is full" in error_message.lower():
                    return JoinSquadResult(
                        success=False,
                        reason=f"The {target_squad.name} squad is full!"
                    )
                elif "insufficient" in error_message.lower():
                    return JoinSquadResult(
                        success=False,
                        cost=join_cost,
                        reason="Insufficient bytes for squad switch!"
                    )

                return JoinSquadResult(
                    success=False,
                    reason=error_message
                )

            # Parse successful join response
            response.json()

            # Get actual updated balance from the database after join cost deduction
            # Only fetch if there was a cost, otherwise use current balance
            if join_cost > 0:
                try:
                    balance_response = await self._api_client.get(
                        f"/guilds/{guild_id}/bytes/balance/{user_id}",
                        timeout=10.0
                    )
                    if balance_response.status_code == 200:
                        balance_data = balance_response.json()
                        new_balance = balance_data.get("balance", current_balance)
                    else:
                        # Fallback to calculated balance if API call fails
                        new_balance = current_balance - join_cost
                except Exception as e:
                    logger.warning(f"Failed to fetch updated balance after squad join: {e}")
                    # Fallback to calculated balance
                    new_balance = current_balance - join_cost
            else:
                new_balance = current_balance

            # Invalidate related caches
            await self._invalidate_user_squad_cache(guild_id, user_id)
            await self._invalidate_squad_cache(guild_id, squad_id)
            if current_squad:
                await self._invalidate_squad_cache(guild_id, current_squad.id)

            # Also invalidate bytes balance cache since cost was deducted
            if join_cost > 0 and self.has_cache:
                bytes_cache_key = f"balance:{guild_id}:{user_id}"
                try:
                    await self._cache_manager.delete(bytes_cache_key)
                    logger.debug(f"Invalidated bytes balance cache for user {user_id}")
                except Exception as e:
                    logger.warning(f"Failed to invalidate bytes balance cache: {e}")

            return JoinSquadResult(
                success=True,
                squad=target_squad,
                previous_squad=current_squad,
                cost=join_cost,
                new_balance=new_balance
            )

        except (ValidationError, APIError):
            raise
        except Exception as e:
            self._log_error(
                "join_squad", e,
                guild_id=guild_id,
                user_id=user_id,
                squad_id=str(squad_id)
            )
            raise ServiceError(f"Failed to join squad: {e}") from e

    async def leave_squad(
        self,
        guild_id: str,
        user_id: str
    ) -> UserSquadResponse:
        """Leave current squad.

        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID

        Returns:
            UserSquadResponse with updated status (should have no squad)

        Raises:
            ValidationError: If inputs are invalid
            NotInSquadError: If user is not in any squad
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not user_id or not user_id.strip():
            raise ValidationError("user_id", "User ID is required")

        try:
            self._leave_attempts += 1

            self._log_operation("leave_squad", guild_id=guild_id, user_id=user_id)

            # Check if user is in a squad
            user_squad_response = await self.get_user_squad(guild_id, user_id, use_cache=False)

            if not user_squad_response.is_in_squad:
                raise NotInSquadError()

            current_squad = user_squad_response.squad

            # Leave squad via API
            response = await self._api_client.delete(
                f"/guilds/{guild_id}/squads/leave",
                json_data={"user_id": user_id},
                timeout=10.0
            )

            if response.status_code == 404:
                raise NotInSquadError()

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            # Invalidate related caches
            await self._invalidate_user_squad_cache(guild_id, user_id)
            if current_squad:
                await self._invalidate_squad_cache(guild_id, current_squad.id)

            # Return updated status (user should not be in any squad now)
            return UserSquadResponse(user_id=user_id, squad=None)

        except (ValidationError, NotInSquadError, APIError):
            raise
        except Exception as e:
            self._log_error("leave_squad", e, guild_id=guild_id, user_id=user_id)
            raise ServiceError(f"Failed to leave squad: {e}") from e

    async def get_squad_members(
        self,
        guild_id: str,
        squad_id: UUID,
        use_cache: bool = True
    ) -> list[SquadMember]:
        """Get list of squad members.

        Args:
            guild_id: Discord guild ID
            squad_id: Squad UUID
            use_cache: Whether to use cache for this request

        Returns:
            List of squad members ordered by join date

        Raises:
            ValidationError: If inputs are invalid
            ResourceNotFoundError: If squad not found
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not squad_id:
            raise ValidationError("squad_id", "Squad ID is required")

        cache_key = self._build_cache_key("squad_members", guild_id, str(squad_id))

        # Try cache first if enabled
        if use_cache and self.has_cache:
            cached_members = await self._get_cached(cache_key)
            if cached_members:
                self._cache_hits += 1
                # Parse cached member data back to proper objects
                parsed_members = []
                for member_data in cached_members:
                    # Parse joined_at string back to datetime if present
                    if member_data.get("joined_at"):
                        from datetime import datetime
                        member_data = member_data.copy()  # Don't modify original cached data
                        member_data["joined_at"] = datetime.fromisoformat(member_data["joined_at"])
                    parsed_members.append(SquadMember(**member_data))
                return parsed_members
            self._cache_misses += 1

        try:
            self._log_operation("get_squad_members", guild_id=guild_id, squad_id=str(squad_id))

            response = await self._api_client.get(
                f"/guilds/{guild_id}/squads/{squad_id}/members",
                timeout=10.0
            )

            if response.status_code == 404:
                raise ResourceNotFoundError("squad", str(squad_id))

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            members_data = response.json()
            members = []

            for member_data in members_data.get("members", []):
                # Parse joined_at if available
                joined_at = None
                if member_data.get("joined_at"):
                    from datetime import datetime
                    joined_at = datetime.fromisoformat(
                        member_data["joined_at"].replace("Z", "+00:00")
                    )

                member = SquadMember(
                    user_id=member_data["user_id"],
                    username=member_data.get("username"),
                    joined_at=joined_at
                )

                members.append(member)

            # Cache the result
            if use_cache and self.has_cache:
                cache_data = []
                for member in members:
                    member_dict = member.__dict__.copy()
                    if member_dict.get("joined_at"):
                        member_dict["joined_at"] = member_dict["joined_at"].isoformat()
                    cache_data.append(member_dict)

                await self._set_cached(
                    cache_key,
                    cache_data,
                    ttl=self.CACHE_TTL_SQUAD_MEMBERS
                )

            return members

        except APIError as e:
            # Convert 404 APIError to ResourceNotFoundError
            if e.status_code == 404:
                raise ResourceNotFoundError("squad", str(squad_id)) from e
            # Re-raise other API errors
            raise
        except (ValidationError, ResourceNotFoundError):
            raise
        except Exception as e:
            self._log_error("get_squad_members", e, guild_id=guild_id, squad_id=str(squad_id))
            raise ServiceError(f"Failed to get squad members: {e}") from e

    async def get_service_stats(self) -> dict[str, Any]:
        """Get comprehensive service statistics.

        Returns:
            Dictionary containing service performance metrics
        """
        cache_hit_rate = 0.0
        total_cache_ops = self._cache_hits + self._cache_misses
        if total_cache_ops > 0:
            cache_hit_rate = self._cache_hits / total_cache_ops

        return {
            "service_name": self.service_name,
            "total_squad_list_requests": self._squad_list_requests,
            "total_join_attempts": self._join_attempts,
            "total_leave_attempts": self._leave_attempts,
            "total_member_lookups": self._member_lookups,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": cache_hit_rate,
            "cache_enabled": self.has_cache
        }

    # Cache management helper methods

    async def _invalidate_user_squad_cache(self, guild_id: str, user_id: str) -> None:
        """Invalidate user squad cache for specific user."""
        cache_key = self._build_cache_key("user_squad", guild_id, user_id)
        await self._invalidate_cache(cache_key)

    async def _invalidate_squad_cache(self, guild_id: str, squad_id: UUID) -> None:
        """Invalidate squad cache for specific squad."""
        cache_key = self._build_cache_key("squad", guild_id, str(squad_id))
        await self._invalidate_cache(cache_key)

        # Also invalidate squad members cache
        members_cache_key = self._build_cache_key("squad_members", guild_id, str(squad_id))
        await self._invalidate_cache(members_cache_key)

        # Invalidate squad list cache
        list_cache_pattern = self._build_cache_key("squads", guild_id, "*")
        await self._invalidate_cache_pattern(list_cache_pattern)

    def _parse_squad_data(self, squad_data: dict[str, Any]) -> Squad:
        """Parse squad data from API response, handling date conversions.

        Args:
            squad_data: Raw data from API response

        Returns:
            Squad object with properly parsed dates
        """
        from uuid import UUID

        # Make a copy to avoid modifying the original
        parsed_data = squad_data.copy()

        # Parse UUID fields properly
        if parsed_data.get("id"):
            if isinstance(parsed_data["id"], str):
                parsed_data["id"] = UUID(parsed_data["id"])

        # Parse date fields properly - handle both string and datetime inputs
        if parsed_data.get("created_at"):
            if isinstance(parsed_data["created_at"], str):
                parsed_data["created_at"] = datetime.fromisoformat(
                    parsed_data["created_at"].replace("Z", "+00:00")
                )
        if parsed_data.get("updated_at"):
            if isinstance(parsed_data["updated_at"], str):
                parsed_data["updated_at"] = datetime.fromisoformat(
                    parsed_data["updated_at"].replace("Z", "+00:00")
                )
        else:
            # If no updated_at field, set it to None
            parsed_data["updated_at"] = None

        return Squad(**parsed_data)
