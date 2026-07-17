"""Native Litestar port of the bytes-economy bot API (legacy ``routers/bytes.py``).

Preserves the exact paths, verbs, status codes, and request/response schemas
of the FastAPI implementation so ``smarter_dev/bot/services/bytes_service.py``
needs zero changes. See docs/v2/legacy-sunset/04-api-rewrite.md (unit U2).

NOT registered in ``app.yaml`` yet — the FastAPI mount still owns ``/api``.
This module exists for isolated parity tests until the atomic switchover.

Rate-limiting parity is deferred to the switchover commit (plan section
"Rate-limiting parity", option A): the multi-tier limiter keys off the
authenticated key's per-window fields, which the Skrift ``auth_guard`` does not
yet surface. The FastAPI mount still enforces those windows in production until
switchover, so no window is dropped in the interim.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from litestar import Controller, Request, delete, get, post, put
from litestar.di import Provide
from litestar.params import Parameter
from litestar.status_codes import HTTP_200_OK
from sqlalchemy.ext.asyncio import AsyncSession

from skrift.auth.guards import APIKeyOnly, Permission, auth_guard

from smarter_dev.bot.services.streak_service import StreakService
from smarter_dev.shared.date_provider import get_date_provider
from smarter_dev.web.api.schemas import (
    BytesBalanceResponse,
    BytesConfigResponse,
    BytesConfigUpdate,
    BytesTransactionCreate,
    BytesTransactionResponse,
    DailyClaimRequest,
    DailyClaimResponse,
    LeaderboardResponse,
    SquadResponse,
    SuccessResponse,
    TransactionHistoryResponse,
)
from smarter_dev.web.api_native.errors import (
    BOT_API_EXCEPTION_HANDLERS,
    conflict_error,
    plain_error,
    validate_discord_id,
    validation_error,
)
from smarter_dev.web.crud import (
    BytesConfigOperations,
    BytesOperations,
    NotFoundError,
    SquadOperations,
)
from smarter_dev.web.models import BytesBalance

# Permission granted to the bot's Skrift service key (see roles.py `bot-service`
# role and the phase-01 key-mint runbook).
BOT_API_PERMISSION = "bot-api"

# Guards are declared PER ROUTE (not only on the controller) because Skrift's
# ``auth_guard`` inspects ``route_handler.guards`` to find the ``APIKeyOnly``
# marker — controller-level guards do not populate that attribute, so a
# controller-only declaration would silently fall back to session auth. Every
# bytes route therefore reuses this list. See the skrift-auth skill and
# docs/v2/legacy-sunset/04-api-rewrite.md ("Auth model").
BOT_API_GUARDS = [auth_guard, APIKeyOnly(), Permission(BOT_API_PERMISSION)]


async def provide_validated_guild_id(guild_id: str) -> str:
    """Validate the guild-id path param, matching the FastAPI 400 shape."""
    try:
        if int(guild_id) <= 0:
            raise ValueError("Invalid guild ID")
    except ValueError:
        raise plain_error(400, "Invalid guild ID")
    return guild_id


class BytesController(Controller):
    """Bytes economy endpoints — balances, daily claims, transactions, config."""

    path = "/api/guilds/{guild_id:str}/bytes"
    exception_handlers = BOT_API_EXCEPTION_HANDLERS
    dependencies = {"validated_guild_id": Provide(provide_validated_guild_id)}

    @get("/balance/{user_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_balance(
        self,
        request: Request,
        db_session: AsyncSession,
        validated_guild_id: str,
        user_id: str,
    ) -> BytesBalanceResponse:
        """Get a user's bytes balance (creating a zero balance if absent)."""
        validate_discord_id(request, user_id, "user ID")

        balance = await BytesOperations().get_or_create_balance(
            db_session, validated_guild_id, user_id
        )
        return BytesBalanceResponse.model_validate(balance)

    @post("/daily", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def claim_daily(
        self,
        request: Request,
        db_session: AsyncSession,
        validated_guild_id: str,
        data: DailyClaimRequest,
    ) -> DailyClaimResponse:
        """Claim the daily reward with streak-bonus calculation."""
        guild_id = validated_guild_id
        user_id = data.user_id
        username = data.username or f"User {user_id}"

        validate_discord_id(request, user_id, "user ID")

        bytes_ops = BytesOperations()
        config_ops = BytesConfigOperations()
        streak_service = StreakService(date_provider=get_date_provider())

        try:
            config = await config_ops.get_config(db_session, guild_id)
        except NotFoundError:
            config = await config_ops.create_config(db_session, guild_id)

        try:
            balance = await bytes_ops.get_balance(db_session, guild_id, user_id)
            is_new_user = balance.last_daily is None
        except NotFoundError:
            # New user starts at zero; their first "daily" grants the starting balance.
            balance = BytesBalance(
                guild_id=guild_id,
                user_id=user_id,
                balance=0,
                total_received=0,
            )
            db_session.add(balance)
            await db_session.flush()
            is_new_user = True

        streak_result = streak_service.calculate_streak_result(
            last_daily=balance.last_daily,
            current_streak=balance.streak_count,
            daily_amount=config.starting_balance if is_new_user else config.daily_amount,
            streak_bonuses=config.streak_bonuses,
        )

        if not streak_result.can_claim:
            raise conflict_error(
                request,
                "Daily reward has already been claimed today. Try again tomorrow!",
            )

        current_utc_date = get_date_provider().today()
        reward_amount = config.starting_balance if is_new_user else config.daily_amount

        updated_balance, assigned_squad = await bytes_ops.update_daily_reward(
            db_session,
            guild_id,
            user_id,
            username,
            reward_amount,
            streak_result.streak_bonus,
            streak_result.new_streak_count,
            current_utc_date,
            is_new_member=is_new_user,
        )

        next_claim_at = datetime.combine(
            streak_result.next_claim_date, datetime.min.time()
        ).replace(tzinfo=timezone.utc)

        await db_session.refresh(updated_balance)

        # Serialize before commit to avoid session-detachment issues.
        balance_response = BytesBalanceResponse.model_validate(updated_balance)

        squad_response = None
        if assigned_squad:
            squad_ops = SquadOperations()
            full_squad_data = await squad_ops.get_squad(db_session, assigned_squad.id)
            member_count = await squad_ops._get_squad_member_count(
                db_session, assigned_squad.id
            )
            squad_data = full_squad_data.__dict__.copy()
            squad_data["member_count"] = member_count
            squad_response = SquadResponse.model_validate(squad_data)

        await db_session.commit()

        return DailyClaimResponse(
            balance=balance_response,
            reward_amount=streak_result.reward_amount,
            streak_bonus=streak_result.streak_bonus,
            next_claim_at=next_claim_at,
            squad_assignment=squad_response,
        )

    @post("/transactions", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def create_transaction(
        self,
        request: Request,
        db_session: AsyncSession,
        validated_guild_id: str,
        data: BytesTransactionCreate,
    ) -> BytesTransactionResponse:
        """Transfer bytes between two users, honoring limits and cooldowns."""
        guild_id = validated_guild_id
        bytes_ops = BytesOperations()
        config_ops = BytesConfigOperations()

        try:
            config = await config_ops.get_config(db_session, guild_id)
        except NotFoundError:
            config = await config_ops.create_config(db_session, guild_id)

        if data.amount > config.max_transfer:
            raise validation_error(
                request,
                f"Transfer amount ({data.amount}) exceeds maximum limit of "
                f"{config.max_transfer} bytes",
            )

        if data.giver_id == data.receiver_id:
            raise validation_error(request, "Cannot transfer bytes to yourself")

        if config.transfer_cooldown_hours > 0:
            cooldown_cutoff = datetime.now(timezone.utc) - timedelta(
                hours=config.transfer_cooldown_hours
            )
            recent_transfers = await bytes_ops.get_sent_transaction_history(
                db_session, guild_id, sender_user_id=data.giver_id, limit=1
            )
            if recent_transfers and recent_transfers[0].created_at > cooldown_cutoff:
                cooldown_end_time = recent_transfers[0].created_at + timedelta(
                    hours=config.transfer_cooldown_hours
                )
                time_remaining = (
                    cooldown_end_time - datetime.now(timezone.utc)
                ).total_seconds()
                hours_remaining = int(time_remaining // 3600)
                minutes_remaining = int((time_remaining % 3600) // 60)
                cooldown_message = (
                    f"Transfer cooldown active. You can send bytes again in "
                    f"{hours_remaining}h {minutes_remaining}m."
                    f"|{int(cooldown_end_time.timestamp())}"
                )
                raise validation_error(request, cooldown_message)

        created_transaction = await bytes_ops.create_transaction(
            db_session,
            guild_id,
            data.giver_id,
            data.giver_username,
            data.receiver_id,
            data.receiver_username,
            data.amount,
            data.reason,
        )

        transaction_response = BytesTransactionResponse.model_validate(
            created_transaction
        )
        await db_session.commit()
        return transaction_response

    @get("/leaderboard", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_leaderboard(
        self,
        db_session: AsyncSession,
        validated_guild_id: str,
        limit: Annotated[int, Parameter(ge=1, le=100, required=False)] = 10,
    ) -> LeaderboardResponse:
        """Return the top users by balance in descending order."""
        top_balances = await BytesOperations().get_leaderboard(
            db_session, validated_guild_id, limit
        )
        users = [BytesBalanceResponse.model_validate(b) for b in top_balances]
        return LeaderboardResponse(
            guild_id=validated_guild_id,
            users=users,
            total_users=len(users),
            generated_at=datetime.now(timezone.utc),
        )

    @get("/transactions", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_transactions(
        self,
        request: Request,
        db_session: AsyncSession,
        validated_guild_id: str,
        user_id: Annotated[str | None, Parameter(required=False)] = None,
        limit: Annotated[int, Parameter(ge=1, le=100, required=False)] = 20,
    ) -> TransactionHistoryResponse:
        """Return recent transactions, optionally filtered by user."""
        if user_id:
            validate_discord_id(request, user_id, "User ID")

        transactions = await BytesOperations().get_transaction_history(
            db_session, validated_guild_id, user_id, limit
        )
        transaction_responses = [
            BytesTransactionResponse.model_validate(tx) for tx in transactions
        ]
        return TransactionHistoryResponse(
            guild_id=validated_guild_id,
            transactions=transaction_responses,
            total_count=len(transactions),
            user_id=user_id,
        )

    @get("/config", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def get_config(
        self, db_session: AsyncSession, validated_guild_id: str
    ) -> BytesConfigResponse:
        """Return guild bytes config, creating defaults if absent."""
        config_ops = BytesConfigOperations()
        try:
            config = await config_ops.get_config(db_session, validated_guild_id)
        except NotFoundError:
            config = await config_ops.create_config(db_session, validated_guild_id)
            await db_session.commit()
        return BytesConfigResponse.model_validate(config)

    @put("/config", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def update_config(
        self,
        request: Request,
        db_session: AsyncSession,
        validated_guild_id: str,
        data: BytesConfigUpdate,
    ) -> BytesConfigResponse:
        """Update guild bytes config; only provided fields change."""
        update_data = data.model_dump(exclude_unset=True, exclude_none=True)
        if not update_data:
            raise validation_error(
                request,
                "No configuration updates provided. At least one field must be "
                "specified.",
            )

        updated_config = await BytesConfigOperations().update_config(
            db_session, validated_guild_id, **update_data
        )
        config_response = BytesConfigResponse.model_validate(updated_config)
        await db_session.commit()
        return config_response

    @delete("/config", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def delete_config(
        self, db_session: AsyncSession, validated_guild_id: str
    ) -> SuccessResponse:
        """Delete guild bytes config; defaults are recreated on next access."""
        await BytesConfigOperations().delete_config(db_session, validated_guild_id)
        await db_session.commit()
        return SuccessResponse(
            message=f"Bytes configuration deleted for guild {validated_guild_id}",
            timestamp=datetime.now(timezone.utc),
        )

    @post("/reset-streak/{user_id:str}", status_code=HTTP_200_OK, guards=BOT_API_GUARDS)
    async def reset_streak(
        self,
        request: Request,
        db_session: AsyncSession,
        validated_guild_id: str,
        user_id: str,
    ) -> BytesBalanceResponse:
        """Reset a user's daily-claim streak to zero."""
        validate_discord_id(request, user_id, "user ID")

        updated_balance = await BytesOperations().reset_streak(
            db_session, validated_guild_id, user_id
        )
        balance_response = BytesBalanceResponse.model_validate(updated_balance)
        await db_session.commit()
        return balance_response
