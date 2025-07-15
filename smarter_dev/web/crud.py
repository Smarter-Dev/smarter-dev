"""Database operations for the Smarter Dev application.

This module provides CRUD operations for all models, following SOLID principles
and ensuring proper separation of concerns. All operations are async and use
SQLAlchemy 2.0 syntax.
"""

from __future__ import annotations

from typing import Optional, List, Dict, Any
from uuid import UUID
from datetime import datetime, timezone, date

from sqlalchemy import select, update, delete, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError, NoResultFound

from smarter_dev.web.models import (
    BytesBalance,
    BytesTransaction,
    BytesConfig,
    Squad,
    SquadMembership,
)


class DatabaseOperationError(Exception):
    """Base exception for database operations."""
    pass


class NotFoundError(DatabaseOperationError):
    """Raised when a requested resource is not found."""
    pass


class ConflictError(DatabaseOperationError):
    """Raised when a database constraint is violated."""
    pass


class BytesOperations:
    """Database operations for the bytes economy system.
    
    This class encapsulates all database operations related to bytes balances,
    transactions, and configurations. Follows the Single Responsibility Principle
    by focusing solely on data access operations.
    """
    
    async def get_balance(
        self, 
        session: AsyncSession, 
        guild_id: str, 
        user_id: str
    ) -> BytesBalance:
        """Get or create user balance for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            
        Returns:
            BytesBalance: User's balance record
            
        Raises:
            DatabaseOperationError: If database operation fails
        """
        try:
            stmt = select(BytesBalance).where(
                BytesBalance.guild_id == guild_id,
                BytesBalance.user_id == user_id
            )
            result = await session.execute(stmt)
            balance = result.scalar_one_or_none()
            
            if balance is None:
                # Get guild config to determine starting balance
                config = await self._get_or_create_config(session, guild_id)
                balance = BytesBalance(
                    guild_id=guild_id,
                    user_id=user_id,
                    balance=config.starting_balance,
                    total_received=config.starting_balance
                )
                session.add(balance)
            
            return balance
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get balance: {e}") from e
    
    async def create_transaction(
        self,
        session: AsyncSession,
        guild_id: str,
        giver_id: str,
        giver_username: str,
        receiver_id: str,
        receiver_username: str,
        amount: int,
        reason: Optional[str] = None
    ) -> BytesTransaction:
        """Create transaction and update balances atomically.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            giver_id: Discord user ID of giver
            giver_username: Username of giver for audit
            receiver_id: Discord user ID of receiver
            receiver_username: Username of receiver for audit
            amount: Amount to transfer (positive integer)
            reason: Optional reason for transaction
            
        Returns:
            BytesTransaction: Created transaction record
            
        Raises:
            DatabaseOperationError: If transaction fails
            ConflictError: If insufficient balance
        """
        try:
            # Get or create balances
            giver_balance = await self.get_balance(session, guild_id, giver_id)
            receiver_balance = await self.get_balance(session, guild_id, receiver_id)
            
            # Check sufficient balance
            if giver_balance.balance < amount:
                raise ConflictError(
                    f"Insufficient balance: {giver_balance.balance} < {amount}"
                )
            
            # Update balances
            giver_balance.balance -= amount
            giver_balance.total_sent += amount
            
            receiver_balance.balance += amount
            receiver_balance.total_received += amount
            
            # Create transaction record
            transaction = BytesTransaction(
                guild_id=guild_id,
                giver_id=giver_id,
                giver_username=giver_username,
                receiver_id=receiver_id,
                receiver_username=receiver_username,
                amount=amount,
                reason=reason
            )
            
            session.add(transaction)
            
            return transaction
            
        except ConflictError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to create transaction: {e}") from e
    
    async def get_leaderboard(
        self,
        session: AsyncSession,
        guild_id: str,
        limit: int = 10
    ) -> List[BytesBalance]:
        """Get top users by balance for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            limit: Maximum number of results
            
        Returns:
            List[BytesBalance]: Top balances ordered by balance descending
            
        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = (
                select(BytesBalance)
                .where(BytesBalance.guild_id == guild_id)
                .order_by(desc(BytesBalance.balance))
                .limit(limit)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get leaderboard: {e}") from e
    
    async def get_transaction_history(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: Optional[str] = None,
        limit: int = 20
    ) -> List[BytesTransaction]:
        """Get transaction history for a guild or user.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Optional user ID to filter by
            limit: Maximum number of results
            
        Returns:
            List[BytesTransaction]: Transactions ordered by creation time descending
            
        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = (
                select(BytesTransaction)
                .where(BytesTransaction.guild_id == guild_id)
                .order_by(desc(BytesTransaction.created_at))
                .limit(limit)
            )
            
            if user_id:
                stmt = stmt.where(
                    (BytesTransaction.giver_id == user_id) |
                    (BytesTransaction.receiver_id == user_id)
                )
            
            result = await session.execute(stmt)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get transaction history: {e}") from e
    
    async def update_daily_reward(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
        daily_amount: int,
        streak_bonus: int = 1,
        new_streak_count: Optional[int] = None,
        claim_date: Optional[date] = None
    ) -> BytesBalance:
        """Update balance with daily reward and streak tracking.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            daily_amount: Base daily reward amount
            streak_bonus: Streak multiplier
            new_streak_count: Optional streak count to set, defaults to incrementing existing
            claim_date: UTC date of claim, defaults to today
            
        Returns:
            BytesBalance: Updated balance record
            
        Raises:
            DatabaseOperationError: If update fails
        """
        try:
            balance = await self.get_balance(session, guild_id, user_id)
            
            # Calculate total reward
            reward_amount = daily_amount * streak_bonus
            
            # Update balance and streak with calculated values
            balance.balance += reward_amount
            balance.total_received += reward_amount
            balance.last_daily = claim_date or date.today()
            balance.streak_count = new_streak_count if new_streak_count is not None else balance.streak_count + 1
            
            return balance
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to update daily reward: {e}") from e
    
    async def reset_streak(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str
    ) -> BytesBalance:
        """Reset user's daily streak to 0.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            
        Returns:
            BytesBalance: Updated balance record
            
        Raises:
            DatabaseOperationError: If update fails
        """
        try:
            balance = await self.get_balance(session, guild_id, user_id)
            balance.streak_count = 0
            return balance
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to reset streak: {e}") from e
    
    async def _get_or_create_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> BytesConfig:
        """Get or create configuration for a guild.
        
        This is a private helper method used internally by other operations.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            
        Returns:
            BytesConfig: Guild configuration
        """
        stmt = select(BytesConfig).where(BytesConfig.guild_id == guild_id)
        result = await session.execute(stmt)
        config = result.scalar_one_or_none()
        
        if config is None:
            config = BytesConfig(guild_id=guild_id)
            session.add(config)
        
        return config


class BytesConfigOperations:
    """Database operations for bytes configuration management.
    
    Handles CRUD operations for guild-specific bytes economy settings.
    Separated from BytesOperations following the Single Responsibility Principle.
    """
    
    async def get_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> BytesConfig:
        """Get configuration for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            
        Returns:
            BytesConfig: Guild configuration
            
        Raises:
            NotFoundError: If configuration doesn't exist
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(BytesConfig).where(BytesConfig.guild_id == guild_id)
            result = await session.execute(stmt)
            config = result.scalar_one_or_none()
            
            if config is None:
                raise NotFoundError(f"Configuration not found for guild {guild_id}")
            
            return config
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get config: {e}") from e
    
    async def create_config(
        self,
        session: AsyncSession,
        guild_id: str,
        **config_data
    ) -> BytesConfig:
        """Create configuration for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            **config_data: Configuration parameters
            
        Returns:
            BytesConfig: Created configuration
            
        Raises:
            ConflictError: If configuration already exists
            DatabaseOperationError: If creation fails
        """
        try:
            config = BytesConfig(guild_id=guild_id, **config_data)
            session.add(config)
            await session.flush()  # This will trigger IntegrityError if duplicate
            return config
            
        except IntegrityError as e:
            raise ConflictError(f"Configuration already exists for guild {guild_id}") from e
        except Exception as e:
            raise DatabaseOperationError(f"Failed to create config: {e}") from e
    
    async def update_config(
        self,
        session: AsyncSession,
        guild_id: str,
        **updates
    ) -> BytesConfig:
        """Update configuration for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            **updates: Fields to update
            
        Returns:
            BytesConfig: Updated configuration
            
        Raises:
            NotFoundError: If configuration doesn't exist
            DatabaseOperationError: If update fails
        """
        try:
            config = await self.get_config(session, guild_id)
            
            for field, value in updates.items():
                if hasattr(config, field):
                    setattr(config, field, value)
            
            return config
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to update config: {e}") from e
    
    async def delete_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> None:
        """Delete configuration for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            
        Raises:
            NotFoundError: If configuration doesn't exist
            DatabaseOperationError: If deletion fails
        """
        try:
            stmt = delete(BytesConfig).where(BytesConfig.guild_id == guild_id)
            result = await session.execute(stmt)
            
            if result.rowcount == 0:
                raise NotFoundError(f"Configuration not found for guild {guild_id}")
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to delete config: {e}") from e


class SquadOperations:
    """Database operations for squad management system.
    
    Handles all squad-related database operations including squad creation,
    membership management, and queries. Follows SOLID principles for clean
    separation of concerns.
    """
    
    async def get_squad(
        self,
        session: AsyncSession,
        squad_id: UUID
    ) -> Squad:
        """Get squad by ID.
        
        Args:
            session: Database session
            squad_id: Squad UUID
            
        Returns:
            Squad: Squad record
            
        Raises:
            NotFoundError: If squad doesn't exist
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(Squad).where(Squad.id == squad_id)
            result = await session.execute(stmt)
            squad = result.scalar_one_or_none()
            
            if squad is None:
                raise NotFoundError(f"Squad not found: {squad_id}")
            
            return squad
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get squad: {e}") from e
    
    async def get_guild_squads(
        self,
        session: AsyncSession,
        guild_id: str,
        active_only: bool = True
    ) -> List[Squad]:
        """Get all squads for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            active_only: Whether to include only active squads
            
        Returns:
            List[Squad]: Guild squads
            
        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(Squad).where(Squad.guild_id == guild_id)
            
            if active_only:
                stmt = stmt.where(Squad.is_active == True)
            
            stmt = stmt.order_by(Squad.name)
            result = await session.execute(stmt)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get guild squads: {e}") from e
    
    async def create_squad(
        self,
        session: AsyncSession,
        guild_id: str,
        role_id: str,
        name: str,
        **squad_data
    ) -> Squad:
        """Create a new squad.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            role_id: Discord role snowflake ID
            name: Squad name
            **squad_data: Additional squad parameters
            
        Returns:
            Squad: Created squad
            
        Raises:
            ConflictError: If role is already associated with a squad
            DatabaseOperationError: If creation fails
        """
        try:
            squad = Squad(
                guild_id=guild_id,
                role_id=role_id,
                name=name,
                **squad_data
            )
            session.add(squad)
            return squad
            
        except IntegrityError as e:
            raise ConflictError(f"Role {role_id} already associated with a squad") from e
        except Exception as e:
            raise DatabaseOperationError(f"Failed to create squad: {e}") from e
    
    async def join_squad(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
        squad_id: UUID
    ) -> SquadMembership:
        """Join a user to a squad with bytes cost deduction.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            squad_id: Squad UUID
            
        Returns:
            SquadMembership: Created membership record
            
        Raises:
            NotFoundError: If squad doesn't exist
            ConflictError: If user already in squad, squad full, or insufficient balance
            DatabaseOperationError: If operation fails
        """
        try:
            # Get squad and validate
            squad = await self.get_squad(session, squad_id)
            if not squad.is_active:
                raise ConflictError(f"Squad {squad.name} is not active")
            
            # Check if user already in any squad in this guild
            current_membership = await self.get_user_squad(session, guild_id, user_id)
            if current_membership:
                raise ConflictError(f"User already in squad {current_membership.name}")
            
            # Check squad capacity
            if squad.max_members:
                member_count = await self._get_squad_member_count(session, squad_id)
                if member_count >= squad.max_members:
                    raise ConflictError(f"Squad {squad.name} is full")
            
            # Check and deduct switch cost if required
            if squad.switch_cost > 0:
                bytes_ops = BytesOperations()
                balance = await bytes_ops.get_balance(session, guild_id, user_id)
                if balance.balance < squad.switch_cost:
                    raise ConflictError(
                        f"Insufficient balance: {balance.balance} < {squad.switch_cost}"
                    )
                balance.balance -= squad.switch_cost
            
            # Create membership
            membership = SquadMembership(
                squad_id=squad_id,
                user_id=user_id,
                guild_id=guild_id
            )
            session.add(membership)
            
            return membership
            
        except (NotFoundError, ConflictError):
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to join squad: {e}") from e
    
    async def leave_squad(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str
    ) -> None:
        """Remove user from their current squad.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            
        Raises:
            NotFoundError: If user not in any squad
            DatabaseOperationError: If operation fails
        """
        try:
            stmt = delete(SquadMembership).where(
                SquadMembership.guild_id == guild_id,
                SquadMembership.user_id == user_id
            )
            result = await session.execute(stmt)
            
            if result.rowcount == 0:
                raise NotFoundError(f"User {user_id} not in any squad")
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to leave squad: {e}") from e
    
    async def get_user_squad(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str
    ) -> Optional[Squad]:
        """Get user's current squad in a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            
        Returns:
            Optional[Squad]: User's current squad or None
            
        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = (
                select(Squad)
                .join(SquadMembership)
                .where(
                    SquadMembership.guild_id == guild_id,
                    SquadMembership.user_id == user_id
                )
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get user squad: {e}") from e
    
    async def get_squad_members(
        self,
        session: AsyncSession,
        squad_id: UUID
    ) -> List[SquadMembership]:
        """Get all members of a squad.
        
        Args:
            session: Database session
            squad_id: Squad UUID
            
        Returns:
            List[SquadMembership]: Squad memberships
            
        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = (
                select(SquadMembership)
                .where(SquadMembership.squad_id == squad_id)
                .order_by(SquadMembership.joined_at)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get squad members: {e}") from e
    
    async def _get_squad_member_count(
        self,
        session: AsyncSession,
        squad_id: UUID
    ) -> int:
        """Get count of squad members.
        
        Args:
            session: Database session
            squad_id: Squad UUID
            
        Returns:
            int: Number of members
        """
        stmt = (
            select(func.count(SquadMembership.user_id))
            .where(SquadMembership.squad_id == squad_id)
        )
        result = await session.execute(stmt)
        return result.scalar() or 0