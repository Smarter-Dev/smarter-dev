"""Bytes economy service for Discord bot.

This module implements the complete business logic for the bytes economy system,
including balance management, daily claims, transfers, and leaderboards.
All operations are fully testable and Discord-agnostic.
"""

from __future__ import annotations

import logging
from datetime import date
from datetime import datetime
from typing import Any

from smarter_dev.bot.services.base import APIClientProtocol
from smarter_dev.bot.services.base import BaseService
from smarter_dev.bot.services.base import CacheManagerProtocol
from smarter_dev.bot.services.base import UserProtocol
from smarter_dev.bot.services.exceptions import AlreadyClaimedError
from smarter_dev.bot.services.exceptions import APIError
from smarter_dev.bot.services.exceptions import InsufficientBalanceError
from smarter_dev.bot.services.exceptions import ResourceNotFoundError
from smarter_dev.bot.services.exceptions import ServiceError
from smarter_dev.bot.services.exceptions import ValidationError
from smarter_dev.bot.services.models import BytesBalance
from smarter_dev.bot.services.models import BytesConfig
from smarter_dev.bot.services.models import BytesTransaction
from smarter_dev.bot.services.models import DailyClaimResult
from smarter_dev.bot.services.models import LeaderboardEntry
from smarter_dev.bot.services.models import TransferResult
from smarter_dev.bot.services.streak_service import StreakService

logger = logging.getLogger(__name__)


class BytesService(BaseService):
    """Production-grade bytes economy service.

    This service handles all bytes economy operations including:
    - Balance retrieval and management
    - Daily claim processing with streak calculations
    - Peer-to-peer transfers with validation
    - Leaderboard generation
    - Transaction history
    - Admin operations (streak resets)

    Features:
    - Intelligent caching with configurable TTLs
    - Comprehensive error handling and validation
    - Integration with existing StreakService
    - Performance optimizations for high-load scenarios
    - Full observability and monitoring
    """

    # Streak multiplier configuration (as specified in planning document)
    STREAK_MULTIPLIERS = {7: 2, 14: 4, 30: 10, 60: 20}

    # Cache TTL configurations (in seconds)
    CACHE_TTL_BALANCE = 300  # 5 minutes (only used when explicitly requested)
    CACHE_TTL_LEADERBOARD = 60  # 1 minute
    CACHE_TTL_CONFIG = 600  # 10 minutes
    CACHE_TTL_TRANSACTION_HISTORY = 120  # 2 minutes

    def __init__(
        self,
        api_client: APIClientProtocol,
        cache_manager: CacheManagerProtocol | None = None,
        streak_service: StreakService | None = None
    ):
        """Initialize bytes service.

        Args:
            api_client: API client for backend communication
            cache_manager: Cache manager for performance optimization
            streak_service: Streak service for daily claim calculations
        """
        super().__init__(api_client, cache_manager, "BytesService")

        # Initialize streak service with dependency injection
        self._streak_service = streak_service or StreakService()

        # Performance tracking
        self._balance_requests = 0
        self._daily_claims = 0
        self._transfers = 0
        self._cache_hits = 0
        self._cache_misses = 0

    def _validate_discord_id(self, field_name: str, value: str) -> None:
        """Validate Discord ID for security and format requirements.

        Args:
            field_name: Name of the field being validated
            value: The ID value to validate

        Raises:
            ValidationError: If the ID is invalid
        """
        if not value or not value.strip():
            raise ValidationError(field_name, f"{field_name} is required")

        # Check for basic malicious patterns
        malicious_patterns = [
            "';", "'--", "DROP", "SELECT", "INSERT", "UPDATE", "DELETE",
            "<script", "javascript:", "${", "../", "../../",
            "\\x00", "\\x01", "\\x02"
        ]

        value_upper = value.upper()
        for pattern in malicious_patterns:
            if pattern.upper() in value_upper:
                raise ValidationError(field_name, f"Invalid {field_name} format")

        # Discord IDs should be numeric strings
        if not value.isdigit():
            raise ValidationError(field_name, f"Invalid {field_name} format")

        # Discord IDs should be reasonable length (snowflakes are ~18 digits, but allow for edge cases)
        if len(value) < 10 or len(value) > 100:
            raise ValidationError(field_name, f"Invalid {field_name} format")

    def _sanitize_error_message(self, error: Exception) -> str:
        """Sanitize error messages to prevent information disclosure.

        Args:
            error: The original exception

        Returns:
            Sanitized error message safe for user consumption
        """
        error_str = str(error).lower()

        # Check for sensitive patterns and return generic message
        sensitive_patterns = [
            "password", "token", "secret", "key", "connection",
            "postgresql://", "mysql://", "mongodb://", "redis://",
            "localhost", "127.0.0.1", "::1", "host:", "port:",
            "user:", "auth", "credential"
        ]

        for pattern in sensitive_patterns:
            if pattern in error_str:
                return "Service temporarily unavailable"

        # Return a cleaned version of the error
        return "Internal service error"

    async def get_balance(
        self,
        guild_id: str,
        user_id: str,
        use_cache: bool = False
    ) -> BytesBalance:
        """Get user's bytes balance with intelligent caching.

        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            use_cache: Whether to use cache for this request

        Returns:
            BytesBalance: Complete balance information

        Raises:
            ValidationError: If IDs are invalid
            ResourceNotFoundError: If guild/user not found
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        self._validate_discord_id("guild_id", guild_id)
        self._validate_discord_id("user_id", user_id)

        cache_key = self._build_cache_key("balance", guild_id, user_id)

        # Try cache first if enabled
        if use_cache and self.has_cache:
            cached_balance = await self._get_cached(cache_key)
            if cached_balance:
                try:
                    self._cache_hits += 1
                    self._logger.debug(f"Cache hit for balance {guild_id}:{user_id}")
                    return self._parse_balance_data(cached_balance)
                except Exception as e:
                    # Cache data is corrupted, log warning and fall back to API
                    self._logger.warning(f"Corrupted cache data for balance {guild_id}:{user_id}: {e}")
                    self._cache_misses += 1
            else:
                self._cache_misses += 1

        try:
            self._balance_requests += 1

            # Fetch from API
            self._log_operation("get_balance", guild_id=guild_id, user_id=user_id)

            response = await self._api_client.get(
                f"/guilds/{guild_id}/bytes/balance/{user_id}",
                timeout=10.0
            )

            if response.status_code == 404:
                raise ResourceNotFoundError("user_balance", f"{guild_id}:{user_id}")

            # Check if response was successful before parsing
            if response.status_code != 200:
                error_data = response.json()
                error_message = error_data.get("detail", "Unknown API error")
                raise APIError(f"API error: {error_message}", status_code=response.status_code)

            balance_data = response.json()
            balance = self._parse_balance_data(balance_data)

            # Cache the result if caching is enabled
            if use_cache and self.has_cache:
                await self._set_cached(
                    cache_key,
                    balance_data,
                    ttl=self.CACHE_TTL_BALANCE
                )

            return balance

        except APIError as e:
            # Convert 404 APIError to ResourceNotFoundError
            if e.status_code == 404:
                raise ResourceNotFoundError("user_balance", f"{guild_id}:{user_id}") from e
            # Re-raise other API errors
            raise
        except ResourceNotFoundError:
            raise
        except Exception as e:
            self._log_error("get_balance", e, guild_id=guild_id, user_id=user_id)
            sanitized_message = self._sanitize_error_message(e)
            raise ServiceError(f"Failed to get balance: {sanitized_message}") from e

    async def claim_daily(
        self,
        guild_id: str,
        user_id: str,
        username: str
    ) -> DailyClaimResult:
        """Claim daily bytes reward with streak calculation.

        Args:
            guild_id: Discord guild ID
            user_id: Discord user ID
            username: User's display name for audit

        Returns:
            DailyClaimResult: Complete claim result with balance and streak info

        Raises:
            ValidationError: If inputs are invalid
            AlreadyClaimedError: If user already claimed today
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not user_id or not user_id.strip():
            raise ValidationError("user_id", "User ID is required")
        if not username or not username.strip():
            raise ValidationError("username", "Username is required")

        try:
            self._daily_claims += 1

            self._log_operation(
                "claim_daily",
                guild_id=guild_id,
                user_id=user_id,
                username=username
            )

            # Make claim request to API
            response = await self._api_client.post(
                f"/guilds/{guild_id}/bytes/daily",
                json_data={
                    "user_id": user_id,
                    "username": username
                },
                timeout=15.0
            )

            # Handle already claimed error
            if response.status_code == 409:
                error_data = response.json()
                raise AlreadyClaimedError(
                    context={"guild_id": guild_id, "user_id": user_id}
                )

            # Handle other errors
            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            # Parse successful response
            claim_data = response.json()

            # Build comprehensive result
            balance = self._parse_balance_data(claim_data["balance"])

            # Calculate next claim time (midnight UTC tomorrow)
            next_claim_at = None
            if "next_claim_at" in claim_data:
                next_claim_at = datetime.fromisoformat(
                    claim_data["next_claim_at"].replace("Z", "+00:00")
                )

            result = DailyClaimResult(
                success=True,
                balance=balance,
                earned=claim_data.get("reward_amount"),
                streak=balance.streak_count,
                streak_bonus=claim_data.get("streak_bonus", 1),
                multiplier=claim_data.get("streak_bonus", 1),
                next_claim_at=next_claim_at,
                squad_assignment=claim_data.get("squad_assignment")
            )

            # Invalidate related caches
            await self._invalidate_balance_cache(guild_id, user_id)
            await self._invalidate_leaderboard_cache(guild_id)

            return result

        except APIError as e:
            # Check if this is actually a 409 "already claimed" error
            if e.status_code == 409 or "already been claimed" in str(e).lower():
                raise AlreadyClaimedError(
                    context={"guild_id": guild_id, "user_id": user_id}
                ) from e
            # Re-raise other API errors
            raise
        except (AlreadyClaimedError, ValidationError):
            raise
        except Exception as e:
            self._log_error("claim_daily", e, guild_id=guild_id, user_id=user_id)
            raise ServiceError(f"Failed to claim daily reward: {e}") from e

    async def transfer_bytes(
        self,
        guild_id: str,
        giver: UserProtocol,
        receiver: UserProtocol,
        amount: int,
        reason: str | None = None
    ) -> TransferResult:
        """Transfer bytes between users using User objects (planning document signature).

        This method provides compatibility with the planning document specification
        by accepting User objects instead of separate ID and username parameters.

        Args:
            guild_id: Discord guild ID
            giver: User object for the giver
            receiver: User object for the receiver
            amount: Amount of bytes to transfer
            reason: Optional reason for the transfer

        Returns:
            TransferResult: Result of the transfer operation
        """
        return await self.transfer_bytes_by_id(
            guild_id=guild_id,
            giver_id=str(giver.id),
            giver_username=str(giver),
            receiver_id=str(receiver.id),
            receiver_username=str(receiver),
            amount=amount,
            reason=reason
        )

    async def transfer_bytes_by_id(
        self,
        guild_id: str,
        giver_id: str,
        giver_username: str,
        receiver_id: str,
        receiver_username: str,
        amount: int,
        reason: str | None = None
    ) -> TransferResult:
        """Transfer bytes between users using IDs and usernames (internal implementation).

        Args:
            guild_id: Discord guild ID
            giver_id: Giver's Discord user ID
            giver_username: Giver's display name
            receiver_id: Receiver's Discord user ID
            receiver_username: Receiver's display name
            amount: Amount to transfer
            reason: Optional transfer reason

        Returns:
            TransferResult: Complete transfer result

        Raises:
            ValidationError: If inputs are invalid
            InsufficientBalanceError: If giver has insufficient balance
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs - convert Snowflake objects to strings
        guild_id_str = str(guild_id) if guild_id else ""
        giver_id_str = str(giver_id) if giver_id else ""
        receiver_id_str = str(receiver_id) if receiver_id else ""

        if not guild_id_str or not guild_id_str.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not giver_id_str or not giver_id_str.strip():
            raise ValidationError("giver_id", "Giver ID is required")
        if not receiver_id_str or not receiver_id_str.strip():
            raise ValidationError("receiver_id", "Receiver ID is required")
        if not giver_username or not giver_username.strip():
            raise ValidationError("giver_username", "Giver username is required")
        if not receiver_username or not receiver_username.strip():
            raise ValidationError("receiver_username", "Receiver username is required")

        # Validate transfer rules
        if giver_id_str == receiver_id_str:
            return TransferResult(
                success=False,
                reason="You can't send bytes to yourself!"
            )

        if amount <= 0:
            return TransferResult(
                success=False,
                reason="Transfer amount must be positive!"
            )

        if amount > 10000:  # Reasonable upper limit
            return TransferResult(
                success=False,
                reason="Transfer amount too large! Maximum is 10,000 bytes."
            )

        try:
            self._transfers += 1

            self._log_operation(
                "transfer_bytes",
                guild_id=guild_id_str,
                giver_id=giver_id_str,
                receiver_id=receiver_id_str,
                amount=amount,
                reason=reason
            )

            # Check giver's balance before attempting transfer
            giver_balance = await self.get_balance(guild_id_str, giver_id_str, use_cache=False)

            if giver_balance.balance < amount:
                raise InsufficientBalanceError(
                    required=amount,
                    available=giver_balance.balance,
                    operation="transfer"
                )

            # Prepare transfer request
            transfer_data = {
                "giver_id": giver_id_str,
                "giver_username": giver_username,
                "receiver_id": receiver_id_str,
                "receiver_username": receiver_username,
                "amount": amount
            }

            if reason:
                transfer_data["reason"] = reason[:200]  # Limit reason length

            # Execute transfer
            response = await self._api_client.post(
                f"/guilds/{guild_id_str}/bytes/transactions",
                json_data=transfer_data,
                timeout=15.0
            )

            if response.status_code >= 400:
                error_data = response.json()

                # Extract error message from nested structure if needed
                if isinstance(error_data.get("detail"), dict):
                    # Error response is nested (ErrorResponse object)
                    error_message = error_data["detail"].get("detail", f"Transfer failed: {response.status_code}")
                    self._logger.info(f"PARSED NESTED ERROR: {error_message}")
                else:
                    # Error response is a simple string
                    error_message = error_data.get("detail", f"Transfer failed: {response.status_code}")
                    self._logger.info(f"PARSED SIMPLE ERROR: {error_message}")

                # DEBUG: Log the full error response and message
                self._logger.debug(f"API Error Response - Status: {response.status_code}")
                self._logger.debug(f"API Error Data: {error_data}")
                self._logger.debug(f"API Error Message: {error_message}")

                # Handle specific error cases
                if "insufficient balance" in error_message.lower():
                    raise InsufficientBalanceError(
                        required=amount,
                        available=giver_balance.balance,
                        operation="transfer"
                    )

                # Check for transfer limit errors
                if "exceeds maximum limit" in error_message.lower():
                    return TransferResult(
                        success=False,
                        reason=error_message
                    )

                # Check for cooldown errors (MAIN PATH)
                if "cooldown active" in error_message.lower():
                    cooldown_msg = error_message
                    cooldown_timestamp = None

                    self._logger.debug(f"Processing main path cooldown error: {error_message}")

                    # Parse enhanced message format: "message|timestamp"
                    if "|" in error_message:
                        # Split message and timestamp
                        msg_part, timestamp_part = error_message.rsplit("|", 1)
                        try:
                            cooldown_timestamp = int(timestamp_part)
                            cooldown_msg = msg_part
                            self._logger.debug(f"Parsed main path cooldown timestamp: {cooldown_timestamp}")
                        except ValueError:
                            # Fall back to original message if timestamp parsing fails
                            self._logger.warning(f"Failed to parse main path cooldown timestamp: {timestamp_part}")
                    else:
                        self._logger.debug("No timestamp found in main path cooldown message")

                    # Extract just the user-friendly message if needed
                    if "Transfer cooldown active." in cooldown_msg:
                        start = cooldown_msg.find("Transfer cooldown active.")
                        end = cooldown_msg.find(".", start + 1)
                        if end != -1:
                            cooldown_msg = cooldown_msg[start:end + 1]

                    self._logger.debug(f"Final main path cooldown result - msg: '{cooldown_msg}', timestamp: {cooldown_timestamp}")

                    return TransferResult(
                        success=False,
                        reason=cooldown_msg,
                        is_cooldown_error=True,
                        cooldown_end_timestamp=cooldown_timestamp
                    )

                return TransferResult(
                    success=False,
                    reason=error_message
                )

            # Parse successful transfer
            transaction_data = response.json()

            # Create a simple transaction object from API response
            # Note: We don't need to recreate the full model, just extract what we need
            class TransactionResult:
                def __init__(self, data):
                    self.id = data["id"]
                    self.amount = data["amount"]
                    self.giver_id = data["giver_id"]
                    self.receiver_id = data["receiver_id"]

            transaction = TransactionResult(transaction_data)

            # Get updated balances
            new_giver_balance = giver_balance.balance - amount
            new_receiver_balance = None

            try:
                receiver_balance = await self.get_balance(guild_id, receiver_id, use_cache=False)
                new_receiver_balance = receiver_balance.balance
            except Exception:
                # Don't fail transfer if we can't get receiver balance
                pass

            # Invalidate related caches
            await self._invalidate_balance_cache(guild_id, giver_id)
            await self._invalidate_balance_cache(guild_id, receiver_id)
            await self._invalidate_leaderboard_cache(guild_id)
            await self._invalidate_transaction_history_cache(guild_id)

            return TransferResult(
                success=True,
                transaction=transaction,
                new_giver_balance=new_giver_balance,
                new_receiver_balance=new_receiver_balance
            )

        except APIError as e:
            # Handle API errors gracefully by returning failed TransferResult
            if e.status_code and e.status_code >= 400:
                error_message = str(e)

                # Handle specific error cases
                if "insufficient balance" in error_message.lower():
                    raise InsufficientBalanceError(
                        required=amount,
                        available=giver_balance.balance,
                        operation="transfer"
                    ) from e

                # Check for cooldown errors
                if "cooldown active" in error_message.lower():
                    # Parse enhanced cooldown message format from API error response
                    cooldown_msg = error_message
                    cooldown_timestamp = None

                    self._logger.debug(f"Processing cooldown error: {error_message}")

                    # The error_message might be a JSON string from the API response
                    # Try to extract the detail field if it's JSON
                    try:
                        # Check if the error message contains JSON-like content
                        if "'" in error_message and "detail" in error_message:
                            # Extract just the detail value using string parsing since it's not valid JSON
                            detail_start = error_message.find("'detail': '")
                            if detail_start != -1:
                                detail_start += len("'detail': '")
                                detail_end = error_message.find("'", detail_start)
                                if detail_end != -1:
                                    cooldown_msg = error_message[detail_start:detail_end]
                    except Exception:
                        # If parsing fails, use the original message
                        pass

                    # Now parse the enhanced message format: "message|timestamp"
                    if "|" in cooldown_msg:
                        # Split message and timestamp
                        msg_part, timestamp_part = cooldown_msg.rsplit("|", 1)
                        try:
                            cooldown_timestamp = int(timestamp_part)
                            cooldown_msg = msg_part
                            self._logger.debug(f"Parsed cooldown timestamp: {cooldown_timestamp}")
                        except ValueError:
                            # Fall back to original message if timestamp parsing fails
                            self._logger.warning(f"Failed to parse cooldown timestamp: {timestamp_part}")
                    else:
                        self._logger.debug("No timestamp found in cooldown message")

                    # Extract just the user-friendly message if needed
                    if "Transfer cooldown active." in cooldown_msg:
                        start = cooldown_msg.find("Transfer cooldown active.")
                        end = cooldown_msg.find(".", start + 1)
                        if end != -1:
                            cooldown_msg = cooldown_msg[start:end + 1]

                    self._logger.debug(f"Final cooldown result - msg: '{cooldown_msg}', timestamp: {cooldown_timestamp}")

                    return TransferResult(
                        success=False,
                        reason=cooldown_msg,
                        is_cooldown_error=True,
                        cooldown_end_timestamp=cooldown_timestamp
                    )

                return TransferResult(
                    success=False,
                    reason=error_message
                )
            # Re-raise unexpected API errors
            raise
        except (ValidationError, InsufficientBalanceError):
            raise
        except Exception as e:
            # Check if this is actually a validation error that wasn't caught as APIError
            error_str = str(e)
            if "cooldown active" in error_str.lower() or "Transfer cooldown active" in error_str:
                # Extract cooldown message and return as failed result
                cooldown_msg = "Transfer is currently on cooldown. Please try again later."
                cooldown_timestamp = None

                self._logger.debug(f"Processing exception cooldown error: {error_str}")

                if "Transfer cooldown active." in error_str:
                    # Find the part with the human-readable message
                    detail_start = error_str.find("'detail': '")
                    if detail_start != -1:
                        detail_start += len("'detail': '")
                        detail_end = error_str.find("'", detail_start)
                        if detail_end != -1:
                            full_detail = error_str[detail_start:detail_end]

                            # Parse enhanced message format
                            if "|" in full_detail:
                                msg_part, timestamp_part = full_detail.rsplit("|", 1)
                                try:
                                    cooldown_timestamp = int(timestamp_part)
                                    cooldown_msg = msg_part
                                    self._logger.debug(f"Parsed exception cooldown timestamp: {cooldown_timestamp}")
                                except ValueError:
                                    cooldown_msg = full_detail
                                    self._logger.warning(f"Failed to parse exception cooldown timestamp: {timestamp_part}")
                            else:
                                cooldown_msg = full_detail

                return TransferResult(
                    success=False,
                    reason=cooldown_msg,
                    is_cooldown_error=True,
                    cooldown_end_timestamp=cooldown_timestamp
                )

            self._log_error(
                "transfer_bytes", e,
                guild_id=guild_id,
                giver_id=giver_id,
                receiver_id=receiver_id,
                amount=amount
            )
            raise ServiceError(f"Failed to transfer bytes: {e}") from e

    async def get_config(
        self,
        guild_id: str,
        use_cache: bool = True
    ) -> BytesConfig:
        """Get guild bytes configuration as specified in planning document.

        Args:
            guild_id: Discord guild ID
            use_cache: Whether to use cache for this request

        Returns:
            BytesConfig: Guild configuration

        Raises:
            ValidationError: If guild_id is invalid
            ResourceNotFoundError: If guild config not found
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")

        cache_key = self._build_cache_key("config", guild_id)

        # Try cache first if enabled
        if use_cache and self.has_cache:
            cached_config = await self._get_cached(cache_key)
            if cached_config:
                self._cache_hits += 1
                return BytesConfig(**cached_config)
            self._cache_misses += 1

        try:
            self._log_operation("get_config", guild_id=guild_id)

            response = await self._api_client.get(
                f"/guilds/{guild_id}/bytes/config",
                timeout=10.0
            )

            if response.status_code == 404:
                raise ResourceNotFoundError("guild_config", guild_id)

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            config_data = response.json()
            config = BytesConfig(**config_data)

            # Cache the result
            if use_cache and self.has_cache:
                config_dict = config.__dict__.copy()
                await self._set_cached(
                    cache_key,
                    config_dict,
                    ttl=self.CACHE_TTL_CONFIG
                )

            return config

        except (ValidationError, ResourceNotFoundError, APIError):
            raise
        except Exception as e:
            self._log_error("get_config", e, guild_id=guild_id)
            raise ServiceError(f"Failed to get config: {e}") from e

    async def get_leaderboard(
        self,
        guild_id: str,
        limit: int = 10,
        use_cache: bool = True
    ) -> list[LeaderboardEntry]:
        """Get guild bytes leaderboard with caching.

        Args:
            guild_id: Discord guild ID
            limit: Maximum number of entries to return
            use_cache: Whether to use cache for this request

        Returns:
            List of leaderboard entries ordered by balance

        Raises:
            ValidationError: If inputs are invalid
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if limit <= 0 or limit > 100:
            raise ValidationError("limit", "Limit must be between 1 and 100")

        cache_key = self._build_cache_key("leaderboard", guild_id, str(limit))

        # Try cache first if enabled
        if use_cache and self.has_cache:
            cached_leaderboard = await self._get_cached(cache_key)
            if cached_leaderboard:
                self._cache_hits += 1
                return [LeaderboardEntry(**entry) for entry in cached_leaderboard]
            self._cache_misses += 1

        try:
            self._log_operation("get_leaderboard", guild_id=guild_id, limit=limit)

            response = await self._api_client.get(
                f"/guilds/{guild_id}/bytes/leaderboard",
                params={"limit": limit},
                timeout=10.0
            )

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            leaderboard_data = response.json()

            # Build leaderboard entries
            entries = []
            for idx, user_data in enumerate(leaderboard_data.get("users", [])):
                entry = LeaderboardEntry(
                    rank=idx + 1,
                    user_id=user_data["user_id"],
                    balance=user_data["balance"],
                    total_received=user_data.get("total_received", 0),
                    streak_count=user_data.get("streak_count", 0)
                )
                entries.append(entry)

            # Cache the result
            if use_cache and self.has_cache:
                # Convert to serializable format
                cache_data = [entry.__dict__ for entry in entries]
                await self._set_cached(
                    cache_key,
                    cache_data,
                    ttl=self.CACHE_TTL_LEADERBOARD
                )

            return entries

        except (ValidationError, APIError):
            raise
        except Exception as e:
            self._log_error("get_leaderboard", e, guild_id=guild_id, limit=limit)
            raise ServiceError(f"Failed to get leaderboard: {e}") from e

    async def get_transaction_history(
        self,
        guild_id: str,
        user_id: str | None = None,
        limit: int = 20,
        use_cache: bool = True
    ) -> list[BytesTransaction]:
        """Get transaction history for guild or specific user.

        Args:
            guild_id: Discord guild ID
            user_id: Optional user ID to filter by
            limit: Maximum number of transactions to return
            use_cache: Whether to use cache for this request

        Returns:
            List of transactions ordered by creation time (newest first)

        Raises:
            ValidationError: If inputs are invalid
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if limit <= 0 or limit > 100:
            raise ValidationError("limit", "Limit must be between 1 and 100")

        cache_key = self._build_cache_key("transactions", guild_id, user_id or "all", str(limit))

        # Try cache first if enabled
        if use_cache and self.has_cache:
            cached_transactions = await self._get_cached(cache_key)
            if cached_transactions:
                self._cache_hits += 1
                return [BytesTransaction(**tx) for tx in cached_transactions]
            self._cache_misses += 1

        try:
            self._log_operation(
                "get_transaction_history",
                guild_id=guild_id,
                user_id=user_id,
                limit=limit
            )

            params = {"limit": limit}
            if user_id:
                params["user_id"] = user_id

            response = await self._api_client.get(
                f"/guilds/{guild_id}/bytes/transactions",
                params=params,
                timeout=10.0
            )

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            transaction_data = response.json()
            transactions = [
                BytesTransaction(**tx)
                for tx in transaction_data.get("transactions", [])
            ]

            # Cache the result
            if use_cache and self.has_cache:
                # Convert to serializable format
                cache_data = []
                for tx in transactions:
                    tx_dict = tx.__dict__.copy()
                    # Convert datetime to ISO string for serialization
                    if tx_dict.get("created_at") and hasattr(tx_dict["created_at"], "isoformat"):
                        tx_dict["created_at"] = tx_dict["created_at"].isoformat()
                    cache_data.append(tx_dict)

                await self._set_cached(
                    cache_key,
                    cache_data,
                    ttl=self.CACHE_TTL_TRANSACTION_HISTORY
                )

            return transactions

        except (ValidationError, APIError):
            raise
        except Exception as e:
            self._log_error(
                "get_transaction_history", e,
                guild_id=guild_id,
                user_id=user_id,
                limit=limit
            )
            raise ServiceError(f"Failed to get transaction history: {e}") from e

    async def reset_streak(
        self,
        guild_id: str,
        user_id: str,
        admin_id: str
    ) -> BytesBalance:
        """Reset user's daily claim streak (admin operation).

        Args:
            guild_id: Discord guild ID
            user_id: Target user's Discord ID
            admin_id: Admin user's Discord ID

        Returns:
            Updated balance with reset streak

        Raises:
            ValidationError: If inputs are invalid
            ResourceNotFoundError: If user not found
            ServiceError: On service failures
        """
        self._ensure_initialized()

        # Validate inputs
        if not guild_id or not guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not user_id or not user_id.strip():
            raise ValidationError("user_id", "User ID is required")
        if not admin_id or not admin_id.strip():
            raise ValidationError("admin_id", "Admin ID is required")

        try:
            self._log_operation(
                "reset_streak",
                guild_id=guild_id,
                user_id=user_id,
                admin_id=admin_id
            )

            response = await self._api_client.post(
                f"/guilds/{guild_id}/bytes/reset-streak/{user_id}",
                timeout=10.0
            )

            if response.status_code == 404:
                raise ResourceNotFoundError("user_balance", f"{guild_id}:{user_id}")

            if response.status_code >= 400:
                error_data = response.json()
                error_message = error_data.get("detail", f"API error: {response.status_code}")
                raise APIError(error_message, status_code=response.status_code)

            balance_data = response.json()
            balance = self._parse_balance_data(balance_data)

            # Invalidate balance cache
            await self._invalidate_balance_cache(guild_id, user_id)

            return balance

        except (ValidationError, ResourceNotFoundError, APIError):
            raise
        except Exception as e:
            self._log_error(
                "reset_streak", e,
                guild_id=guild_id,
                user_id=user_id,
                admin_id=admin_id
            )
            raise ServiceError(f"Failed to reset streak: {e}") from e

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
            "total_balance_requests": self._balance_requests,
            "total_daily_claims": self._daily_claims,
            "total_transfers": self._transfers,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "cache_hit_rate": cache_hit_rate,
            "cache_enabled": self.has_cache
        }

    # Cache management helper methods

    async def _invalidate_balance_cache(self, guild_id: str, user_id: str) -> None:
        """Invalidate balance cache for specific user."""
        cache_key = self._build_cache_key("balance", guild_id, user_id)
        await self._invalidate_cache(cache_key)

    async def _invalidate_leaderboard_cache(self, guild_id: str) -> None:
        """Invalidate all leaderboard cache entries for guild."""
        pattern = self._build_cache_key("leaderboard", guild_id, "*")
        await self._invalidate_cache_pattern(pattern)

    async def _invalidate_transaction_history_cache(self, guild_id: str) -> None:
        """Invalidate transaction history cache for guild."""
        pattern = self._build_cache_key("transactions", guild_id, "*")
        await self._invalidate_cache_pattern(pattern)

    def _calculate_multiplier(self, streak: int) -> int:
        """Calculate streak multiplier as specified in planning document.

        Args:
            streak: Current streak count

        Returns:
            Multiplier value based on streak thresholds
        """
        for threshold, multiplier in sorted(
            self.STREAK_MULTIPLIERS.items(),
            reverse=True
        ):
            if streak >= threshold:
                return multiplier
        return 1

    def _parse_balance_data(self, balance_data: dict[str, Any]) -> BytesBalance:
        """Parse balance data from API response, handling date conversions.

        Args:
            balance_data: Raw data from API response

        Returns:
            BytesBalance object with properly parsed dates
        """
        # Make a copy to avoid modifying the original
        parsed_data = balance_data.copy()

        # Parse date fields properly
        if parsed_data.get("last_daily"):
            parsed_data["last_daily"] = date.fromisoformat(parsed_data["last_daily"])

        # Handle created_at - parse if string, set None if None
        if parsed_data.get("created_at"):
            if isinstance(parsed_data["created_at"], str):
                parsed_data["created_at"] = datetime.fromisoformat(
                    parsed_data["created_at"].replace("Z", "+00:00")
                )
        else:
            parsed_data["created_at"] = None

        # Handle updated_at - parse if string, set None if None
        if parsed_data.get("updated_at"):
            if isinstance(parsed_data["updated_at"], str):
                parsed_data["updated_at"] = datetime.fromisoformat(
                    parsed_data["updated_at"].replace("Z", "+00:00")
                )
        else:
            parsed_data["updated_at"] = None

        return BytesBalance(**parsed_data)
