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
        """Get user balance for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            
        Returns:
            BytesBalance: User's balance record
            
        Raises:
            NotFoundError: If balance doesn't exist
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
                raise NotFoundError(f"Balance not found for user {user_id} in guild {guild_id}")
            
            return balance
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get balance: {e}") from e
    
    async def get_or_create_balance(
        self, 
        session: AsyncSession, 
        guild_id: str, 
        user_id: str
    ) -> BytesBalance:
        """Get or create user balance for a guild (used for transactions).
        
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
                # Create new user with 0 balance - they'll get starting balance through daily reward system
                balance = BytesBalance(
                    guild_id=guild_id,
                    user_id=user_id,
                    balance=0,
                    total_received=0
                )
                session.add(balance)
                await session.flush()  # Ensure timestamps are populated
            
            return balance
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get or create balance: {e}") from e
    
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
            giver_balance = await self.get_or_create_balance(session, guild_id, giver_id)
            receiver_balance = await self.get_or_create_balance(session, guild_id, receiver_id)
            
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
            await session.flush()  # Ensure timestamps are populated
            
            return transaction
            
        except ConflictError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to create transaction: {e}") from e
    
    async def create_system_charge(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
        username: str,
        amount: int,
        reason: str
    ) -> BytesTransaction:
        """Create system charge transaction (user pays system/squad).
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user ID paying the charge
            username: Username for audit
            amount: Amount to charge (positive integer)
            reason: Reason for the charge (e.g. "Squad join fee")
            
        Returns:
            BytesTransaction: Created transaction record
            
        Raises:
            DatabaseOperationError: If transaction fails
            ConflictError: If insufficient balance
        """
        try:
            # Get user balance
            balance = await self.get_or_create_balance(session, guild_id, user_id)
            
            # Check sufficient balance
            if balance.balance < amount:
                raise ConflictError(
                    f"Insufficient balance: {balance.balance} < {amount}"
                )
            
            # Update balance
            balance.balance -= amount
            balance.total_sent += amount
            
            # Create transaction record with system as receiver
            transaction = BytesTransaction(
                guild_id=guild_id,
                giver_id=user_id,
                giver_username=username,
                receiver_id="SYSTEM",  # Special receiver for system charges
                receiver_username="System",
                amount=amount,
                reason=reason
            )
            
            session.add(transaction)
            await session.flush()  # Ensure timestamps are populated
            
            return transaction
            
        except ConflictError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to create system charge: {e}") from e
    
    async def create_system_reward(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
        username: str,
        amount: int,
        reason: str
    ) -> BytesTransaction:
        """Create system reward transaction (system gives user bytes).
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user ID receiving the reward
            username: Username for audit
            amount: Amount to reward (positive integer)
            reason: Reason for the reward (e.g. "Daily reward", "New member bonus")
            
        Returns:
            BytesTransaction: Created transaction record
            
        Raises:
            DatabaseOperationError: If transaction fails
        """
        try:
            # Get or create user balance
            balance = await self.get_or_create_balance(session, guild_id, user_id)
            
            # Update balance
            balance.balance += amount
            balance.total_received += amount
            
            # Create transaction record with system as giver
            transaction = BytesTransaction(
                guild_id=guild_id,
                giver_id="SYSTEM",  # Special giver for system rewards
                giver_username="System",
                receiver_id=user_id,
                receiver_username=username,
                amount=amount,
                reason=reason
            )
            
            session.add(transaction)
            await session.flush()  # Ensure timestamps are populated
            
            return transaction
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to create system reward: {e}") from e
    
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
        username: str,
        daily_amount: int,
        streak_bonus: int = 1,
        new_streak_count: Optional[int] = None,
        claim_date: Optional[date] = None,
        is_new_member: bool = False
    ) -> BytesBalance:
        """Update balance with daily reward and streak tracking using transaction records.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            username: Username for transaction audit
            daily_amount: Base daily reward amount
            streak_bonus: Streak multiplier
            new_streak_count: Optional streak count to set, defaults to incrementing existing
            claim_date: UTC date of claim, defaults to today
            is_new_member: Whether this is a new member getting starting balance
            
        Returns:
            BytesBalance: Updated balance record
            
        Raises:
            DatabaseOperationError: If update fails
        """
        try:
            balance = await self.get_balance(session, guild_id, user_id)
            
            # Calculate total reward
            reward_amount = daily_amount * streak_bonus
            
            # Calculate the new streak count
            final_streak_count = new_streak_count if new_streak_count is not None else balance.streak_count + 1
            
            # Build descriptive reason message
            if is_new_member:
                reason = "New member welcome bonus"
            elif streak_bonus > 1:
                reason = f"Daily reward (Day {final_streak_count}, {streak_bonus}x multiplier)"
            else:
                reason = f"Daily reward (Day {final_streak_count})"
            
            # Update balance directly (instead of using create_system_reward which gets a different balance object)
            balance.balance += reward_amount
            balance.total_received += reward_amount
            balance.last_daily = claim_date or date.today()
            balance.streak_count = final_streak_count
            
            # Create transaction record for audit trail
            transaction = BytesTransaction(
                guild_id=guild_id,
                giver_id="SYSTEM",
                giver_username="System",
                receiver_id=user_id,
                receiver_username=username,
                amount=reward_amount,
                reason=reason
            )
            
            session.add(transaction)
            await session.flush()  # Ensure timestamps are populated
            
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
            await session.flush()  # Ensure timestamps are populated
        
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
    
    async def update_squad(
        self,
        session: AsyncSession,
        squad_id: UUID,
        updates: Dict[str, Any]
    ) -> Squad:
        """Update a squad's information.
        
        Args:
            session: Database session
            squad_id: Squad UUID
            updates: Dictionary of fields to update
            
        Returns:
            Squad: Updated squad
            
        Raises:
            NotFoundError: If squad doesn't exist
            DatabaseOperationError: If update fails
        """
        try:
            squad = await self.get_squad(session, squad_id)
            
            for field, value in updates.items():
                if hasattr(squad, field):
                    setattr(squad, field, value)
            
            return squad
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to update squad: {e}") from e
    
    async def delete_squad(
        self,
        session: AsyncSession,
        squad_id: UUID
    ) -> None:
        """Delete a squad and all its memberships.
        
        Args:
            session: Database session
            squad_id: Squad UUID
            
        Raises:
            NotFoundError: If squad doesn't exist
            DatabaseOperationError: If deletion fails
        """
        try:
            # First delete all memberships
            stmt = delete(SquadMembership).where(SquadMembership.squad_id == squad_id)
            await session.execute(stmt)
            
            # Then delete the squad
            stmt = delete(Squad).where(Squad.id == squad_id)
            result = await session.execute(stmt)
            
            if result.rowcount == 0:
                raise NotFoundError(f"Squad not found: {squad_id}")
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to delete squad: {e}") from e
    
    async def join_squad(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
        squad_id: UUID,
        username: Optional[str] = None
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
                # Create system charge transaction for squad join fee
                await bytes_ops.create_system_charge(
                    session,
                    guild_id,
                    user_id,
                    username or f"User {user_id}",  # Use provided username or fallback
                    squad.switch_cost,
                    f"Squad join fee: {squad.name}"
                )
            
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