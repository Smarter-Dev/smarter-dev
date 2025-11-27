"""Service layer models for the Discord bot.

This module defines immutable dataclasses that represent data transferred
between services and the API. These models provide type safety, validation,
and clear contracts for service operations.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from uuid import UUID

from smarter_dev.bot.services.exceptions import ValidationError


@dataclass(frozen=True)
class BytesBalance:
    """Immutable representation of a user's bytes balance.

    This model represents the complete state of a user's bytes economy
    account, including balance, transaction totals, and streak information.
    """

    guild_id: str
    user_id: str
    balance: int
    total_received: int
    total_sent: int
    streak_count: int
    last_daily: date | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self):
        """Validate balance data after initialization."""
        if self.balance < 0:
            raise ValidationError("balance", "Balance cannot be negative")
        if self.total_received < 0:
            raise ValidationError("total_received", "Total received cannot be negative")
        if self.total_sent < 0:
            raise ValidationError("total_sent", "Total sent cannot be negative")
        if self.streak_count < 0:
            raise ValidationError("streak_count", "Streak count cannot be negative")
        if not self.guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not self.user_id.strip():
            raise ValidationError("user_id", "User ID is required")

    @property
    def net_balance(self) -> int:
        """Calculate net balance (received - sent)."""
        return self.total_received - self.total_sent

    @property
    def has_daily_streak(self) -> bool:
        """Check if user has an active daily streak."""
        return self.streak_count > 0

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        return {
            "Balance": f"{self.balance:,} bytes",
            "Total Received": f"{self.total_received:,} bytes",
            "Total Sent": f"{self.total_sent:,} bytes",
            "Daily Streak": f"{self.streak_count} days" if self.streak_count > 0 else "No streak",
            "Last Daily": self.last_daily.strftime("%Y-%m-%d") if self.last_daily else "Never"
        }


@dataclass(frozen=True)
class DailyClaimResult:
    """Result of a daily bytes claim operation.

    This model encapsulates all information about a daily claim attempt,
    including success status, earned amounts, and streak information.
    """

    success: bool
    balance: BytesBalance | None = None
    earned: int | None = None
    streak: int | None = None
    streak_bonus: int | None = None
    multiplier: float | None = None
    reason: str | None = None
    next_claim_at: datetime | None = None
    squad_assignment: dict[str, any] | None = None

    def __post_init__(self):
        """Validate claim result data."""
        if self.success and self.balance is None:
            raise ValidationError("balance", "Balance is required for successful claims")
        if self.success and self.earned is None:
            raise ValidationError("earned", "Earned amount is required for successful claims")
        if not self.success and self.reason is None:
            raise ValidationError("reason", "Reason is required for failed claims")
        if self.earned is not None and self.earned < 0:
            raise ValidationError("earned", "Earned amount cannot be negative")
        if self.streak is not None and self.streak < 0:
            raise ValidationError("streak", "Streak cannot be negative")

    @property
    def is_streak_bonus(self) -> bool:
        """Check if this claim includes a streak bonus."""
        return self.streak_bonus is not None and self.streak_bonus > 1

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        if not self.success:
            return {"Error": self.reason or "Unknown error"}

        result = {
            "Earned": f"{self.earned:,} bytes",
            "New Balance": f"{self.balance.balance:,} bytes",
            "Streak": f"{self.streak} days"
        }

        if self.is_streak_bonus:
            result["Streak Bonus"] = f"{self.streak_bonus}x multiplier"

        if self.next_claim_at:
            result["Next Claim"] = f"<t:{int(self.next_claim_at.timestamp())}:R>"

        return result


@dataclass(frozen=True)
class BytesTransaction:
    """Immutable representation of a bytes transaction.

    This model represents a completed transfer of bytes between users,
    including all metadata for audit and display purposes.
    """

    id: UUID
    guild_id: str
    giver_id: str
    giver_username: str
    receiver_id: str
    receiver_username: str
    amount: int
    reason: str | None = None
    created_at: datetime | None = None

    def __post_init__(self):
        """Validate transaction data."""
        if self.amount <= 0:
            raise ValidationError("amount", "Transaction amount must be positive")
        if not self.guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not self.giver_id.strip():
            raise ValidationError("giver_id", "Giver ID is required")
        if not self.receiver_id.strip():
            raise ValidationError("receiver_id", "Receiver ID is required")
        if not self.giver_username.strip():
            raise ValidationError("giver_username", "Giver username is required")
        if not self.receiver_username.strip():
            raise ValidationError("receiver_username", "Receiver username is required")
        if self.giver_id == self.receiver_id:
            raise ValidationError("receiver_id", "Cannot transfer to yourself")

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        result = {
            "From": self.giver_username,
            "To": self.receiver_username,
            "Amount": f"{self.amount:,} bytes"
        }

        if self.reason:
            result["Reason"] = self.reason

        if self.created_at:
            result["When"] = f"<t:{int(self.created_at.timestamp())}:R>"

        return result


@dataclass(frozen=True)
class TransferResult:
    """Result of a bytes transfer operation.

    This model encapsulates the outcome of attempting to transfer
    bytes between users, including success status and transaction details.
    """

    success: bool
    transaction: BytesTransaction | None = None
    reason: str | None = None
    new_giver_balance: int | None = None
    new_receiver_balance: int | None = None
    is_cooldown_error: bool = False
    cooldown_end_timestamp: int | None = None

    def __post_init__(self):
        """Validate transfer result data."""
        if self.success and self.transaction is None:
            raise ValidationError("transaction", "Transaction is required for successful transfers")
        if not self.success and self.reason is None:
            raise ValidationError("reason", "Reason is required for failed transfers")

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        if not self.success:
            return {"Error": self.reason or "Unknown error"}

        result = self.transaction.to_embed_dict()

        if self.new_giver_balance is not None:
            result["Your New Balance"] = f"{self.new_giver_balance:,} bytes"

        return result


@dataclass(frozen=True)
class LeaderboardEntry:
    """Single entry in the bytes leaderboard.

    This model represents one user's position and stats
    in the guild's bytes economy leaderboard.
    """

    rank: int
    user_id: str
    username: str | None = None
    balance: int = 0
    total_received: int = 0
    streak_count: int = 0

    def __post_init__(self):
        """Validate leaderboard entry data."""
        if self.rank <= 0:
            raise ValidationError("rank", "Rank must be positive")
        if not self.user_id.strip():
            raise ValidationError("user_id", "User ID is required")
        if self.balance < 0:
            raise ValidationError("balance", "Balance cannot be negative")
        if self.total_received < 0:
            raise ValidationError("total_received", "Total received cannot be negative")
        if self.streak_count < 0:
            raise ValidationError("streak_count", "Streak count cannot be negative")

    def to_embed_dict(self) -> builtins.dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        return {
            "Rank": f"#{self.rank}",
            "User": self.username or f"<@{self.user_id}>",
            "Balance": f"{self.balance:,} bytes",
            "Streak": f"{self.streak_count} days" if self.streak_count > 0 else "No streak"
        }

    def dict(self) -> builtins.dict[str, any]:
        """Convert to dictionary for planning document compatibility."""
        return {
            "rank": self.rank,
            "user_id": self.user_id,
            "username": self.username,
            "balance": self.balance,
            "total_received": self.total_received,
            "streak_count": self.streak_count
        }


@dataclass(frozen=True)
class Squad:
    """Immutable representation of a squad.

    This model represents a squad (team) in the guild,
    including its configuration and current status.
    """

    id: UUID
    guild_id: str
    role_id: str
    name: str
    description: str | None = None
    welcome_message: str | None = None
    announcement_channel: str | None = None
    switch_cost: int = 0
    max_members: int | None = None
    member_count: int = 0
    is_active: bool = True
    is_default: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    # Sale cost information (populated from API with sale discounts applied)
    join_cost_info: dict[str, any] | None = None
    switch_cost_info: dict[str, any] | None = None

    def __post_init__(self):
        """Validate squad data."""
        if not self.guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if not self.role_id.strip():
            raise ValidationError("role_id", "Role ID is required")
        if not self.name.strip():
            raise ValidationError("name", "Squad name is required")
        if self.switch_cost < 0:
            raise ValidationError("switch_cost", "Switch cost cannot be negative")
        if self.max_members is not None and self.max_members <= 0:
            raise ValidationError("max_members", "Max members must be positive")
        if self.member_count < 0:
            raise ValidationError("member_count", "Member count cannot be negative")
        if self.max_members is not None and self.member_count > self.max_members:
            raise ValidationError("member_count", "Member count cannot exceed max members")

    @property
    def is_full(self) -> bool:
        """Check if squad is at capacity."""
        return self.max_members is not None and self.member_count >= self.max_members

    @property
    def has_switch_cost(self) -> bool:
        """Check if squad has a switching cost."""
        return self.switch_cost > 0

    @property
    def current_join_cost(self) -> int:
        """Get the current join cost including any sale discounts."""
        if self.join_cost_info:
            return self.join_cost_info.get("current_cost", self.switch_cost)
        return self.switch_cost

    @property
    def current_switch_cost(self) -> int:
        """Get the current switch cost including any sale discounts."""
        if self.switch_cost_info:
            return self.switch_cost_info.get("current_cost", self.switch_cost)
        return self.switch_cost

    @property
    def has_join_sale(self) -> bool:
        """Check if there's an active sale for joining this squad."""
        return (self.join_cost_info and
                self.join_cost_info.get("is_on_sale", False))

    @property
    def has_switch_sale(self) -> bool:
        """Check if there's an active sale for switching to this squad."""
        return (self.switch_cost_info and
                self.switch_cost_info.get("is_on_sale", False))

    @property
    def join_discount_percent(self) -> int | None:
        """Get the join discount percentage if there's an active sale."""
        if self.join_cost_info:
            return self.join_cost_info.get("discount_percent")
        return None

    @property
    def switch_discount_percent(self) -> int | None:
        """Get the switch discount percentage if there's an active sale."""
        if self.switch_cost_info:
            return self.switch_cost_info.get("discount_percent")
        return None

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        result = {
            "Name": self.name,
            "Members": f"{self.member_count}"
        }

        if self.max_members:
            result["Members"] += f"/{self.max_members}"
            if self.is_full:
                result["Members"] += " (FULL)"

        if self.description:
            result["Description"] = self.description

        if self.has_switch_cost:
            # Show join/switch costs with sale information
            if self.has_join_sale or self.has_switch_sale:
                cost_parts = []

                # Join cost
                join_cost = self.current_join_cost
                if self.has_join_sale:
                    join_discount = self.join_discount_percent
                    cost_parts.append(f"Join: ~~{self.switch_cost:,}~~ **{join_cost:,}** bytes ({join_discount}% off)")
                else:
                    cost_parts.append(f"Join: {join_cost:,} bytes")

                # Switch cost (if different from join)
                switch_cost = self.current_switch_cost
                if self.has_switch_sale:
                    switch_discount = self.switch_discount_percent
                    cost_parts.append(f"Switch: ~~{self.switch_cost:,}~~ **{switch_cost:,}** bytes ({switch_discount}% off)")
                else:
                    cost_parts.append(f"Switch: {switch_cost:,} bytes")

                result["Cost"] = " | ".join(cost_parts)

                # Add sale indicator
                if self.has_join_sale or self.has_switch_sale:
                    result["🔥 ON SALE"] = "Limited time discount active!"
            else:
                result["Cost"] = f"{self.switch_cost:,} bytes"

        result["Status"] = "Active" if self.is_active else "Inactive"

        return result


@dataclass(frozen=True)
class SquadMember:
    """Representation of a squad member.

    This model represents a user's membership in a squad,
    including join date and any member-specific data.
    """

    user_id: str
    username: str | None = None
    joined_at: datetime | None = None

    def __post_init__(self):
        """Validate squad member data."""
        if not self.user_id.strip():
            raise ValidationError("user_id", "User ID is required")

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        result = {
            "User": self.username or f"<@{self.user_id}>"
        }

        if self.joined_at:
            result["Joined"] = f"<t:{int(self.joined_at.timestamp())}:R>"

        return result


@dataclass(frozen=True)
class JoinSquadResult:
    """Result of a squad join operation.

    This model encapsulates the outcome of attempting to join a squad,
    including success status, costs, and squad information.
    """

    success: bool
    squad: Squad | None = None
    previous_squad: Squad | None = None
    cost: int = 0
    reason: str | None = None
    new_balance: int | None = None

    def __post_init__(self):
        """Validate join squad result data."""
        if self.success and self.squad is None:
            raise ValidationError("squad", "Squad is required for successful joins")
        if not self.success and self.reason is None:
            raise ValidationError("reason", "Reason is required for failed joins")
        if self.cost < 0:
            raise ValidationError("cost", "Cost cannot be negative")

    @property
    def had_previous_squad(self) -> bool:
        """Check if user was in a squad before joining."""
        return self.previous_squad is not None

    @property
    def is_free_join(self) -> bool:
        """Check if the join was free (no cost)."""
        return self.cost == 0

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        if not self.success:
            return {"Error": self.reason or "Unknown error"}

        result = {
            "Joined Squad": self.squad.name
        }

        if self.had_previous_squad:
            result["Left Squad"] = self.previous_squad.name

        if not self.is_free_join:
            result["Cost"] = f"{self.cost:,} bytes"
            if self.new_balance is not None:
                result["New Balance"] = f"{self.new_balance:,} bytes"

        return result


@dataclass(frozen=True)
class UserSquadResponse:
    """Response model for user's current squad information.

    This model represents the squad that a user is currently
    a member of, or None if they're not in any squad.
    """

    user_id: str
    squad: Squad | None = None
    member_since: datetime | None = None

    def __post_init__(self):
        """Validate user squad response data."""
        if not self.user_id.strip():
            raise ValidationError("user_id", "User ID is required")

    @property
    def is_in_squad(self) -> bool:
        """Check if user is currently in a squad."""
        return self.squad is not None

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        if not self.is_in_squad:
            return {"Squad": "Not in any squad"}

        result = self.squad.to_embed_dict()

        if self.member_since:
            result["Member Since"] = f"<t:{int(self.member_since.timestamp())}:R>"

        return result


@dataclass(frozen=True)
class ServiceHealth:
    """Health status information for a service.

    This model represents the operational status of a service,
    including connectivity, performance metrics, and error rates.
    """

    service_name: str
    is_healthy: bool
    response_time_ms: float | None = None
    error_rate: float | None = None
    last_check: datetime | None = None
    details: dict[str, any] = field(default_factory=dict)

    def __post_init__(self):
        """Validate service health data."""
        if not self.service_name.strip():
            raise ValidationError("service_name", "Service name is required")
        if self.response_time_ms is not None and self.response_time_ms < 0:
            raise ValidationError("response_time_ms", "Response time cannot be negative")
        if self.error_rate is not None and (self.error_rate < 0 or self.error_rate > 1):
            raise ValidationError("error_rate", "Error rate must be between 0 and 1")


@dataclass(frozen=True)
class BytesConfig:
    """Configuration for bytes economy in a guild.

    This model represents the configurable parameters for the bytes
    economy system, including daily amounts and limits.
    """

    guild_id: str
    daily_amount: int
    starting_balance: int
    max_transfer: int
    transfer_cooldown_hours: int
    streak_bonuses: dict[int, int]
    role_rewards: dict[str, int]
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        """Validate config data."""
        if not self.guild_id or not self.guild_id.strip():
            raise ValidationError("guild_id", "Guild ID is required")
        if self.daily_amount < 0:
            raise ValidationError("daily_amount", "Daily amount cannot be negative")
        if self.max_transfer < 0:
            raise ValidationError("max_transfer", "Max transfer cannot be negative")

    def to_embed_dict(self) -> dict[str, any]:
        """Convert to dictionary suitable for Discord embed fields."""
        result = {
            "Service": self.service_name,
            "Status": "🟢 Healthy" if self.is_healthy else "🔴 Unhealthy"
        }

        if self.response_time_ms is not None:
            result["Response Time"] = f"{self.response_time_ms:.1f}ms"

        if self.error_rate is not None:
            result["Error Rate"] = f"{self.error_rate * 100:.1f}%"

        if self.last_check:
            result["Last Check"] = f"<t:{int(self.last_check.timestamp())}:R>"

        return result
