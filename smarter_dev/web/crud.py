"""Database operations for the Smarter Dev application.

This module provides CRUD operations for all models, following SOLID principles
and ensuring proper separation of concerns. All operations are async and use
SQLAlchemy 2.0 syntax.
"""

from __future__ import annotations

from typing import Optional, List, Dict, Any, Tuple
from uuid import UUID
from datetime import datetime, timezone, date

from sqlalchemy import select, update, delete, func, desc, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError, NoResultFound

from smarter_dev.web.models import (
    BytesBalance,
    BytesTransaction,
    BytesConfig,
    Squad,
    SquadMembership,
    APIKey,
    ForumAgent,
    ForumAgentResponse,
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
            **squad_data: Additional squad parameters (including is_default)
            
        Returns:
            Squad: Created squad
            
        Raises:
            ConflictError: If role is already associated with a squad or default squad conflict
            DatabaseOperationError: If creation fails
        """
        try:
            # Check if trying to create a default squad when one already exists
            if squad_data.get('is_default', False):
                existing_default = await self.get_default_squad(session, guild_id)
                if existing_default:
                    raise ConflictError(f"Guild already has a default squad: {existing_default.name}")
            
            squad = Squad(
                guild_id=guild_id,
                role_id=role_id,
                name=name,
                **squad_data
            )
            session.add(squad)
            return squad
            
        except IntegrityError as e:
            # Check if it's the unique constraint for default squad
            if "uq_squads_guild_default" in str(e):
                raise ConflictError("Guild already has a default squad") from e
            else:
                raise ConflictError(f"Role {role_id} already associated with a squad") from e
        except ConflictError:
            raise
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
            ConflictError: If trying to set as default when another default exists
            DatabaseOperationError: If update fails
        """
        try:
            squad = await self.get_squad(session, squad_id)
            
            # Handle is_default field specially to ensure only one default per guild
            if 'is_default' in updates and updates['is_default']:
                # Check if there's already a default squad in this guild
                existing_default = await self.get_default_squad(session, squad.guild_id)
                if existing_default and existing_default.id != squad_id:
                    raise ConflictError(f"Guild already has a default squad: {existing_default.name}")
                
                # Clear any existing default first (in case of race conditions)
                await self._clear_default_squad(session, squad.guild_id)
            
            for field, value in updates.items():
                if hasattr(squad, field):
                    setattr(squad, field, value)
            
            return squad
            
        except (NotFoundError, ConflictError):
            raise
        except IntegrityError as e:
            if "uq_squads_guild_default" in str(e):
                raise ConflictError("Guild already has a default squad") from e
            raise DatabaseOperationError(f"Failed to update squad: {e}") from e
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
            
            # Prevent manual joining of default squads
            if squad.is_default:
                raise ConflictError("Cannot manually join the default squad. Users are automatically assigned when earning bytes.")
            
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
    
    async def get_default_squad(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> Optional[Squad]:
        """Get the default squad for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            
        Returns:
            Optional[Squad]: Default squad if exists, None otherwise
            
        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(Squad).where(
                Squad.guild_id == guild_id,
                Squad.is_default == True,
                Squad.is_active == True
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get default squad: {e}") from e
    
    async def set_default_squad(
        self,
        session: AsyncSession,
        squad_id: UUID
    ) -> Squad:
        """Set a squad as the default for its guild.
        
        This method ensures only one default squad per guild by:
        1. Clearing any existing default squad in the guild
        2. Setting the specified squad as default
        
        Args:
            session: Database session
            squad_id: Squad UUID to set as default
            
        Returns:
            Squad: The updated default squad
            
        Raises:
            NotFoundError: If squad doesn't exist
            DatabaseOperationError: If update fails
        """
        try:
            # Get the squad to be made default
            squad = await self.get_squad(session, squad_id)
            
            # Clear existing default squad in this guild (if any)
            await self._clear_default_squad(session, squad.guild_id)
            
            # Set this squad as default
            squad.is_default = True
            
            return squad
            
        except NotFoundError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to set default squad: {e}") from e
    
    async def clear_default_squad(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> None:
        """Clear the default squad for a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            
        Raises:
            DatabaseOperationError: If update fails
        """
        try:
            await self._clear_default_squad(session, guild_id)
        except Exception as e:
            raise DatabaseOperationError(f"Failed to clear default squad: {e}") from e
    
    async def _clear_default_squad(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> None:
        """Internal method to clear default squad for a guild."""
        stmt = update(Squad).where(
            Squad.guild_id == guild_id,
            Squad.is_default == True
        ).values(is_default=False)
        await session.execute(stmt)
    
    async def auto_assign_to_default_squad(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
        username: Optional[str] = None
    ) -> Optional[Squad]:
        """Auto-assign a user to the default squad if they're not in any squad.
        
        This method is called when users earn bytes but aren't in a squad.
        It will only assign them if:
        1. They are not currently in any squad
        2. A default squad exists and is active
        3. The default squad is not full
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            username: Optional username for the membership record
            
        Returns:
            Optional[Squad]: The default squad if assignment occurred, None otherwise
            
        Raises:
            DatabaseOperationError: If operation fails
        """
        # Import logging for debugging
        import logging
        logger = logging.getLogger(__name__)
        
        try:
            # Check if user is already in a squad
            current_squad = await self.get_user_squad(session, guild_id, user_id)
            if current_squad:
                logger.info(f"User {user_id} already in squad '{current_squad.name}' in guild {guild_id}, no auto-assignment needed")
                return None  # User is already in a squad
            
            # Get default squad for guild
            default_squad = await self.get_default_squad(session, guild_id)
            if not default_squad:
                logger.info(f"No default squad configured for guild {guild_id}, cannot auto-assign user {user_id}")
                return None  # No default squad configured
            
            logger.info(f"Found default squad '{default_squad.name}' for guild {guild_id}, checking if user {user_id} can be assigned")
            
            # Check if default squad is active
            if not default_squad.is_active:
                logger.info(f"Default squad '{default_squad.name}' is inactive in guild {guild_id}, cannot auto-assign user {user_id}")
                return None  # Default squad is inactive
            
            # Check if default squad is full
            if default_squad.max_members:
                member_count = await self._get_squad_member_count(session, default_squad.id)
                if member_count >= default_squad.max_members:
                    logger.info(f"Default squad '{default_squad.name}' is full ({member_count}/{default_squad.max_members}) in guild {guild_id}, cannot auto-assign user {user_id}")
                    return None  # Default squad is full
            
            # Auto-assign user to default squad (no cost for default squad assignment)
            membership = SquadMembership(
                squad_id=default_squad.id,
                user_id=user_id,
                guild_id=guild_id,
                joined_at=datetime.now(timezone.utc)
            )
            session.add(membership)
            
            # Import logging to track auto-assignments
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Auto-assigned user {user_id} to default squad '{default_squad.name}' in guild {guild_id}")
            
            return default_squad
            
        except Exception as e:
            # Import logging for error reporting  
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to auto-assign user {user_id} to default squad in guild {guild_id}: {e}")
            # Don't raise the error - auto-assignment failure shouldn't break bytes earning
            return None


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
            
            # Auto-assign receiver to default squad if they aren't in any squad
            await self._auto_assign_default_squad_if_needed(session, guild_id, receiver_id, receiver_username)
            
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
            
            # Auto-assign user to default squad if they aren't in any squad
            await self._auto_assign_default_squad_if_needed(session, guild_id, user_id, username)
            
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
    
    async def get_sent_transaction_history(
        self,
        session: AsyncSession,
        guild_id: str,
        sender_user_id: str,
        limit: int = 20
    ) -> List[BytesTransaction]:
        """Get transaction history for user-to-user transfers sent by a specific user.
        
        This method only returns transactions where the specified user was the sender (giver)
        to another user, excluding system charges like squad join fees. This is useful for 
        cooldown checks where we only care about user-to-user transfers, not system payments.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            sender_user_id: User ID to filter by (as sender only)
            limit: Maximum number of results
            
        Returns:
            List[BytesTransaction]: User-to-user transactions where user was sender, ordered by creation time descending
            
        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = (
                select(BytesTransaction)
                .where(BytesTransaction.guild_id == guild_id)
                .where(BytesTransaction.giver_id == sender_user_id)  # Only transactions where user was sender
                .where(BytesTransaction.receiver_id != "SYSTEM")  # Exclude system charges (squad fees, etc.)
                .order_by(desc(BytesTransaction.created_at))
                .limit(limit)
            )
            
            result = await session.execute(stmt)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get sent transaction history: {e}") from e
    
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
    ) -> tuple[BytesBalance, Optional["Squad"]]:
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
            tuple[BytesBalance, Optional["Squad"]]: Updated balance record and assigned squad (if auto-assigned)
            
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
            
            # Auto-assign to default squad if user isn't in any squad
            assigned_squad = await self._auto_assign_default_squad_if_needed(session, guild_id, user_id, username)
            
            return balance, assigned_squad
            
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
    
    async def _auto_assign_default_squad_if_needed(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
        username: Optional[str] = None
    ) -> Optional["Squad"]:
        """Helper method to auto-assign user to default squad if they're not in any squad.
        
        This method is called after users earn bytes to check if they should be 
        auto-assigned to the default squad. It gracefully handles any errors
        to ensure bytes earning is never disrupted.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID  
            user_id: Discord user snowflake ID
            username: Optional username for logging
            
        Returns:
            Squad: The squad the user was assigned to, or None if no assignment occurred
        """
        # Import logging here for better visibility
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Checking auto-assignment for user {user_id} in guild {guild_id} after earning bytes")
        
        try:
            # Use SquadOperations to handle the auto-assignment
            squad_ops = SquadOperations()
            assigned_squad = await squad_ops.auto_assign_to_default_squad(
                session, guild_id, user_id, username
            )
            
            if assigned_squad:
                logger.info(f"Auto-assigned user {user_id} to default squad '{assigned_squad.name}' in guild {guild_id} after earning bytes")
                return assigned_squad
            else:
                logger.info(f"No auto-assignment needed for user {user_id} in guild {guild_id} (may already be in squad or no default squad exists)")
                return None
                
        except Exception as e:
            logger.warning(f"Failed to auto-assign user {user_id} to default squad in guild {guild_id} after earning bytes: {e}")
            # Don't raise - squad assignment failure shouldn't break bytes earning
            return None


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



class APIKeyOperations:
    """CRUD operations for API key management with security features.
    
    Provides secure API key generation, validation, and management operations
    with proper cryptographic practices and audit trails.
    """
    
    async def create_api_key(
        self,
        session: AsyncSession,
        name: str,
        scopes: List[str],
        created_by: str,
        expires_days: Optional[int] = None,
        rate_limit_per_hour: int = 1000
    ) -> Tuple[APIKey, str]:
        """Create a new API key with secure generation.
        
        Args:
            session: Database session
            name: Human-readable name for the API key
            scopes: List of permission scopes
            created_by: Username of the creator
            expires_days: Optional expiration in days
            rate_limit_per_hour: Rate limit for this key
            
        Returns:
            tuple: (APIKey model, plaintext_key)
            
        Raises:
            ConflictError: If name already exists
            DatabaseOperationError: If creation fails
        """
        from smarter_dev.web.security import generate_secure_api_key
        from smarter_dev.web.models import APIKey
        from datetime import timedelta
        
        try:
            # Check if name already exists
            stmt = select(APIKey).where(APIKey.name == name)
            result = await session.execute(stmt)
            if result.scalar_one_or_none():
                raise ConflictError(f"API key name '{name}' already exists")
            
            # Generate secure API key
            full_key, key_hash, key_prefix = generate_secure_api_key()
            
            # Calculate expiration
            expires_at = None
            if expires_days and expires_days > 0:
                expires_at = datetime.now(timezone.utc) + timedelta(days=expires_days)
            
            # Create API key record
            api_key = APIKey(
                name=name,
                key_hash=key_hash,
                key_prefix=key_prefix,
                scopes=scopes,
                expires_at=expires_at,
                rate_limit_per_hour=rate_limit_per_hour,
                created_by=created_by
            )
            
            session.add(api_key)
            await session.commit()
            await session.refresh(api_key)
            
            # Return both the model and plaintext key (shown only once)
            return api_key, full_key
            
        except ConflictError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to create API key: {e}") from e
    
    async def get_api_key_by_hash(
        self,
        session: AsyncSession,
        key_hash: str
    ) -> Optional[APIKey]:
        """Get API key by hash for authentication.
        
        Args:
            session: Database session
            key_hash: SHA-256 hash of the API key
            
        Returns:
            APIKey or None if not found
            
        Raises:
            DatabaseOperationError: If query fails
        """
        from smarter_dev.web.models import APIKey
        
        try:
            stmt = (
                select(APIKey)
                .where(
                    APIKey.key_hash == key_hash,
                    APIKey.is_active == True
                )
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get API key: {e}") from e
    
    async def list_api_keys(
        self,
        session: AsyncSession,
        include_inactive: bool = False
    ) -> List[APIKey]:
        """List all API keys with usage statistics.
        
        Args:
            session: Database session
            include_inactive: Whether to include inactive keys
            
        Returns:
            List of APIKey models
            
        Raises:
            DatabaseOperationError: If query fails
        """
        from smarter_dev.web.models import APIKey
        
        try:
            stmt = select(APIKey).order_by(APIKey.created_at.desc())
            
            if not include_inactive:
                stmt = stmt.where(APIKey.is_active == True)
            
            result = await session.execute(stmt)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to list API keys: {e}") from e
    
    async def get_api_key_by_id(
        self,
        session: AsyncSession,
        key_id: UUID
    ) -> Optional[APIKey]:
        """Get API key by ID.
        
        Args:
            session: Database session
            key_id: API key UUID
            
        Returns:
            APIKey or None if not found
            
        Raises:
            DatabaseOperationError: If query fails
        """
        from smarter_dev.web.models import APIKey
        
        try:
            stmt = select(APIKey).where(APIKey.id == key_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get API key: {e}") from e
    
    async def revoke_api_key(
        self,
        session: AsyncSession,
        key_id: UUID
    ) -> bool:
        """Revoke (deactivate) an API key.
        
        Args:
            session: Database session
            key_id: API key UUID
            
        Returns:
            bool: True if revoked, False if not found
            
        Raises:
            DatabaseOperationError: If operation fails
        """
        from smarter_dev.web.models import APIKey
        
        try:
            stmt = (
                update(APIKey)
                .where(APIKey.id == key_id)
                .values(
                    is_active=False,
                    updated_at=datetime.now(timezone.utc)
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            
            return result.rowcount > 0
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to revoke API key: {e}") from e
    
    async def activate_api_key(
        self,
        session: AsyncSession,
        key_id: UUID
    ) -> bool:
        """Activate a revoked API key.
        
        Args:
            session: Database session
            key_id: API key UUID
            
        Returns:
            bool: True if activated, False if not found
            
        Raises:
            DatabaseOperationError: If operation fails
        """
        from smarter_dev.web.models import APIKey
        
        try:
            stmt = (
                update(APIKey)
                .where(APIKey.id == key_id)
                .values(
                    is_active=True,
                    updated_at=datetime.now(timezone.utc)
                )
            )
            result = await session.execute(stmt)
            await session.commit()
            
            return result.rowcount > 0
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to activate API key: {e}") from e
    
    async def delete_api_key(
        self,
        session: AsyncSession,
        key_id: UUID
    ) -> bool:
        """Permanently delete an API key.
        
        Args:
            session: Database session
            key_id: API key UUID
            
        Returns:
            bool: True if deleted, False if not found
            
        Raises:
            DatabaseOperationError: If operation fails
        """
        from smarter_dev.web.models import APIKey
        
        try:
            stmt = delete(APIKey).where(APIKey.id == key_id)
            result = await session.execute(stmt)
            await session.commit()
            
            return result.rowcount > 0
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to delete API key: {e}") from e
    
    async def update_last_used(
        self,
        session: AsyncSession,
        key_id: UUID
    ) -> None:
        """Update the last used timestamp and increment usage count.
        
        Args:
            session: Database session
            key_id: API key UUID
            
        Note:
            This operation is fire-and-forget to avoid blocking API requests.
        """
        from smarter_dev.web.models import APIKey
        
        try:
            stmt = (
                update(APIKey)
                .where(APIKey.id == key_id)
                .values(
                    last_used_at=datetime.now(timezone.utc),
                    usage_count=APIKey.usage_count + 1,
                    updated_at=datetime.now(timezone.utc)
                )
            )
            await session.execute(stmt)
            await session.commit()
            
        except Exception:
            # Silently fail to avoid breaking API requests
            # This is tracked separately for monitoring
            pass
    
    async def list_api_keys(
        self,
        db: AsyncSession,
        offset: int = 0,
        limit: int = 20,
        active_only: bool = False,
        search: Optional[str] = None
    ) -> tuple[List[APIKey], int]:
        """List API keys with pagination and filtering.
        
        Args:
            db: Database session
            offset: Number of records to skip
            limit: Maximum number of records to return
            active_only: Whether to show only active keys
            search: Search term for name or description
            
        Returns:
            Tuple of (list of API keys, total count)
        """
        from smarter_dev.web.models import APIKey
        
        try:
            # Base query
            query = select(APIKey)
            count_query = select(func.count(APIKey.id))
            
            # Apply filters
            filters = []
            
            if active_only:
                filters.append(APIKey.is_active == True)
            
            if search:
                search_filter = or_(
                    APIKey.name.ilike(f"%{search}%"),
                    APIKey.description.ilike(f"%{search}%")
                )
                filters.append(search_filter)
            
            if filters:
                query = query.where(and_(*filters))
                count_query = count_query.where(and_(*filters))
            
            # Get total count
            count_result = await db.execute(count_query)
            total = count_result.scalar() or 0
            
            # Apply pagination and ordering
            query = (
                query
                .order_by(APIKey.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
            
            # Execute query
            result = await db.execute(query)
            keys = list(result.scalars().all())
            
            return keys, total
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to list API keys: {e}") from e
    
    async def get_admin_stats(self, db: AsyncSession) -> dict:
        """Get admin statistics for the dashboard.
        
        Args:
            db: Database session
            
        Returns:
            Dictionary with admin statistics
        """
        from smarter_dev.web.models import APIKey
        
        try:
            # Count total API keys
            total_query = select(func.count(APIKey.id))
            total_result = await db.execute(total_query)
            total_api_keys = total_result.scalar() or 0
            
            # Count active API keys
            active_query = select(func.count(APIKey.id)).where(APIKey.is_active == True)
            active_result = await db.execute(active_query)
            active_api_keys = active_result.scalar() or 0
            
            # Count revoked API keys
            revoked_query = select(func.count(APIKey.id)).where(APIKey.is_active == False)
            revoked_result = await db.execute(revoked_query)
            revoked_api_keys = revoked_result.scalar() or 0
            
            # Count expired API keys (active but past expiration)
            now = datetime.now(timezone.utc)
            expired_query = select(func.count(APIKey.id)).where(
                and_(
                    APIKey.is_active == True,
                    APIKey.expires_at < now
                )
            )
            expired_result = await db.execute(expired_query)
            expired_api_keys = expired_result.scalar() or 0
            
            # Calculate total requests
            usage_query = select(func.sum(APIKey.usage_count))
            usage_result = await db.execute(usage_query)
            total_api_requests = usage_result.scalar() or 0
            
            # Get top consumers (top 5 by usage count)
            top_consumers_query = (
                select(APIKey.name, APIKey.usage_count, APIKey.key_prefix)
                .where(APIKey.usage_count > 0)
                .order_by(APIKey.usage_count.desc())
                .limit(5)
            )
            top_consumers_result = await db.execute(top_consumers_query)
            top_consumers = [
                {
                    "name": row.name,
                    "usage_count": row.usage_count,
                    "key_prefix": row.key_prefix
                }
                for row in top_consumers_result.fetchall()
            ]
            
            return {
                "total_api_keys": total_api_keys,
                "active_api_keys": active_api_keys,
                "revoked_api_keys": revoked_api_keys,
                "expired_api_keys": expired_api_keys,
                "total_api_requests": total_api_requests,
                "api_requests_today": 0,  # TODO: Implement daily tracking
                "top_api_consumers": top_consumers
            }
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get admin stats: {e}") from e


class ForumAgentOperations:
    """Database operations for forum agents.
    
    This class encapsulates all database operations related to forum agents
    and their responses. Follows the Single Responsibility Principle.
    """
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_agent(
        self,
        guild_id: str,
        name: str,
        system_prompt: str,
        monitored_forums: List[str],
        response_threshold: float = 0.7,
        max_responses_per_hour: int = 5,
        description: str = None,
        is_active: bool = True,
        created_by: str = "admin"
    ) -> ForumAgent:
        """Create a new forum agent.
        
        Args:
            guild_id: Discord guild ID
            name: Agent name
            system_prompt: AI system prompt
            monitored_forums: List of forum channel IDs to monitor
            response_threshold: Minimum confidence to respond
            max_responses_per_hour: Rate limit for responses
            description: Optional description
            is_active: Whether agent should be active immediately
            created_by: Who created the agent
            
        Returns:
            Created ForumAgent instance
            
        Raises:
            ConflictError: If agent with same name already exists in guild
            DatabaseOperationError: If creation fails
        """
        try:
            agent = ForumAgent(
                guild_id=guild_id,
                name=name,
                description=description,
                system_prompt=system_prompt,
                monitored_forums=monitored_forums,
                response_threshold=response_threshold,
                max_responses_per_hour=max_responses_per_hour,
                is_active=is_active,
                created_by=created_by
            )
            
            self.session.add(agent)
            await self.session.commit()
            await self.session.refresh(agent)
            
            return agent
            
        except IntegrityError as e:
            await self.session.rollback()
            if "UNIQUE constraint" in str(e) or "unique" in str(e).lower():
                raise ConflictError(f"Agent with name '{name}' already exists in guild") from e
            raise DatabaseOperationError(f"Failed to create agent: {e}") from e
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to create agent: {e}") from e
    
    async def get_agent(self, agent_id: UUID, guild_id: str) -> Optional[ForumAgent]:
        """Get a forum agent by ID.
        
        Args:
            agent_id: Agent UUID
            guild_id: Discord guild ID (for security)
            
        Returns:
            ForumAgent instance or None if not found
        """
        try:
            result = await self.session.execute(
                select(ForumAgent)
                .where(and_(ForumAgent.id == agent_id, ForumAgent.guild_id == guild_id))
            )
            return result.scalar_one_or_none()
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get agent: {e}") from e
    
    async def list_agents(self, guild_id: str, active_only: bool = False) -> List[ForumAgent]:
        """List all forum agents for a guild.
        
        Args:
            guild_id: Discord guild ID
            active_only: If True, only return active agents
            
        Returns:
            List of ForumAgent instances
        """
        try:
            query = select(ForumAgent).where(ForumAgent.guild_id == guild_id)
            
            if active_only:
                query = query.where(ForumAgent.is_active == True)
            
            query = query.order_by(ForumAgent.name)
            
            result = await self.session.execute(query)
            return list(result.scalars().all())
        except Exception as e:
            raise DatabaseOperationError(f"Failed to list agents: {e}") from e
    
    async def update_agent(
        self,
        agent_id: UUID,
        guild_id: str,
        **updates
    ) -> Optional[ForumAgent]:
        """Update a forum agent.
        
        Args:
            agent_id: Agent UUID
            guild_id: Discord guild ID (for security)
            **updates: Fields to update
            
        Returns:
            Updated ForumAgent instance or None if not found
            
        Raises:
            ConflictError: If update violates constraints
            DatabaseOperationError: If update fails
        """
        try:
            # Get the agent first to ensure it exists and belongs to guild
            agent = await self.get_agent(agent_id, guild_id)
            if not agent:
                return None
            
            # Apply updates
            for field, value in updates.items():
                if hasattr(agent, field):
                    setattr(agent, field, value)
            
            agent.updated_at = datetime.now(timezone.utc)
            
            await self.session.commit()
            await self.session.refresh(agent)
            
            return agent
            
        except IntegrityError as e:
            await self.session.rollback()
            if "UNIQUE constraint" in str(e) or "unique" in str(e).lower():
                raise ConflictError(f"Update violates unique constraint") from e
            raise DatabaseOperationError(f"Failed to update agent: {e}") from e
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to update agent: {e}") from e
    
    async def delete_agent(self, agent_id: UUID, guild_id: str) -> bool:
        """Delete a forum agent.
        
        Args:
            agent_id: Agent UUID
            guild_id: Discord guild ID (for security)
            
        Returns:
            True if deleted, False if not found
            
        Raises:
            DatabaseOperationError: If deletion fails
        """
        try:
            # Get the agent first to ensure it exists and belongs to guild
            agent = await self.get_agent(agent_id, guild_id)
            if not agent:
                return False
            
            await self.session.delete(agent)
            await self.session.commit()
            
            return True
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to delete agent: {e}") from e
    
    async def toggle_agent(self, agent_id: UUID, guild_id: str) -> Optional[ForumAgent]:
        """Toggle agent active status.
        
        Args:
            agent_id: Agent UUID
            guild_id: Discord guild ID (for security)
            
        Returns:
            Updated ForumAgent instance or None if not found
        """
        try:
            agent = await self.get_agent(agent_id, guild_id)
            if not agent:
                return None
            
            agent.is_active = not agent.is_active
            agent.updated_at = datetime.now(timezone.utc)
            
            await self.session.commit()
            await self.session.refresh(agent)
            
            return agent
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to toggle agent: {e}") from e
    
    async def get_agent_analytics(self, agent_id: UUID, guild_id: str) -> Dict[str, Any]:
        """Get analytics for a forum agent.
        
        Args:
            agent_id: Agent UUID
            guild_id: Discord guild ID (for security)
            
        Returns:
            Dictionary containing analytics data
        """
        try:
            # Verify agent exists and belongs to guild
            agent = await self.get_agent(agent_id, guild_id)
            if not agent:
                return {}
            
            # Get response statistics
            total_responses_result = await self.session.execute(
                select(func.count(ForumAgentResponse.id))
                .where(ForumAgentResponse.agent_id == agent_id)
            )
            total_responses = total_responses_result.scalar() or 0
            
            responses_posted_result = await self.session.execute(
                select(func.count(ForumAgentResponse.id))
                .where(and_(
                    ForumAgentResponse.agent_id == agent_id,
                    ForumAgentResponse.responded == True
                ))
            )
            responses_posted = responses_posted_result.scalar() or 0
            
            total_tokens_result = await self.session.execute(
                select(func.coalesce(func.sum(ForumAgentResponse.tokens_used), 0))
                .where(ForumAgentResponse.agent_id == agent_id)
            )
            total_tokens = total_tokens_result.scalar() or 0
            
            avg_confidence_result = await self.session.execute(
                select(func.avg(ForumAgentResponse.confidence_score))
                .where(ForumAgentResponse.agent_id == agent_id)
                .where(ForumAgentResponse.confidence_score.is_not(None))
            )
            avg_confidence = avg_confidence_result.scalar()
            
            avg_response_time_result = await self.session.execute(
                select(func.avg(ForumAgentResponse.response_time_ms))
                .where(ForumAgentResponse.agent_id == agent_id)
            )
            avg_response_time = avg_response_time_result.scalar()
            
            # Get recent responses for activity table
            recent_responses_result = await self.session.execute(
                select(ForumAgentResponse)
                .where(ForumAgentResponse.agent_id == agent_id)
                .order_by(ForumAgentResponse.created_at.desc())
                .limit(20)
            )
            recent_responses_raw = recent_responses_result.scalars().all()
            
            # Format recent responses for template
            from types import SimpleNamespace
            recent_responses = []
            for response in recent_responses_raw:
                # Convert to object with attributes for template compatibility
                response_obj = SimpleNamespace(
                    id=str(response.id),
                    post_title=response.post_title or "Untitled",
                    author_display_name=response.author_display_name or "Unknown",  # Match template expectation
                    confidence_score=response.confidence_score,
                    responded=response.responded,
                    tokens_used=response.tokens_used or 0,
                    created_at=response.created_at,  # Match template expectation
                    post_tags=response.post_tags or [],
                    response_content=response.response_content[:100] if response.response_content else None,
                    decision_reasoning=response.decision_reason,
                    full_response_content=response.response_content
                )
                recent_responses.append(response_obj)
            
            return {
                "agent": {
                    "id": str(agent.id),
                    "name": agent.name,
                    "system_prompt": agent.system_prompt,
                    "is_active": agent.is_active,
                    "created_at": agent.created_at,
                    "updated_at": agent.updated_at,
                    "response_threshold": agent.response_threshold,
                    "max_responses_per_hour": agent.max_responses_per_hour,
                    "created_by": agent.created_by,
                },
                "statistics": {
                    "total_evaluations": total_responses,
                    "total_responses": responses_posted,
                    "response_rate": responses_posted / max(1, total_responses),
                    "total_tokens_used": total_tokens,
                    "average_confidence": avg_confidence,
                    "average_response_time_ms": avg_response_time,
                },
                "recent_responses": recent_responses
            }
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get agent analytics: {e}") from e
    
    async def get_guild_agent_overview(self, guild_id: str) -> Dict[str, Any]:
        """Get overview of all forum agents in a guild.
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            Dictionary containing guild agent overview
        """
        try:
            # Get agent counts
            total_agents_result = await self.session.execute(
                select(func.count(ForumAgent.id))
                .where(ForumAgent.guild_id == guild_id)
            )
            total_agents = total_agents_result.scalar() or 0
            
            active_agents_result = await self.session.execute(
                select(func.count(ForumAgent.id))
                .where(and_(ForumAgent.guild_id == guild_id, ForumAgent.is_active == True))
            )
            active_agents = active_agents_result.scalar() or 0
            
            # Get all agents with basic info
            agents_result = await self.session.execute(
                select(ForumAgent)
                .where(ForumAgent.guild_id == guild_id)
                .order_by(ForumAgent.name)
            )
            agents = list(agents_result.scalars().all())
            
            # Get agent summaries with response counts
            agent_summaries = []
            for agent in agents:
                responses_count_result = await self.session.execute(
                    select(func.count(ForumAgentResponse.id))
                    .where(ForumAgentResponse.agent_id == agent.id)
                )
                responses_count = responses_count_result.scalar() or 0
                
                agent_summaries.append({
                    "id": str(agent.id),
                    "name": agent.name,
                    "is_active": agent.is_active,
                    "response_count": responses_count,
                    "monitored_forums_count": len(agent.monitored_forums),
                    "response_threshold": agent.response_threshold,
                })
            
            return {
                "guild_id": guild_id,
                "total_agents": total_agents,
                "active_agents": active_agents,
                "overall_statistics": {
                    "total_agents": total_agents,
                    "active_percentage": active_agents / max(1, total_agents) * 100,
                },
                "agent_summaries": agent_summaries,
            }
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get guild agent overview: {e}") from e
    
    async def bulk_update_agents(
        self,
        agent_ids: List[UUID],
        guild_id: str,
        action: str
    ) -> int:
        """Perform bulk operations on forum agents.
        
        Args:
            agent_ids: List of agent UUIDs
            guild_id: Discord guild ID (for security)
            action: Action to perform ("enable", "disable", "delete")
            
        Returns:
            Number of agents modified
            
        Raises:
            DatabaseOperationError: If bulk operation fails
        """
        try:
            if not agent_ids:
                return 0
            
            base_query = select(ForumAgent).where(
                and_(
                    ForumAgent.id.in_(agent_ids),
                    ForumAgent.guild_id == guild_id
                )
            )
            
            result = await self.session.execute(base_query)
            agents = list(result.scalars().all())
            
            if not agents:
                return 0
            
            modified_count = 0
            
            if action == "enable":
                for agent in agents:
                    if not agent.is_active:
                        agent.is_active = True
                        agent.updated_at = datetime.now(timezone.utc)
                        modified_count += 1
                        
            elif action == "disable":
                for agent in agents:
                    if agent.is_active:
                        agent.is_active = False
                        agent.updated_at = datetime.now(timezone.utc)
                        modified_count += 1
                        
            elif action == "delete":
                for agent in agents:
                    await self.session.delete(agent)
                    modified_count += 1
            
            else:
                raise ValueError(f"Invalid bulk action: {action}")
            
            await self.session.commit()
            return modified_count
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to perform bulk operation: {e}") from e