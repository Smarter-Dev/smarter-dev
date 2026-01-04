"""Database operations for the Smarter Dev application.

This module provides CRUD operations for all models, following SOLID principles
and ensuring proper separation of concerns. All operations are async and use
SQLAlchemy 2.0 syntax.
"""

from __future__ import annotations

import logging
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
    SquadSaleEvent,
    APIKey,
    ForumAgent,
    ForumAgentResponse,
    ForumNotificationTopic,
    ForumUserSubscription,
    Quest,
    QuestProgress,
    QuestSubmission,
    QuestInput,
    DailyQuest,
    Campaign,
    Challenge,
    ChallengeInput,
    ChallengeSubmission,
    ScheduledMessage,
    RepeatingMessage,
    AuditLogConfig,
    AdventOfCodeConfig,
    AdventOfCodeThread,
    AttachmentFilterConfig,
)

logger = logging.getLogger(__name__)


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
            is_switch = current_membership is not None  # Determine if this is a switch or first join
            
            if current_membership:
                raise ConflictError(f"User already in squad {current_membership.name}")
            
            # Check squad capacity
            if squad.max_members:
                member_count = await self._get_squad_member_count(session, squad_id)
                if member_count >= squad.max_members:
                    raise ConflictError(f"Squad {squad.name} is full")
            
            # Check and deduct switch cost if required (with sale discounts)
            if squad.switch_cost > 0:
                # Check for active sale events and calculate discounted cost
                sale_ops = SquadSaleEventOperations(session)
                
                discounted_cost, active_sale_event = await sale_ops.calculate_discounted_cost(
                    guild_id=guild_id,
                    original_cost=squad.switch_cost,
                    is_switch=is_switch
                )
                
                # Create appropriate transaction description
                if active_sale_event and discounted_cost < squad.switch_cost:
                    discount_percent = active_sale_event.switch_discount_percent if is_switch else active_sale_event.join_discount_percent
                    transaction_reason = f"Squad {'switch' if is_switch else 'join'} fee: {squad.name} ({discount_percent}% off - {active_sale_event.name})"
                else:
                    transaction_reason = f"Squad {'switch' if is_switch else 'join'} fee: {squad.name}"
                
                bytes_ops = BytesOperations()
                # Create system charge transaction for squad join/switch fee
                await bytes_ops.create_system_charge(
                    session,
                    guild_id,
                    user_id,
                    username or f"User {user_id}",  # Use provided username or fallback
                    discounted_cost,
                    transaction_reason
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
    
    async def switch_squad(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
        new_squad_id: UUID,
        username: Optional[str] = None
    ) -> SquadMembership:
        """Switch a user from their current squad to a new squad with sale discount support.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            user_id: Discord user snowflake ID
            new_squad_id: UUID of the squad to switch to
            username: Optional username for transaction logging
            
        Returns:
            SquadMembership: New membership record
            
        Raises:
            NotFoundError: If user not in any squad or new squad doesn't exist
            ConflictError: If new squad is full, inactive, or same as current
            DatabaseOperationError: If operation fails
        """
        try:
            # Check current membership
            current_membership = await self.get_user_squad(session, guild_id, user_id)
            if not current_membership:
                raise NotFoundError(f"User {user_id} not in any squad")
            
            # Prevent switching to the same squad
            if current_membership.id == new_squad_id:
                raise ConflictError("User is already in this squad")
            
            # Get new squad and validate
            new_squad = await self.get_squad(session, new_squad_id)
            if not new_squad.is_active:
                raise ConflictError(f"Squad {new_squad.name} is not active")
            
            # Prevent switching to default squads
            if new_squad.is_default:
                raise ConflictError("Cannot manually switch to the default squad. Users are automatically assigned when earning bytes.")
            
            # Check squad capacity
            if new_squad.max_members:
                member_count = await self._get_squad_member_count(session, new_squad_id)
                if member_count >= new_squad.max_members:
                    raise ConflictError(f"Squad {new_squad.name} is full")
            
            # Check and deduct switch cost if required (with sale discounts)
            if new_squad.switch_cost > 0:
                # Check for active sale events and calculate discounted cost
                sale_ops = SquadSaleEventOperations(session)
                
                discounted_cost, active_sale_event = await sale_ops.calculate_discounted_cost(
                    guild_id=guild_id,
                    original_cost=new_squad.switch_cost,
                    is_switch=True  # This is always a switch operation
                )
                
                # Create appropriate transaction description
                if active_sale_event and discounted_cost < new_squad.switch_cost:
                    discount_percent = active_sale_event.switch_discount_percent
                    transaction_reason = f"Squad switch fee: {current_membership.name} → {new_squad.name} ({discount_percent}% off - {active_sale_event.name})"
                else:
                    transaction_reason = f"Squad switch fee: {current_membership.name} → {new_squad.name}"
                
                bytes_ops = BytesOperations()
                # Create system charge transaction for squad switch fee
                await bytes_ops.create_system_charge(
                    session,
                    guild_id,
                    user_id,
                    username or f"User {user_id}",
                    discounted_cost,
                    transaction_reason
                )
            
            # Remove from current squad
            await self.leave_squad(session, guild_id, user_id)
            
            # Create new membership
            new_membership = SquadMembership(
                squad_id=new_squad_id,
                user_id=user_id,
                guild_id=guild_id
            )
            session.add(new_membership)
            
            return new_membership
            
        except (NotFoundError, ConflictError):
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to switch squad: {e}") from e
    
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

    async def get_all_guild_squad_members(
        self,
        session: AsyncSession,
        guild_id: str,
        squad_filter: Optional[UUID] = None,
        username_search: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Get all squad members across all squads in a guild.
        
        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            squad_filter: Optional UUID to filter by specific squad
            username_search: Optional string to search in usernames
            
        Returns:
            List[Dict[str, Any]]: Squad members with squad information
            
        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            # Create subquery to get the most recent username for each user
            # We'll check both giver and receiver transactions to find the latest username
            latest_username_subq = (
                select(
                    BytesTransaction.giver_id.label('user_id'),
                    BytesTransaction.giver_username.label('username'),
                    BytesTransaction.created_at.label('last_transaction')
                )
                .where(
                    BytesTransaction.guild_id == guild_id,
                    BytesTransaction.giver_username.isnot(None),
                    BytesTransaction.giver_username != ''
                )
                .union_all(
                    select(
                        BytesTransaction.receiver_id.label('user_id'),
                        BytesTransaction.receiver_username.label('username'),
                        BytesTransaction.created_at.label('last_transaction')
                    )
                    .where(
                        BytesTransaction.guild_id == guild_id,
                        BytesTransaction.receiver_username.isnot(None),
                        BytesTransaction.receiver_username != ''
                    )
                )
                .subquery()
            )
            
            # Get the most recent username for each user_id
            latest_username_cte = (
                select(
                    latest_username_subq.c.user_id,
                    latest_username_subq.c.username,
                    func.row_number().over(
                        partition_by=latest_username_subq.c.user_id,
                        order_by=desc(latest_username_subq.c.last_transaction)
                    ).label('rn')
                )
                .select_from(latest_username_subq)
                .cte('latest_usernames')
            )
            
            # Build main query joining squad memberships with squads and usernames
            stmt = (
                select(
                    SquadMembership.user_id,
                    SquadMembership.joined_at,
                    Squad.id.label('squad_id'),
                    Squad.name.label('squad_name'),
                    Squad.role_id.label('squad_role_id'),
                    Squad.is_default.label('squad_is_default'),
                    Squad.is_active.label('squad_is_active'),
                    func.coalesce(latest_username_cte.c.username, SquadMembership.user_id).label('username')
                )
                .select_from(SquadMembership)
                .join(Squad, SquadMembership.squad_id == Squad.id)
                .outerjoin(
                    latest_username_cte,
                    and_(
                        latest_username_cte.c.user_id == SquadMembership.user_id,
                        latest_username_cte.c.rn == 1
                    )
                )
                .where(Squad.guild_id == guild_id)
                .order_by(SquadMembership.joined_at.desc())
            )
            
            # Apply filters if provided
            if squad_filter:
                stmt = stmt.where(Squad.id == squad_filter)
                
            if username_search:
                search_pattern = f"%{username_search}%"
                stmt = stmt.where(
                    or_(
                        func.coalesce(latest_username_cte.c.username, SquadMembership.user_id).ilike(search_pattern),
                        SquadMembership.user_id.ilike(search_pattern)
                    )
                )
            
            result = await session.execute(stmt)
            rows = result.fetchall()
            
            # Convert to list of dictionaries
            members = []
            for row in rows:
                members.append({
                    'user_id': row.user_id,
                    'username': row.username,  # Now contains actual username from transactions
                    'joined_at': row.joined_at,
                    'squad_id': str(row.squad_id),
                    'squad_name': row.squad_name,
                    'squad_role_id': row.squad_role_id,
                    'squad_is_default': row.squad_is_default,
                    'squad_is_active': row.squad_is_active
                })
            
            return members
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get all guild squad members: {e}") from e
    
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
        created_by: str = "admin",
        enable_user_tagging: bool = False,
        enable_responses: bool = True,
        notification_topics: List[str] = None,
        notification_topic_descriptions: List[str] = None
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
            enable_user_tagging: Whether agent should classify posts for user tagging
            enable_responses: Whether agent should generate responses to posts
            notification_topics: List of topic names for user notifications
            notification_topic_descriptions: List of topic descriptions (optional)
            
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
                created_by=created_by,
                enable_user_tagging=enable_user_tagging,
                enable_responses=enable_responses,
                notification_topics=notification_topics or []
            )
            
            self.session.add(agent)
            
            # Sync notification topics if user tagging is enabled
            if enable_user_tagging and notification_topics:
                await self.sync_notification_topics(
                    agent, 
                    notification_topics, 
                    notification_topic_descriptions
                )
            
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
            
            # Extract notification topics before applying other updates
            notification_topics = updates.pop('notification_topics', None)
            notification_topic_descriptions = updates.pop('notification_topic_descriptions', None)
            
            # Apply updates
            for field, value in updates.items():
                if hasattr(agent, field):
                    setattr(agent, field, value)
            
            agent.updated_at = datetime.now(timezone.utc)
            
            # Sync notification topics if user tagging is enabled
            if agent.enable_user_tagging and notification_topics is not None:
                await self.sync_notification_topics(
                    agent, 
                    notification_topics, 
                    notification_topic_descriptions
                )
            elif not agent.enable_user_tagging:
                # Clear topics if tagging is disabled
                await self.sync_notification_topics(agent, [])
            
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

    async def sync_notification_topics(
        self,
        agent: ForumAgent,
        topic_names: List[str],
        topic_descriptions: List[str] = None
    ) -> None:
        """Sync notification topics for a forum agent.
        
        Creates ForumNotificationTopic records for each monitored forum channel.
        Removes topics that are no longer in the list and adds new ones.
        
        Args:
            agent: ForumAgent instance
            topic_names: List of topic names
            topic_descriptions: Optional list of topic descriptions
        """
        try:
            if not topic_names:
                # Remove all topics if none specified
                delete_stmt = delete(ForumNotificationTopic).where(
                    and_(
                        ForumNotificationTopic.guild_id == agent.guild_id,
                        ForumNotificationTopic.forum_channel_id.in_(agent.monitored_forums or [])
                    )
                )
                await self.session.execute(delete_stmt)
                return

            # Ensure descriptions list matches topics list length
            descriptions = topic_descriptions or []
            while len(descriptions) < len(topic_names):
                descriptions.append("")
            
            # Process each monitored forum (or empty list if monitoring all)
            forums_to_process = agent.monitored_forums or ["*"]  # "*" represents all forums
            
            for forum_id in forums_to_process:
                # Get existing topics for this forum
                existing_stmt = select(ForumNotificationTopic).where(
                    and_(
                        ForumNotificationTopic.guild_id == agent.guild_id,
                        ForumNotificationTopic.forum_channel_id == forum_id
                    )
                )
                existing_topics = list((await self.session.execute(existing_stmt)).scalars().all())
                existing_topic_names = {topic.topic_name for topic in existing_topics}
                
                # Remove topics not in the new list
                topics_to_remove = existing_topic_names - set(topic_names)
                if topics_to_remove:
                    remove_stmt = delete(ForumNotificationTopic).where(
                        and_(
                            ForumNotificationTopic.guild_id == agent.guild_id,
                            ForumNotificationTopic.forum_channel_id == forum_id,
                            ForumNotificationTopic.topic_name.in_(topics_to_remove)
                        )
                    )
                    await self.session.execute(remove_stmt)
                
                # Add new topics
                topics_to_add = set(topic_names) - existing_topic_names
                for i, topic_name in enumerate(topic_names):
                    if topic_name in topics_to_add:
                        new_topic = ForumNotificationTopic(
                            guild_id=agent.guild_id,
                            forum_channel_id=forum_id,
                            topic_name=topic_name,
                            topic_description=descriptions[i] if i < len(descriptions) else ""
                        )
                        self.session.add(new_topic)
                
                # Update descriptions for existing topics
                for i, topic_name in enumerate(topic_names):
                    if topic_name not in topics_to_add:
                        # Update existing topic description
                        update_stmt = update(ForumNotificationTopic).where(
                            and_(
                                ForumNotificationTopic.guild_id == agent.guild_id,
                                ForumNotificationTopic.forum_channel_id == forum_id,
                                ForumNotificationTopic.topic_name == topic_name
                            )
                        ).values(
                            topic_description=descriptions[i] if i < len(descriptions) else ""
                        )
                        await self.session.execute(update_stmt)
            
            # Note: We don't commit here - let the calling function handle it
            
        except Exception as e:
            logger.error(f"Error syncing notification topics for agent {agent.id}: {e}")
            raise DatabaseOperationError(f"Failed to sync notification topics: {e}") from e



class QuestOperations:
    """Database operations for quest management system."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_quest_by_id(
            self,
            quest_id: UUID,
            guild_id: Optional[str] = None,
    ) -> Optional[Quest]:
        try:
            query = select(Quest).where(Quest.id == quest_id)

            if guild_id is not None:
                query = query.where(Quest.guild_id == guild_id)

            result = await self.session.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            raise DatabaseOperationError(
                f"Failed to get quest: {e}"
            ) from e

    async def get_daily_quest_by_id(self, daily_quest_id: UUID, guild_id: str) -> Optional[DailyQuest]:
        try:
            query = select(DailyQuest).where(DailyQuest.id == daily_quest_id)

            if guild_id is not None:
                query = query.where(DailyQuest.guild_id == guild_id)

            result = await self.session.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            raise DatabaseOperationError(
                f"Failed to get daily quest: {e}"
            ) from e

    async def get_upcoming_daily_quests(
            self,
            window_end: datetime,
    ) -> list[DailyQuest]:
        """
        Daily quests that will become active soon and are not announced yet.
        """
        try:
            now = datetime.now(timezone.utc)

            query = (
                select(DailyQuest)
                .join(DailyQuest.quest)
                .where(
                    DailyQuest.is_announced.is_(False),
                    DailyQuest.active_date >= now.date(),
                    DailyQuest.active_date <= window_end.date(),
                )
            )

            result = await self.session.execute(query)
            return list(result.scalars().all())

        except Exception as e:
            raise DatabaseOperationError(
                f"Failed to get upcoming daily quests: {e}"
            ) from e

    async def get_pending_daily_quests(self) -> list[DailyQuest]:
        """
        Daily quests that should already be announced but aren't.
        """
        try:
            today = datetime.now(timezone.utc).date()

            query = (
                select(DailyQuest)
                .where(
                    DailyQuest.is_announced.is_(False),
                    DailyQuest.active_date <= today,
                )
            )

            result = await self.session.execute(query)
            return list(result.scalars().all())

        except Exception as e:
            raise DatabaseOperationError(
                f"Failed to get pending daily quests: {e}"
            ) from e

    async def mark_daily_quest_announced(self, daily_quest_id: UUID) -> bool:
        try:
            now = datetime.now(timezone.utc)

            result = await self.session.execute(
                update(DailyQuest)
                .where(DailyQuest.id == daily_quest_id)
                .values(
                    is_announced=True,
                    announced_at=now,
                )
            )

            await self.session.commit()
            return result.rowcount > 0

        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(
                f"Failed to mark daily quest announced: {e}"
            ) from e

    async def mark_daily_quest_active(self, daily_quest_id: UUID) -> bool:
        try:
            result = await self.session.execute(
                update(DailyQuest)
                .where(DailyQuest.id == daily_quest_id)
                .values(is_active=True)
            )

            await self.session.commit()
            return result.rowcount > 0

        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(
                f"Failed to activate daily quest: {e}"
            ) from e

    async def get_daily_quest(
            self,
            active_date: date,
            guild_id: str,
    ) -> Optional[DailyQuest]:
        try:
            now = datetime.now(timezone.utc)
            logger.info("Now is=%s", now)

            query = (
                select(DailyQuest)
                .join(DailyQuest.quest)
                .where(
                    DailyQuest.active_date == active_date,
                    DailyQuest.guild_id == guild_id,
                    DailyQuest.is_active.is_(True),
                    DailyQuest.expires_at > now,
                )
            )

            logger.info(
                "DailyQuest query inputs | guild_id=%s active_date=%s now=%s",
                guild_id,
                active_date,
                now,
            )

            result = await self.session.execute(query)
            return result.scalar_one_or_none()

        except Exception as e:
            raise DatabaseOperationError(
                f"Failed to get daily quest: {e}"
            ) from e

    async def create_quest(
            self,
            *,
            guild_id: str,
            title: str,
            prompt: str,
            quest_type: str = "daily",
            python_script: Optional[str] = None,
            input_generator_script: Optional[str] = None,
            solution_validator_script: Optional[str] = None,
    ) -> Quest:
        try:
            quest = Quest(
                guild_id=guild_id,
                title=title,
                prompt=prompt,
                quest_type=quest_type,
                python_script=python_script,
                input_generator_script=input_generator_script,
                solution_validator_script=solution_validator_script,
            )

            self.session.add(quest)
            await self.session.commit()
            await self.session.refresh(quest)

            return quest

        except IntegrityError as e:
            await self.session.rollback()
            raise DatabaseOperationError(
                f"Quest integrity error: {e}"
            ) from e

        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(
                f"Failed to create quest: {e}"
            ) from e


class QuestInputOperations:
    """Database operations for daily quest input generation.
    Exactly one input is generated per DailyQuest and shared by all users.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_or_create_input(
        self,
        daily_quest_id: UUID,
        script: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        Get existing input for a daily quest or generate it once.

        Returns:
            (input_data, result_data)
        """
        try:
            # 1. Check if input already exists
            existing = await self.get_input(daily_quest_id)
            if existing:
                return existing.input_data, existing.result_data

            # 2. Generate input if missing
            if not script:
                raise DatabaseOperationError(
                    "No input_generator_script provided for quest"
                )

            input_data, result_data = await self._execute_script(script)

            quest_input = QuestInput(
                daily_quest_id=daily_quest_id,
                input_data=input_data,
                result_data=result_data,
            )

            self.session.add(quest_input)
            await self.session.commit()

            return input_data, result_data

        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(
                f"Failed to get or create quest input: {e}"
            ) from e

    async def get_input(
        self,
        daily_quest_id: UUID,
    ) -> Optional[QuestInput]:
        """Fetch existing input without generating it."""
        try:
            result = await self.session.execute(
                select(QuestInput).where(
                    QuestInput.daily_quest_id == daily_quest_id
                )
            )
            return result.scalar_one_or_none()

        except Exception as e:
            raise DatabaseOperationError(
                f"Failed to get quest input: {e}"
            ) from e

    async def _execute_script(self, script: str) -> tuple[str, str]:
        import json
        import io
        from contextlib import redirect_stdout

        try:
            buf = io.StringIO()
            exec_globals = {"__builtins__": __builtins__}

            with redirect_stdout(buf):
                exec(script, exec_globals)

            raw = buf.getvalue().strip()

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ScriptExecutionError(
                    f"Script output is not valid JSON: {e}"
                )

            if not isinstance(payload, dict):
                raise ScriptExecutionError("Script output must be a JSON object")

            if "input" not in payload or "result" not in payload:
                raise ScriptExecutionError(
                    "Script output must contain 'input' and 'result'"
                )

            return str(payload["input"]), str(payload["result"])

        except ScriptExecutionError:
            raise
        except Exception as e:
            raise ScriptExecutionError(
                f"Script execution failed: {e}"
            ) from e


class QuestSubmissionOperations:
    """Competitive submission handling for daily quests."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def submit_solution(
        self,
        daily_quest_id: UUID,
        guild_id: str,
        squad_id: UUID,
        user_id: str,
        username: str,
        submitted_solution: str,
    ) -> tuple[bool, bool, Optional[int]]:
        """
        Returns:
            (is_correct, is_first_success, points_earned)
        """
        try:
            # 1. Fetch expected result
            input_ops = QuestInputOperations(self.session)
            quest_input = await input_ops.get_input(daily_quest_id)

            if not quest_input:
                raise ValueError("Quest input not generated")

            expected = quest_input.result_data.strip()
            submitted = submitted_solution.strip()
            is_correct = expected == submitted

            is_first_success = False
            points_earned = None

            if is_correct:
                existing = await self._get_first_success_for_squad(
                    daily_quest_id,
                    squad_id,
                )

                is_first_success = existing is None

                # Only add points on first submission
                if is_first_success:
                    points_earned = self._calculate_points()

            submission = QuestSubmission(
                daily_quest_id=daily_quest_id,
                guild_id=guild_id,
                squad_id=squad_id,
                user_id=user_id,
                username=username,
                submitted_solution=submitted_solution,
                is_correct=is_correct,
                is_first_success=is_first_success,
                points_earned=points_earned,
            )

            self.session.add(submission)
            await self.session.commit()

            return is_correct, is_first_success, points_earned

        except Exception as e:
            await self.session.rollback()
            if isinstance(e, ValueError):
                raise
            raise DatabaseOperationError(
                f"Failed to submit quest solution: {e}"
            ) from e

    async def _get_first_success_for_squad(
        self,
        daily_quest_id: UUID,
        squad_id: UUID,
    ) -> Optional[QuestSubmission]:
        result = await self.session.execute(
            select(QuestSubmission).where(
                QuestSubmission.daily_quest_id == daily_quest_id,
                QuestSubmission.squad_id == squad_id,
                QuestSubmission.is_first_success.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def get_daily_quest_scoreboard(
            self,
            daily_quest_id: UUID,
    ) -> list[dict]:
        """
        Get squad rankings for a single daily quest.

        Returns:
            [
              {
                "squad_id": ...,
                "squad_name": ...,
                "points": ...,
                "winner_user_id": ...,
                "winner_username": ...
              },
              ...
            ]
        """
        try:
            query = (
                select(
                    Squad.id.label("squad_id"),
                    Squad.name.label("squad_name"),
                    QuestSubmission.points_earned.label("points"),
                    QuestSubmission.user_id.label("winner_user_id"),
                    QuestSubmission.username.label("winner_username"),
                )
                .select_from(QuestSubmission)
                .join(Squad, QuestSubmission.squad_id == Squad.id)
                .where(
                    QuestSubmission.daily_quest_id == daily_quest_id,
                    QuestSubmission.is_first_success.is_(True),
                )
                .order_by(desc(QuestSubmission.points_earned))
            )

            result = await self.session.execute(query)
            rows = result.fetchall()

            return [
                {
                    "squad_id": row.squad_id,
                    "squad_name": row.squad_name,
                    "points": row.points or 0,
                    "winner_user_id": row.winner_user_id,
                    "winner_username": row.winner_username,
                }
                for row in rows
            ]

        except Exception as e:
            raise DatabaseOperationError(
                f"Failed to get daily quest scoreboard: {e}"
            ) from e

    def _calculate_points(self) -> int:
        """Simple, deterministic quest scoring."""
        return 100


class CampaignOperations:
    """Database operations for campaign management system.
    
    Handles all campaign-related database operations including campaign creation,
    challenge management, and queries. Follows SOLID principles for clean
    separation of concerns.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize with database session.
        
        Args:
            session: SQLAlchemy async session
        """
        self.session = session
    
    async def create_campaign(
        self,
        guild_id: str,
        title: str,
        description: str,
        start_time: datetime,
        release_cadence_hours: int,
        announcement_channels: List[str],
        created_by: str,
        scheduled_message_title: Optional[str] = None,
        scheduled_message_description: Optional[str] = None,
        scheduled_message_time: Optional[datetime] = None
    ) -> Campaign:
        """Create a new campaign.
        
        Args:
            guild_id: Discord guild ID
            title: Campaign title
            description: Campaign description
            start_time: When campaign starts
            release_cadence_hours: Hours between challenge releases
            announcement_channels: List of Discord channel IDs
            created_by: Admin who created the campaign
            scheduled_message_title: Optional title for scheduled message
            scheduled_message_description: Optional description for scheduled message
            scheduled_message_time: Optional datetime when to send scheduled message
            
        Returns:
            Created campaign
            
        Raises:
            ConflictError: If campaign title already exists in guild
            DatabaseOperationError: For database errors
        """
        try:
            campaign = Campaign(
                guild_id=guild_id,
                title=title,
                description=description,
                start_time=start_time,
                release_cadence_hours=release_cadence_hours,
                announcement_channels=announcement_channels,
                created_by=created_by
            )
            
            self.session.add(campaign)
            await self.session.commit()
            await self.session.refresh(campaign)
            
            # Create scheduled message if provided
            if scheduled_message_time and scheduled_message_title:
                from .models import ScheduledMessage
                scheduled_message = ScheduledMessage(
                    campaign_id=campaign.id,
                    title=scheduled_message_title,
                    description=scheduled_message_description or "",
                    scheduled_time=scheduled_message_time,
                    created_by=created_by
                )
                self.session.add(scheduled_message)
                await self.session.commit()
            
            return campaign
            
        except IntegrityError as e:
            await self.session.rollback()
            if "uq_campaigns_guild_title" in str(e):
                raise ConflictError(f"Campaign with title '{title}' already exists in this guild")
            raise DatabaseOperationError(f"Failed to create campaign: {e}") from e
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to create campaign: {e}") from e
    
    async def get_campaigns_by_guild(
        self,
        guild_id: str,
        active_only: bool = False,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> Tuple[List[Campaign], int]:
        """Get campaigns for a guild with optional filtering.
        
        Args:
            guild_id: Discord guild ID
            active_only: Whether to filter to active campaigns only
            limit: Maximum number of campaigns to return
            offset: Number of campaigns to skip
            
        Returns:
            Tuple of (campaigns, total_count)
        """
        try:
            # Build base query with eager loading
            query = select(Campaign).options(selectinload(Campaign.challenges)).where(Campaign.guild_id == guild_id)
            
            if active_only:
                query = query.where(Campaign.is_active == True)
            
            # Get total count
            count_query = select(func.count(Campaign.id)).where(Campaign.guild_id == guild_id)
            if active_only:
                count_query = count_query.where(Campaign.is_active == True)
            
            total_result = await self.session.execute(count_query)
            total_count = total_result.scalar() or 0
            
            # Apply ordering, limit, and offset
            query = query.order_by(desc(Campaign.created_at))
            
            if limit is not None:
                query = query.limit(limit)
            if offset > 0:
                query = query.offset(offset)
            
            result = await self.session.execute(query)
            campaigns = result.scalars().all()
            
            return list(campaigns), total_count
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get campaigns: {e}") from e
    
    async def get_campaign_by_id(self, campaign_id: UUID, guild_id: Optional[str] = None) -> Optional[Campaign]:
        """Get a campaign by its ID.
        
        Args:
            campaign_id: Campaign UUID
            guild_id: Optional guild ID to verify ownership
            
        Returns:
            Campaign if found, None otherwise
        """
        try:
            query = select(Campaign).options(selectinload(Campaign.challenges)).where(Campaign.id == campaign_id)
            
            if guild_id is not None:
                query = query.where(Campaign.guild_id == guild_id)
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get campaign: {e}") from e
    
    async def update_campaign(
        self,
        campaign_id: UUID,
        guild_id: str,
        **updates
    ) -> Optional[Campaign]:
        """Update a campaign.
        
        Args:
            campaign_id: Campaign UUID
            guild_id: Discord guild ID for verification
            **updates: Fields to update
            
        Returns:
            Updated campaign if found, None otherwise
            
        Raises:
            ConflictError: If title conflict occurs
            DatabaseOperationError: For database errors
        """
        try:
            # Get existing campaign
            campaign = await self.get_campaign_by_id(campaign_id, guild_id)
            if not campaign:
                return None
            
            # Update fields
            for field, value in updates.items():
                if hasattr(campaign, field):
                    setattr(campaign, field, value)
            
            campaign.updated_at = datetime.now(timezone.utc)
            
            await self.session.commit()
            await self.session.refresh(campaign)
            
            return campaign
            
        except IntegrityError as e:
            await self.session.rollback()
            if "uq_campaigns_guild_title" in str(e):
                raise ConflictError(f"Campaign with this title already exists in the guild")
            raise DatabaseOperationError(f"Failed to update campaign: {e}") from e
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to update campaign: {e}") from e
    
    async def delete_campaign(self, campaign_id: UUID, guild_id: str) -> bool:
        """Soft delete a campaign (set is_active = False).
        
        Args:
            campaign_id: Campaign UUID
            guild_id: Discord guild ID for verification
            
        Returns:
            True if campaign was found and deactivated, False otherwise
        """
        try:
            campaign = await self.get_campaign_by_id(campaign_id, guild_id)
            if not campaign:
                return False
            
            campaign.is_active = False
            campaign.updated_at = datetime.now(timezone.utc)
            
            await self.session.commit()
            return True
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to delete campaign: {e}") from e
    
    async def get_campaign_with_challenges(self, campaign_id: UUID, guild_id: Optional[str] = None) -> Optional[Campaign]:
        """Get a campaign with all its challenges loaded.
        
        Args:
            campaign_id: Campaign UUID
            guild_id: Optional guild ID to verify ownership
            
        Returns:
            Campaign with challenges if found, None otherwise
        """
        try:
            query = select(Campaign).where(Campaign.id == campaign_id)
            
            if guild_id is not None:
                query = query.where(Campaign.guild_id == guild_id)
            
            # Use selectinload to eager load challenges with ordering
            query = query.options(
                selectinload(Campaign.challenges)
            )
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get campaign with challenges: {e}") from e
    
    async def create_challenge(
        self,
        campaign_id: UUID,
        title: str,
        description: str,
        order_position: int,
        points_value: int = 100,
        python_script: Optional[str] = None,
        input_generator_script: Optional[str] = None,
        solution_validator_script: Optional[str] = None
    ) -> Challenge:
        """Create a new challenge in a campaign.
        
        Args:
            campaign_id: Campaign UUID
            title: Challenge title
            description: Challenge description
            order_position: Position in campaign (1-based)
            points_value: Base points for completion
            input_generator_script: Python script to generate inputs
            solution_validator_script: Python script to validate solutions
            
        Returns:
            Created challenge
            
        Raises:
            ConflictError: If position already exists in campaign
            DatabaseOperationError: For database errors
        """
        try:
            challenge = Challenge(
                campaign_id=campaign_id,
                title=title,
                description=description,
                order_position=order_position,
                points_value=points_value,
                python_script=python_script,
                input_generator_script=input_generator_script,
                solution_validator_script=solution_validator_script
            )
            
            self.session.add(challenge)
            await self.session.commit()
            await self.session.refresh(challenge)
            
            return challenge
            
        except IntegrityError as e:
            await self.session.rollback()
            if "uq_challenges_campaign_position" in str(e):
                raise ConflictError(f"Challenge position {order_position} already exists in this campaign")
            raise DatabaseOperationError(f"Failed to create challenge: {e}") from e
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to create challenge: {e}") from e
    
    async def get_challenge_by_id(self, challenge_id: UUID) -> Optional[Challenge]:
        """Get a challenge by its ID.
        
        Args:
            challenge_id: Challenge UUID
            
        Returns:
            Challenge if found, None otherwise
        """
        try:
            result = await self.session.execute(
                select(Challenge).where(Challenge.id == challenge_id)
            )
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get challenge: {e}") from e
    
    async def get_challenges_by_campaign(
        self,
        campaign_id: UUID,
        released_only: bool = False
    ) -> List[Challenge]:
        """Get all challenges for a campaign.
        
        Args:
            campaign_id: Campaign UUID
            released_only: Whether to only return released challenges
            
        Returns:
            List of challenges ordered by position
        """
        try:
            query = select(Challenge).where(Challenge.campaign_id == campaign_id)
            
            if released_only:
                query = query.where(Challenge.is_released == True)
            
            query = query.order_by(Challenge.order_position)
            
            result = await self.session.execute(query)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get challenges: {e}") from e
    
    async def get_pending_announcements(self) -> List[Challenge]:
        """Get challenges that should be announced but haven't been yet.
        
        Returns:
            List of challenges ready for announcement
        """
        try:
            # Find challenges that:
            # 1. Haven't been announced yet (is_announced = False)
            # 2. Are part of active campaigns
            # Then filter by release time in Python
            
            now = datetime.now(timezone.utc)
            
            query = select(Challenge).join(Campaign).where(
                and_(
                    Challenge.is_announced == False,
                    Campaign.is_active == True
                )
            ).options(selectinload(Challenge.campaign))
            
            result = await self.session.execute(query)
            all_pending = list(result.scalars().all())
            
            # Filter by release time in Python
            ready_challenges = []
            for challenge in all_pending:
                campaign = challenge.campaign
                release_time = challenge.calculate_release_time(campaign.start_time, campaign.release_cadence_hours)
                if now >= release_time:
                    ready_challenges.append(challenge)
            
            return ready_challenges
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get pending announcements: {e}") from e
    
    async def get_upcoming_announcements(self, upcoming_time: datetime) -> List[Challenge]:
        """Get challenges that will be announced soon.
        
        Args:
            upcoming_time: Time limit to check up to
            
        Returns:
            List of challenges that will be announced soon
        """
        try:
            # Find challenges that:
            # 1. Haven't been announced yet (is_announced = False)
            # 2. Are part of active campaigns
            # Then filter by release time in Python
            
            now = datetime.now(timezone.utc)
            
            query = select(Challenge).join(Campaign).where(
                and_(
                    Challenge.is_announced == False,
                    Campaign.is_active == True
                )
            ).options(selectinload(Challenge.campaign))
            
            result = await self.session.execute(query)
            all_pending = list(result.scalars().all())
            
            # Filter by release time in Python for upcoming window
            upcoming_challenges = []
            for challenge in all_pending:
                campaign = challenge.campaign
                release_time = challenge.calculate_release_time(campaign.start_time, campaign.release_cadence_hours)
                if now < release_time <= upcoming_time:
                    upcoming_challenges.append(challenge)
            
            return upcoming_challenges
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get upcoming announcements: {e}") from e
    
    async def mark_challenge_announced(self, challenge_id: UUID) -> bool:
        """Mark a challenge as announced.
        
        Args:
            challenge_id: Challenge UUID
            
        Returns:
            True if marked successfully, False if challenge not found
        """
        try:
            now = datetime.now(timezone.utc)
            
            query = update(Challenge).where(
                Challenge.id == challenge_id
            ).values(
                is_announced=True,
                announced_at=now
            )
            
            result = await self.session.execute(query)
            await self.session.commit()
            
            return result.rowcount > 0
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to mark challenge as announced: {e}") from e
    
    async def mark_challenge_released(self, challenge_id: UUID) -> bool:
        """Mark a challenge as released.
        
        Args:
            challenge_id: Challenge UUID
            
        Returns:
            True if marked successfully, False if challenge not found
        """
        try:
            now = datetime.now(timezone.utc)
            
            query = update(Challenge).where(
                Challenge.id == challenge_id
            ).values(
                is_released=True,
                released_at=now
            )
            
            result = await self.session.execute(query)
            await self.session.commit()
            
            return result.rowcount > 0
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to mark challenge as released: {e}") from e
    
    async def get_challenge_with_campaign(self, challenge_id: UUID) -> Optional[Challenge]:
        """Get a challenge with its campaign data loaded.
        
        Args:
            challenge_id: Challenge UUID
            
        Returns:
            Challenge with campaign if found, None otherwise
        """
        try:
            query = select(Challenge).where(
                Challenge.id == challenge_id
            ).options(selectinload(Challenge.campaign))
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get challenge with campaign: {e}") from e
    
    async def get_most_recent_campaign(self, guild_id: str) -> Optional[Campaign]:
        """Get the most recently begun campaign for a guild.
        
        Prioritizes active campaigns over inactive ones.
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            Most recent campaign if found, None otherwise
        """
        try:
            # First try to get an active campaign
            query = select(Campaign).where(
                and_(
                    Campaign.guild_id == guild_id,
                    Campaign.start_time <= datetime.now(timezone.utc),
                    Campaign.is_active.is_(True)
                )
            ).order_by(desc(Campaign.start_time)).limit(1)
            
            result = await self.session.execute(query)
            active_campaign = result.scalar_one_or_none()
            
            if active_campaign:
                return active_campaign
            
            # If no active campaign, fall back to most recent started campaign
            query = select(Campaign).where(
                and_(
                    Campaign.guild_id == guild_id,
                    Campaign.start_time <= datetime.now(timezone.utc)
                )
            ).order_by(desc(Campaign.start_time)).limit(1)
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get most recent campaign: {e}") from e
    
    async def get_campaign_challenge_count(self, campaign_id: UUID) -> int:
        """Get the total number of challenges in a campaign.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            Number of challenges in the campaign
        """
        try:
            query = select(func.count(Challenge.id)).where(Challenge.campaign_id == campaign_id)
            result = await self.session.execute(query)
            return result.scalar() or 0
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get campaign challenge count: {e}") from e


class ChallengeInputOperations:
    """Database operations for challenge input management system.
    
    Handles squad-specific challenge input generation, storage, and retrieval.
    Ensures all squad members receive the same input data for fairness.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize with database session.
        
        Args:
            session: SQLAlchemy async session
        """
        self.session = session
    
    async def get_or_create_input(
        self, 
        challenge_id: UUID, 
        squad_id: UUID, 
        script: Optional[str] = None
    ) -> tuple[str, str]:
        """Get existing input for squad or generate new one if not exists.
        
        Args:
            challenge_id: UUID of the challenge
            squad_id: UUID of the squad
            script: Python script to execute for generating input (if needed)
            
        Returns:
            Tuple of (input_data, result_data)
            
        Raises:
            DatabaseOperationError: If database operation fails
            ScriptExecutionError: If script execution fails
        """
        try:
            # First, try to get existing input
            existing_input = await self.get_input_by_squad(challenge_id, squad_id)
            if existing_input:
                return existing_input.input_data, existing_input.result_data
            
            # If no existing input, generate new one
            if not script:
                raise DatabaseOperationError("No script provided for input generation")
            
            input_data, result_data = await self._execute_script(script)
            
            # Store the generated input in database
            challenge_input = ChallengeInput(
                challenge_id=challenge_id,
                squad_id=squad_id,
                input_data=input_data,
                result_data=result_data
            )
            
            self.session.add(challenge_input)
            await self.session.commit()
            
            return input_data, result_data
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to get or create challenge input: {e}") from e
    
    async def get_input_by_squad(
        self, 
        challenge_id: UUID, 
        squad_id: UUID
    ) -> Optional["ChallengeInput"]:
        """Get existing challenge input for a specific squad.
        
        Args:
            challenge_id: UUID of the challenge
            squad_id: UUID of the squad
            
        Returns:
            ChallengeInput object or None if not found
            
        Raises:
            DatabaseOperationError: If database operation fails
        """
        try:
            query = select(ChallengeInput).where(
                ChallengeInput.challenge_id == challenge_id,
                ChallengeInput.squad_id == squad_id
            )
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get challenge input by squad: {e}") from e
    
    async def get_existing_input(
        self, 
        challenge_id: UUID, 
        squad_id: UUID
    ) -> Optional["ChallengeInput"]:
        """Check if challenge input exists for a squad without generating it.
        
        This is an alias for get_input_by_squad() to make the intent clear
        when checking existence without side effects.
        
        Args:
            challenge_id: UUID of the challenge
            squad_id: UUID of the squad
            
        Returns:
            ChallengeInput object or None if not found
        """
        return await self.get_input_by_squad(challenge_id, squad_id)
    
    async def _execute_script(self, script: str) -> tuple[str, str]:
        """Execute Python script to generate input and result data.
        
        Args:
            script: Python script code to execute
            
        Returns:
            Tuple of (input_data, result_data)
            
        Raises:
            ScriptExecutionError: If script execution fails or output is invalid
        """
        import json
        import io
        from contextlib import redirect_stdout
        
        try:
            # Capture stdout
            captured_output = io.StringIO()
            
            # Allow unrestricted script execution with full Python capabilities
            # These scripts are trusted and need access to arbitrary imports
            exec_globals = {'__builtins__': __builtins__}
            
            # Execute the script with captured stdout and full Python environment
            with redirect_stdout(captured_output):
                exec(script, exec_globals)
            
            # Get the output
            output = captured_output.getvalue().strip()
            
            # Parse JSON output
            try:
                output_data = json.loads(output)
            except json.JSONDecodeError as e:
                raise ScriptExecutionError(f"Script output is not valid JSON: {e}")
            
            # Validate required keys
            if not isinstance(output_data, dict):
                raise ScriptExecutionError("Script output must be a JSON object")
            
            if "input" not in output_data or "result" not in output_data:
                raise ScriptExecutionError("Script output must contain 'input' and 'result' keys")
            
            # Convert values to strings for database storage
            input_data = str(output_data["input"])
            result_data = str(output_data["result"])
            
            return input_data, result_data
            
        except Exception as e:
            if isinstance(e, ScriptExecutionError):
                raise
            raise ScriptExecutionError(f"Script execution failed: {e}") from e


class ScriptExecutionError(Exception):
    """Exception raised when script execution fails."""
    pass


class ChallengeSubmissionOperations:
    """Database operations for challenge solution submission and success tracking.
    
    Handles solution submission, comparison with expected results, and tracking
    of first successful submissions per squad.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize with database session.
        
        Args:
            session: SQLAlchemy async session
        """
        self.session = session
    
    async def submit_solution(
        self,
        challenge_id: UUID,
        squad_id: UUID,
        user_id: str,
        username: str,
        submitted_solution: str
    ) -> tuple[bool, bool, Optional[int]]:
        """Submit a solution and check if it matches the expected result.
        
        Args:
            challenge_id: UUID of the challenge
            squad_id: UUID of the squad
            user_id: Discord user ID of the submitter
            username: Username for audit purposes
            submitted_solution: The solution text submitted by the user
            
        Returns:
            Tuple of (is_correct, is_first_success, points_earned)
            
        Raises:
            DatabaseOperationError: If operation fails
            ValueError: If no expected result exists for this challenge/squad
        """
        try:
            # Get the expected result from ChallengeInput
            input_ops = ChallengeInputOperations(self.session)
            challenge_input = await input_ops.get_input_by_squad(challenge_id, squad_id)
            
            if not challenge_input:
                raise ValueError("No input/result data found for this challenge and squad. Generate input first.")
            
            # Compare submitted solution with expected result
            expected_result = challenge_input.result_data.strip()
            submitted_solution_clean = submitted_solution.strip()
            is_correct = expected_result == submitted_solution_clean
            
            # Check if this squad already has a successful submission
            is_first_success = False
            points_earned = None
            
            if is_correct:
                existing_success = await self._get_first_success_for_squad(challenge_id, squad_id)
                is_first_success = existing_success is None
                
                # Calculate points for first successful submission
                if is_first_success:
                    points_earned = await self._calculate_points(challenge_id, challenge_input.created_at)
            
            # Create submission record
            submission = ChallengeSubmission(
                challenge_id=challenge_id,
                squad_id=squad_id,
                user_id=user_id,
                username=username,
                submitted_solution=submitted_solution,
                is_correct=is_correct,
                is_first_success=is_first_success,
                points_earned=points_earned
            )
            
            self.session.add(submission)
            await self.session.commit()
            
            return is_correct, is_first_success, points_earned
            
        except Exception as e:
            await self.session.rollback()
            if isinstance(e, ValueError):
                raise
            raise DatabaseOperationError(f"Failed to submit solution: {e}") from e
    
    async def _get_first_success_for_squad(
        self, 
        challenge_id: UUID, 
        squad_id: UUID
    ) -> Optional["ChallengeSubmission"]:
        """Get the first successful submission for a squad/challenge combination.
        
        Args:
            challenge_id: UUID of the challenge
            squad_id: UUID of the squad
            
        Returns:
            ChallengeSubmission object or None if no successful submission exists
        """
        try:
            query = select(ChallengeSubmission).where(
                ChallengeSubmission.challenge_id == challenge_id,
                ChallengeSubmission.squad_id == squad_id,
                ChallengeSubmission.is_correct == True,
                ChallengeSubmission.is_first_success == True
            ).order_by(ChallengeSubmission.submitted_at.asc()).limit(1)
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get first success: {e}") from e
    
    async def get_squad_submissions(
        self, 
        challenge_id: UUID, 
        squad_id: UUID,
        limit: int = 10
    ) -> list["ChallengeSubmission"]:
        """Get recent submissions for a squad/challenge combination.
        
        Args:
            challenge_id: UUID of the challenge
            squad_id: UUID of the squad
            limit: Maximum number of submissions to return
            
        Returns:
            List of ChallengeSubmission objects
        """
        try:
            query = select(ChallengeSubmission).where(
                ChallengeSubmission.challenge_id == challenge_id,
                ChallengeSubmission.squad_id == squad_id
            ).order_by(ChallengeSubmission.submitted_at.desc()).limit(limit)
            
            result = await self.session.execute(query)
            return result.scalars().all()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get squad submissions: {e}") from e
    
    async def _calculate_points(self, challenge_id: UUID, input_generated_at: datetime) -> int:
        """Calculate points earned using the dual-phase scoring system.
        
        Uses a sophisticated scoring system with:
        - Fixed 4096 max points
        - Logarithmic decay for first 2 hours (if ≥3 hours remain)
        - Linear fractional reduction for remaining time
        
        Args:
            challenge_id: UUID of the challenge
            input_generated_at: When the input was generated (timer start)
            
        Returns:
            Points earned (0-4096)
        """
        from smarter_dev.web.scoring import calculate_challenge_points
        
        try:
            # Get the challenge and its campaign to calculate end time
            challenge_query = select(Challenge).options(
                selectinload(Challenge.campaign)
            ).where(Challenge.id == challenge_id)
            
            result = await self.session.execute(challenge_query)
            challenge = result.scalar_one_or_none()
            
            if not challenge or not challenge.campaign:
                return 0
            
            # Get all challenges in the campaign to count them
            campaign_challenges_query = select(func.count(Challenge.id)).where(
                Challenge.campaign_id == challenge.campaign_id
            )
            challenges_result = await self.session.execute(campaign_challenges_query)
            num_challenges = challenges_result.scalar() or 0
            
            if num_challenges == 0:
                return 0
            
            # Calculate when the campaign/challenge ends
            # End time = campaign start + (num_challenges * release_cadence)
            from datetime import timedelta
            total_duration = timedelta(hours=num_challenges * challenge.campaign.release_cadence_hours)
            challenge_end_time = challenge.campaign.start_time + total_duration
            
            # Get current time for submission
            submission_time = datetime.now(timezone.utc)
            
            # Use the new scoring system
            points = calculate_challenge_points(
                input_generated_at,
                submission_time,
                challenge_end_time
            )
            
            logger.info(
                f"New scoring calculation: challenge_id={challenge_id}, "
                f"input_at={input_generated_at}, submission_at={submission_time}, "
                f"end_at={challenge_end_time}, points={points}"
            )
            
            return points
            
        except Exception as e:
            logger.error(f"Failed to calculate points: {e}")
            return 0
    
    async def get_campaign_scoreboard(self, campaign_id: UUID) -> List[Dict[str, Any]]:
        """Get scoreboard data for a campaign with squad rankings by total points.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            List of dictionaries containing squad names, total points, and submission counts
        """
        try:
            # Query to get scoreboard data grouped by squad
            query = select(
                Squad.name.label("squad_name"),
                Squad.id.label("squad_id"),
                func.coalesce(func.sum(ChallengeSubmission.points_earned), 0).label("total_points"),
                func.count(ChallengeSubmission.id).filter(
                    ChallengeSubmission.is_first_success == True
                ).label("successful_submissions")
            ).select_from(
                Squad
            ).outerjoin(
                ChallengeSubmission, Squad.id == ChallengeSubmission.squad_id
            ).outerjoin(
                Challenge, ChallengeSubmission.challenge_id == Challenge.id
            ).where(
                Challenge.campaign_id == campaign_id
            ).group_by(
                Squad.id, Squad.name
            ).order_by(
                func.coalesce(func.sum(ChallengeSubmission.points_earned), 0).desc()
            )
            
            result = await self.session.execute(query)
            rows = result.fetchall()
            
            # Convert to list of dictionaries
            scoreboard = []
            for row in rows:
                scoreboard.append({
                    "squad_name": row.squad_name,
                    "squad_id": row.squad_id,
                    "total_points": row.total_points,
                    "successful_submissions": row.successful_submissions
                })
            
            return scoreboard
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get campaign scoreboard: {e}") from e
    
    async def get_detailed_campaign_scoreboard(self, campaign_id: UUID) -> Dict[str, Any]:
        """Get detailed scoreboard data organized by challenge.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            Dictionary with challenge breakdown and overall squad totals
        """
        try:
            # Get all successful submissions organized by challenge
            submissions_query = select(
                Challenge.title.label("challenge_title"),
                Challenge.id.label("challenge_id"),
                ChallengeSubmission.points_earned,
                Squad.name.label("squad_name"),
                Squad.id.label("squad_id")
            ).select_from(
                Challenge
            ).join(
                ChallengeSubmission, Challenge.id == ChallengeSubmission.challenge_id
            ).join(
                Squad, ChallengeSubmission.squad_id == Squad.id
            ).where(
                and_(
                    Challenge.campaign_id == campaign_id,
                    ChallengeSubmission.is_correct == True,
                    ChallengeSubmission.is_first_success == True
                )
            ).order_by(Challenge.title, Squad.name)
            
            result = await self.session.execute(submissions_query)
            submissions = result.fetchall()
            
            # Group data by challenge
            challenges_breakdown = {}
            squad_totals = {}
            
            for submission in submissions:
                challenge_title = submission.challenge_title
                squad_name = submission.squad_name
                squad_id = submission.squad_id
                points = submission.points_earned or 0
                
                # Track challenge breakdown
                if challenge_title not in challenges_breakdown:
                    challenges_breakdown[challenge_title] = []
                
                challenges_breakdown[challenge_title].append({
                    "squad_name": squad_name,
                    "squad_id": str(squad_id),
                    "points_earned": points
                })
                
                # Track squad totals
                if squad_id not in squad_totals:
                    squad_totals[squad_id] = {
                        "squad_name": squad_name,
                        "squad_id": str(squad_id),
                        "total_points": 0,
                        "challenges_completed": 0
                    }
                
                squad_totals[squad_id]["total_points"] += points
                squad_totals[squad_id]["challenges_completed"] += 1
            
            # Convert to lists and sort
            challenges_list = []
            for challenge_title, squads in challenges_breakdown.items():
                # Sort squads by points descending
                squads.sort(key=lambda x: x["points_earned"], reverse=True)
                challenges_list.append({
                    "challenge_title": challenge_title,
                    "submissions": squads
                })
            
            # Sort challenges alphabetically
            challenges_list.sort(key=lambda x: x["challenge_title"])
            
            # Convert squad totals to list and sort by total points
            squad_totals_list = list(squad_totals.values())
            squad_totals_list.sort(key=lambda x: x["total_points"], reverse=True)
            
            return {
                "challenges_breakdown": challenges_list,
                "squad_totals": squad_totals_list
            }
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get detailed campaign scoreboard: {e}") from e
    
    async def get_campaign_submission_count(self, campaign_id: UUID) -> int:
        """Get the total number of submissions for a campaign.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            Total number of submissions across all challenges in the campaign
        """
        try:
            query = select(func.count(ChallengeSubmission.id)).select_from(
                ChallengeSubmission
            ).join(
                Challenge, ChallengeSubmission.challenge_id == Challenge.id
            ).where(
                Challenge.campaign_id == campaign_id
            )
            
            result = await self.session.execute(query)
            return result.scalar() or 0
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get campaign submission count: {e}") from e


class ScheduledMessageOperations:
    """Database operations for scheduled message management system.
    
    Handles all scheduled message-related database operations including creation,
    management, and queries. Follows SOLID principles for clean separation of concerns.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize with database session.
        
        Args:
            session: SQLAlchemy async session
        """
        self.session = session
    
    async def create_scheduled_message(
        self,
        campaign_id: UUID,
        title: str,
        description: str,
        scheduled_time: datetime,
        created_by: str,
        announcement_channel_message: Optional[str] = None
    ) -> ScheduledMessage:
        """Create a new scheduled message for a campaign.
        
        Args:
            campaign_id: Campaign UUID
            title: Message title
            description: Message description (sent to squad channels)
            scheduled_time: When to send the message (UTC)
            created_by: Admin who created the scheduled message
            announcement_channel_message: Optional message for campaign channels
            
        Returns:
            Created scheduled message
            
        Raises:
            DatabaseOperationError: For database errors
        """
        try:
            scheduled_message = ScheduledMessage(
                campaign_id=campaign_id,
                title=title,
                description=description,
                announcement_channel_message=announcement_channel_message,
                scheduled_time=scheduled_time,
                created_by=created_by
            )
            
            self.session.add(scheduled_message)
            await self.session.commit()
            await self.session.refresh(scheduled_message)
            
            return scheduled_message
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to create scheduled message: {e}") from e
    
    async def get_scheduled_messages_by_campaign(
        self,
        campaign_id: UUID
    ) -> List[ScheduledMessage]:
        """Get all scheduled messages for a campaign.
        
        Args:
            campaign_id: Campaign UUID
            
        Returns:
            List of scheduled messages ordered by scheduled time
        """
        try:
            query = select(ScheduledMessage).where(
                ScheduledMessage.campaign_id == campaign_id
            ).order_by(ScheduledMessage.scheduled_time)
            
            result = await self.session.execute(query)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get scheduled messages: {e}") from e
    
    async def get_scheduled_message_by_id(
        self,
        message_id: UUID,
        campaign_id: Optional[UUID] = None
    ) -> Optional[ScheduledMessage]:
        """Get a scheduled message by its ID.
        
        Args:
            message_id: Scheduled message UUID
            campaign_id: Optional campaign ID to verify ownership
            
        Returns:
            Scheduled message if found, None otherwise
        """
        try:
            query = select(ScheduledMessage).where(ScheduledMessage.id == message_id)
            
            if campaign_id is not None:
                query = query.where(ScheduledMessage.campaign_id == campaign_id)
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get scheduled message: {e}") from e
    
    async def update_scheduled_message(
        self,
        message_id: UUID,
        campaign_id: UUID,
        **updates
    ) -> Optional[ScheduledMessage]:
        """Update a scheduled message.
        
        Args:
            message_id: Scheduled message UUID
            campaign_id: Campaign UUID for verification
            **updates: Fields to update
            
        Returns:
            Updated scheduled message if found, None otherwise
            
        Raises:
            DatabaseOperationError: For database errors
        """
        try:
            # Get existing scheduled message
            message = await self.get_scheduled_message_by_id(message_id, campaign_id)
            if not message:
                return None
            
            # Update fields
            for field, value in updates.items():
                if hasattr(message, field):
                    setattr(message, field, value)
            
            message.updated_at = datetime.now(timezone.utc)
            
            await self.session.commit()
            await self.session.refresh(message)
            
            return message
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to update scheduled message: {e}") from e
    
    async def delete_scheduled_message(
        self,
        message_id: UUID,
        campaign_id: UUID
    ) -> bool:
        """Delete a scheduled message.
        
        Args:
            message_id: Scheduled message UUID
            campaign_id: Campaign UUID for verification
            
        Returns:
            True if message was found and deleted, False otherwise
        """
        try:
            message = await self.get_scheduled_message_by_id(message_id, campaign_id)
            if not message:
                return False
            
            await self.session.delete(message)
            await self.session.commit()
            return True
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to delete scheduled message: {e}") from e
    
    async def get_pending_scheduled_messages(self) -> List[ScheduledMessage]:
        """Get scheduled messages that should be sent but haven't been yet.
        
        Returns:
            List of scheduled messages ready to be sent
        """
        try:
            now = datetime.now(timezone.utc)
            
            query = select(ScheduledMessage).join(Campaign).where(
                and_(
                    ScheduledMessage.is_sent == False,
                    ScheduledMessage.scheduled_time <= now,
                    Campaign.is_active == True
                )
            ).options(selectinload(ScheduledMessage.campaign))
            
            result = await self.session.execute(query)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get pending scheduled messages: {e}") from e
    
    async def get_upcoming_scheduled_messages(self, upcoming_time: datetime) -> List[ScheduledMessage]:
        """Get scheduled messages that will be sent soon.
        
        Args:
            upcoming_time: Time limit to check up to
            
        Returns:
            List of scheduled messages that will be sent soon
        """
        try:
            now = datetime.now(timezone.utc)
            
            query = select(ScheduledMessage).join(Campaign).where(
                and_(
                    ScheduledMessage.is_sent == False,
                    ScheduledMessage.scheduled_time > now,
                    ScheduledMessage.scheduled_time <= upcoming_time,
                    Campaign.is_active == True
                )
            ).options(selectinload(ScheduledMessage.campaign))
            
            result = await self.session.execute(query)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get upcoming scheduled messages: {e}") from e
    
    async def mark_scheduled_message_sent(self, message_id: UUID) -> bool:
        """Mark a scheduled message as sent.
        
        Args:
            message_id: Scheduled message UUID
            
        Returns:
            True if marked successfully, False if message not found
        """
        try:
            now = datetime.now(timezone.utc)
            
            query = update(ScheduledMessage).where(
                ScheduledMessage.id == message_id
            ).values(
                is_sent=True,
                sent_at=now
            )
            
            result = await self.session.execute(query)
            await self.session.commit()
            
            return result.rowcount > 0
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to mark scheduled message as sent: {e}") from e
    
    async def get_scheduled_message_with_campaign(self, message_id: UUID) -> Optional[ScheduledMessage]:
        """Get a scheduled message with its campaign data loaded.
        
        Args:
            message_id: Scheduled message UUID
            
        Returns:
            Scheduled message with campaign if found, None otherwise
        """
        try:
            query = select(ScheduledMessage).where(
                ScheduledMessage.id == message_id
            ).options(selectinload(ScheduledMessage.campaign))
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get scheduled message with campaign: {e}") from e


class SquadSaleEventOperations:
    """Database operations for squad sale event management system.
    
    Handles all squad sale event-related database operations including creation,
    management, and queries for time-based discount events.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize with database session.
        
        Args:
            session: SQLAlchemy async session
        """
        self.session = session
    
    async def create_sale_event(
        self,
        guild_id: str,
        name: str,
        description: str,
        start_time: datetime,
        duration_hours: int,
        join_discount_percent: int,
        switch_discount_percent: int,
        created_by: str
    ) -> SquadSaleEvent:
        """Create a new squad sale event.
        
        Args:
            guild_id: Discord guild ID
            name: Event name
            description: Event description
            start_time: When event starts (UTC)
            duration_hours: How long event lasts
            join_discount_percent: Discount percent for joining squads (0-100)
            switch_discount_percent: Discount percent for switching squads (0-100)
            created_by: Admin who created the event
            
        Returns:
            Created sale event
            
        Raises:
            ConflictError: If event name already exists in guild
            DatabaseOperationError: For database errors
        """
        try:
            sale_event = SquadSaleEvent(
                guild_id=guild_id,
                name=name,
                description=description,
                start_time=start_time,
                duration_hours=duration_hours,
                join_discount_percent=join_discount_percent,
                switch_discount_percent=switch_discount_percent,
                created_by=created_by
            )
            
            self.session.add(sale_event)
            await self.session.commit()
            await self.session.refresh(sale_event)
            
            return sale_event
            
        except IntegrityError as e:
            await self.session.rollback()
            if "uq_squad_sale_events_guild_name" in str(e):
                raise ConflictError(f"Sale event with name '{name}' already exists in this guild")
            raise DatabaseOperationError(f"Failed to create sale event: {e}") from e
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to create sale event: {e}") from e
    
    async def get_sale_events_by_guild(
        self,
        guild_id: str,
        active_only: bool = False,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> Tuple[List[SquadSaleEvent], int]:
        """Get sale events for a guild with optional filtering.
        
        Args:
            guild_id: Discord guild ID
            active_only: Whether to filter to active events only
            limit: Maximum number of events to return
            offset: Number of events to skip
            
        Returns:
            Tuple of (events, total_count)
        """
        try:
            # Build base query
            query = select(SquadSaleEvent).where(SquadSaleEvent.guild_id == guild_id)
            
            if active_only:
                query = query.where(SquadSaleEvent.is_active == True)
            
            # Get total count
            count_query = select(func.count(SquadSaleEvent.id)).where(SquadSaleEvent.guild_id == guild_id)
            if active_only:
                count_query = count_query.where(SquadSaleEvent.is_active == True)
            
            total_result = await self.session.execute(count_query)
            total_count = total_result.scalar() or 0
            
            # Apply ordering, limit, and offset
            query = query.order_by(desc(SquadSaleEvent.start_time))
            
            if limit is not None:
                query = query.limit(limit)
            if offset > 0:
                query = query.offset(offset)
            
            result = await self.session.execute(query)
            events = result.scalars().all()
            
            return list(events), total_count
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get sale events: {e}") from e
    
    async def get_sale_event_by_id(
        self,
        event_id: UUID,
        guild_id: Optional[str] = None
    ) -> Optional[SquadSaleEvent]:
        """Get a sale event by its ID.
        
        Args:
            event_id: Sale event UUID
            guild_id: Optional guild ID to verify ownership
            
        Returns:
            Sale event if found, None otherwise
        """
        try:
            query = select(SquadSaleEvent).where(SquadSaleEvent.id == event_id)
            
            if guild_id is not None:
                query = query.where(SquadSaleEvent.guild_id == guild_id)
            
            result = await self.session.execute(query)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get sale event: {e}") from e
    
    async def update_sale_event(
        self,
        event_id: UUID,
        guild_id: str,
        **updates
    ) -> Optional[SquadSaleEvent]:
        """Update a sale event.
        
        Args:
            event_id: Sale event UUID
            guild_id: Discord guild ID for verification
            **updates: Fields to update
            
        Returns:
            Updated sale event if found, None otherwise
            
        Raises:
            ConflictError: If name conflict occurs
            DatabaseOperationError: For database errors
        """
        try:
            # Get existing sale event
            event = await self.get_sale_event_by_id(event_id, guild_id)
            if not event:
                return None
            
            # Update fields
            for field, value in updates.items():
                if hasattr(event, field):
                    setattr(event, field, value)
            
            event.updated_at = datetime.now(timezone.utc)
            
            await self.session.commit()
            await self.session.refresh(event)
            
            return event
            
        except IntegrityError as e:
            await self.session.rollback()
            if "uq_squad_sale_events_guild_name" in str(e):
                raise ConflictError(f"Sale event with this name already exists in the guild")
            raise DatabaseOperationError(f"Failed to update sale event: {e}") from e
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to update sale event: {e}") from e
    
    async def delete_sale_event(self, event_id: UUID, guild_id: str) -> bool:
        """Delete a sale event.
        
        Args:
            event_id: Sale event UUID
            guild_id: Discord guild ID for verification
            
        Returns:
            True if event was found and deleted, False otherwise
        """
        try:
            event = await self.get_sale_event_by_id(event_id, guild_id)
            if not event:
                return False
            
            await self.session.delete(event)
            await self.session.commit()
            return True
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to delete sale event: {e}") from e
    
    async def get_active_sale_events(self, guild_id: str) -> List[SquadSaleEvent]:
        """Get currently active sale events for a guild.
        
        An event is active if:
        1. is_active = True
        2. Current time is between start_time and end_time
        
        Args:
            guild_id: Discord guild ID
            
        Returns:
            List of currently active sale events
        """
        try:
            now = datetime.now(timezone.utc)
            
            # Get all active events from database
            query = select(SquadSaleEvent).where(
                and_(
                    SquadSaleEvent.guild_id == guild_id,
                    SquadSaleEvent.is_active == True
                )
            ).order_by(SquadSaleEvent.start_time)
            
            result = await self.session.execute(query)
            all_active_events = list(result.scalars().all())
            
            # Filter by time in Python to ensure accuracy
            currently_active = []
            for event in all_active_events:
                if event.is_currently_active:
                    currently_active.append(event)
            
            return currently_active
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get active sale events: {e}") from e
    
    async def toggle_sale_event(self, event_id: UUID, guild_id: str) -> Optional[SquadSaleEvent]:
        """Toggle sale event active status.
        
        Args:
            event_id: Sale event UUID
            guild_id: Discord guild ID for verification
            
        Returns:
            Updated sale event if found, None otherwise
        """
        try:
            event = await self.get_sale_event_by_id(event_id, guild_id)
            if not event:
                return None
            
            event.is_active = not event.is_active
            event.updated_at = datetime.now(timezone.utc)
            
            await self.session.commit()
            await self.session.refresh(event)
            
            return event
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to toggle sale event: {e}") from e
    
    async def get_best_discount_for_action(
        self,
        guild_id: str,
        is_switch: bool
    ) -> Optional[int]:
        """Get the best available discount percentage for a specific action.
        
        Args:
            guild_id: Discord guild ID
            is_switch: True if this is a squad switch, False if first join
            
        Returns:
            Best discount percentage (0-100) or None if no active events
        """
        try:
            active_events = await self.get_active_sale_events(guild_id)
            
            if not active_events:
                return None
            
            # Find the highest discount for the specified action
            best_discount = 0
            for event in active_events:
                discount = event.switch_discount_percent if is_switch else event.join_discount_percent
                best_discount = max(best_discount, discount)
            
            return best_discount if best_discount > 0 else None
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get best discount: {e}") from e
    
    async def calculate_discounted_cost(
        self,
        guild_id: str,
        original_cost: int,
        is_switch: bool
    ) -> tuple[int, Optional[SquadSaleEvent]]:
        """Calculate discounted cost and return the event providing the best discount.
        
        Args:
            guild_id: Discord guild ID
            original_cost: Original cost before discount
            is_switch: True if this is a squad switch, False if first join
            
        Returns:
            Tuple of (discounted_cost, best_event_or_none)
        """
        try:
            active_events = await self.get_active_sale_events(guild_id)
            
            if not active_events:
                return original_cost, None
            
            # Find the event with the best discount for this action
            best_discount = 0
            best_event = None
            
            for event in active_events:
                discount = event.switch_discount_percent if is_switch else event.join_discount_percent
                if discount > best_discount:
                    best_discount = discount
                    best_event = event
            
            if best_discount == 0:
                return original_cost, None
            
            # Calculate discounted cost
            discount_amount = int(original_cost * best_discount / 100)
            discounted_cost = max(0, original_cost - discount_amount)
            
            return discounted_cost, best_event
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to calculate discounted cost: {e}") from e


class RepeatingMessageOperations:
    """Database operations for repeating message management system.
    
    Handles all repeating message-related database operations including creation,
    management, and queries. Follows SOLID principles for clean separation of concerns.
    """
    
    def __init__(self, session: AsyncSession):
        """Initialize with database session.
        
        Args:
            session: SQLAlchemy async session
        """
        self.session = session
    
    async def create_repeating_message(
        self,
        guild_id: str,
        channel_id: str,
        message_content: str,
        start_time: datetime,
        interval_minutes: int,
        created_by: str,
        role_id: Optional[str] = None
    ) -> RepeatingMessage:
        """Create a new repeating message.
        
        Args:
            guild_id: Discord guild ID
            channel_id: Discord channel ID where messages will be sent
            message_content: The message text to send repeatedly
            start_time: UTC datetime when the first message should be sent
            interval_minutes: Minutes between repeated messages
            created_by: Admin who created the repeating message
            role_id: Optional Discord role ID to mention
            
        Returns:
            Created repeating message
            
        Raises:
            DatabaseOperationError: For database errors
        """
        try:
            repeating_message = RepeatingMessage(
                guild_id=guild_id,
                channel_id=channel_id,
                message_content=message_content,
                role_id=role_id,
                start_time=start_time,
                interval_minutes=interval_minutes,
                created_by=created_by
            )
            
            self.session.add(repeating_message)
            await self.session.commit()
            await self.session.refresh(repeating_message)
            
            logger.info(f"Created repeating message {repeating_message.id} for guild {guild_id}")
            return repeating_message
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to create repeating message: {e}") from e
    
    async def get_guild_repeating_messages(
        self,
        guild_id: str,
        active_only: bool = False
    ) -> List[RepeatingMessage]:
        """Get all repeating messages for a guild.
        
        Args:
            guild_id: Discord guild ID
            active_only: If True, only return active messages
            
        Returns:
            List of repeating messages
        """
        try:
            stmt = select(RepeatingMessage).where(RepeatingMessage.guild_id == guild_id)
            
            if active_only:
                stmt = stmt.where(RepeatingMessage.is_active == True)
            
            stmt = stmt.order_by(RepeatingMessage.created_at.desc())
            result = await self.session.execute(stmt)
            return list(result.scalars().all())
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get guild repeating messages: {e}") from e
    
    async def get_repeating_message(self, message_id: UUID) -> Optional[RepeatingMessage]:
        """Get a repeating message by ID.
        
        Args:
            message_id: Repeating message UUID
            
        Returns:
            RepeatingMessage if found, None otherwise
        """
        try:
            stmt = select(RepeatingMessage).where(RepeatingMessage.id == message_id)
            result = await self.session.execute(stmt)
            return result.scalar_one_or_none()
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get repeating message: {e}") from e
    
    async def update_repeating_message(
        self,
        message_id: UUID,
        **updates
    ) -> bool:
        """Update a repeating message.
        
        Args:
            message_id: Repeating message UUID
            **updates: Fields to update
            
        Returns:
            True if message was updated, False if not found
        """
        try:
            # Add updated_at timestamp
            updates['updated_at'] = datetime.now(timezone.utc)
            
            stmt = (
                update(RepeatingMessage)
                .where(RepeatingMessage.id == message_id)
                .values(**updates)
            )
            result = await self.session.execute(stmt)
            await self.session.commit()
            
            return result.rowcount > 0
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to update repeating message: {e}") from e
    
    async def delete_repeating_message(self, message_id: UUID) -> bool:
        """Delete a repeating message.
        
        Args:
            message_id: Repeating message UUID
            
        Returns:
            True if message was deleted, False if not found
        """
        try:
            stmt = delete(RepeatingMessage).where(RepeatingMessage.id == message_id)
            result = await self.session.execute(stmt)
            await self.session.commit()
            
            return result.rowcount > 0
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to delete repeating message: {e}") from e
    
    async def get_due_repeating_messages(self) -> List[RepeatingMessage]:
        """Get repeating messages that are due to be sent.
        
        Rule: Only return messages that should be sent NOW. If we missed xx:11 
        and it's now xx:13, return the xx:13 message, not the old xx:11 one.
        
        Returns:
            List of repeating messages due for sending
        """
        try:
            from datetime import timedelta
            current_time = datetime.now(timezone.utc)
            
            # Get all active messages that have missed their send time
            stmt = select(RepeatingMessage).where(
                and_(
                    RepeatingMessage.is_active == True,
                    RepeatingMessage.next_send_time <= current_time
                )
            )
            
            result = await self.session.execute(stmt)
            all_overdue_messages = list(result.scalars().all())
            
            # For each overdue message, calculate what time it should send NOW
            # and only return if it matches the current schedule
            due_now_messages = []
            
            for message in all_overdue_messages:
                # Calculate the current expected send time based on original schedule
                expected_send_time = message.next_send_time
                while expected_send_time < current_time:
                    expected_send_time += timedelta(minutes=message.interval_minutes)
                
                # Only include if the current expected time is due now
                # Allow messages that are due now or within the next 60 seconds
                seconds_until_expected = (expected_send_time - current_time).total_seconds()
                if -60 <= seconds_until_expected <= 60:  # Past due up to 1 min, or due within 1 min
                    # Update the message's next_send_time to the current expected time
                    message.next_send_time = expected_send_time
                    due_now_messages.append(message)
            
            return due_now_messages
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get due repeating messages: {e}") from e
    
    async def mark_message_sent(self, message_id: UUID) -> bool:
        """Mark a repeating message as sent and update next send time.
        
        Args:
            message_id: Repeating message UUID
            
        Returns:
            True if message was updated, False if not found
        """
        try:
            # Get the message first
            message = await self.get_repeating_message(message_id)
            if not message:
                logger.warning(f"Repeating message {message_id} not found when trying to mark as sent")
                return False
            
            logger.info(f"Marking message {message_id} as sent - before update: next_send_time={message.next_send_time}, total_sent={message.total_sent}")
            
            # Update the message statistics and next send time
            message.update_after_send()
            
            await self.session.commit()
            logger.info(f"Marked repeating message {message_id} as sent - after update: next_send_time={message.next_send_time}, total_sent={message.total_sent}")
            
            return True
            
        except Exception as e:
            await self.session.rollback()
            raise DatabaseOperationError(f"Failed to mark repeating message as sent: {e}") from e
    
    async def toggle_repeating_message(self, message_id: UUID, is_active: bool) -> bool:
        """Enable or disable a repeating message.
        
        Args:
            message_id: Repeating message UUID
            is_active: Whether to enable or disable the message
            
        Returns:
            True if message was updated, False if not found
        """
        try:
            return await self.update_repeating_message(message_id, is_active=is_active)
            
        except Exception as e:
            raise DatabaseOperationError(f"Failed to toggle repeating message: {e}") from e


class GuildOperations:
    """Cross-cutting guild-scoped operations.

    Provides helpers that coordinate data across features for a guild.
    """

    async def remove_user_data(
        self,
        session: AsyncSession,
        guild_id: str,
        user_id: str,
    ) -> dict:
        """Remove a user's squad membership and bytes balance in a guild.

        Idempotent: succeeds even if records do not exist. Does not delete
        transaction history to preserve audit trails.

        Returns a dict with counts of deleted rows.
        """
        try:
            # Delete squad memberships for this user in the guild
            memberships_result = await session.execute(
                delete(SquadMembership).where(
                    SquadMembership.guild_id == guild_id,
                    SquadMembership.user_id == user_id,
                )
            )

            # Delete bytes balance for this user in the guild
            balances_result = await session.execute(
                delete(BytesBalance).where(
                    BytesBalance.guild_id == guild_id,
                    BytesBalance.user_id == user_id,
                )
            )

            return {
                "deleted_memberships": memberships_result.rowcount or 0,
                "deleted_balances": balances_result.rowcount or 0,
            }
        except Exception as e:
            raise DatabaseOperationError(f"Failed to remove user data: {e}") from e


class AuditLogConfigOperations:
    """Database operations for audit log configuration management.

    Handles CRUD operations for guild-specific audit logging settings.
    Manages audit log channel configuration and event type toggles.
    """

    async def get_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> Optional[AuditLogConfig]:
        """Get audit log configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            AuditLogConfig or None if not configured

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(AuditLogConfig).where(AuditLogConfig.guild_id == guild_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get audit log config: {e}") from e

    async def get_or_create_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> AuditLogConfig:
        """Get or create audit log configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            AuditLogConfig: Guild configuration (existing or newly created)

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            config = await self.get_config(session, guild_id)
            if config is None:
                config = AuditLogConfig(guild_id=guild_id)
                session.add(config)
                await session.flush()
            return config
        except DatabaseOperationError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get or create audit log config: {e}") from e

    async def update_config(
        self,
        session: AsyncSession,
        guild_id: str,
        **updates
    ) -> AuditLogConfig:
        """Update audit log configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            **updates: Fields to update

        Returns:
            AuditLogConfig: Updated configuration

        Raises:
            DatabaseOperationError: If update fails
        """
        try:
            config = await self.get_or_create_config(session, guild_id)

            # Update fields
            for key, value in updates.items():
                if hasattr(config, key):
                    setattr(config, key, value)

            config.updated_at = datetime.now(timezone.utc)
            await session.flush()

            return config
        except DatabaseOperationError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to update audit log config: {e}") from e

    async def set_audit_channel(
        self,
        session: AsyncSession,
        guild_id: str,
        channel_id: Optional[str]
    ) -> AuditLogConfig:
        """Set or clear the audit log channel for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            channel_id: Discord channel ID or None to clear

        Returns:
            AuditLogConfig: Updated configuration

        Raises:
            DatabaseOperationError: If update fails
        """
        return await self.update_config(
            session,
            guild_id,
            audit_channel_id=channel_id
        )

    async def set_event_logging(
        self,
        session: AsyncSession,
        guild_id: str,
        event_type: str,
        enabled: bool
    ) -> AuditLogConfig:
        """Enable or disable logging for a specific event type.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            event_type: Event type field name (e.g., 'log_member_join')
            enabled: Whether to enable logging for this event

        Returns:
            AuditLogConfig: Updated configuration

        Raises:
            DatabaseOperationError: If update fails
            ValueError: If event_type is invalid
        """
        valid_events = {
            'log_member_join', 'log_member_leave', 'log_member_ban', 'log_member_unban',
            'log_message_edit', 'log_message_delete', 'log_username_change',
            'log_nickname_change', 'log_role_change'
        }

        if event_type not in valid_events:
            raise ValueError(f"Invalid event type: {event_type}")

        return await self.update_config(
            session,
            guild_id,
            **{event_type: enabled}
        )

    async def delete_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> bool:
        """Delete audit log configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            bool: True if deleted, False if not found

        Raises:
            DatabaseOperationError: If deletion fails
        """
        try:
            result = await session.execute(
                delete(AuditLogConfig).where(AuditLogConfig.guild_id == guild_id)
            )
            return result.rowcount > 0
        except Exception as e:
            raise DatabaseOperationError(f"Failed to delete audit log config: {e}") from e


class AdventOfCodeConfigOperations:
    """Database operations for Advent of Code configuration management.

    Handles CRUD operations for guild-specific Advent of Code settings,
    including forum channel configuration and thread tracking.
    """

    async def get_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> Optional[AdventOfCodeConfig]:
        """Get Advent of Code configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            AdventOfCodeConfig or None if not configured

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(AdventOfCodeConfig).where(AdventOfCodeConfig.guild_id == guild_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get AoC config: {e}") from e

    async def get_or_create_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> AdventOfCodeConfig:
        """Get or create Advent of Code configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            AdventOfCodeConfig: Guild configuration (existing or newly created)

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            config = await self.get_config(session, guild_id)
            if config is None:
                config = AdventOfCodeConfig(guild_id=guild_id)
                session.add(config)
                await session.flush()
            return config
        except DatabaseOperationError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get or create AoC config: {e}") from e

    async def update_config(
        self,
        session: AsyncSession,
        guild_id: str,
        **updates
    ) -> AdventOfCodeConfig:
        """Update Advent of Code configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            **updates: Fields to update

        Returns:
            AdventOfCodeConfig: Updated configuration

        Raises:
            DatabaseOperationError: If update fails
        """
        try:
            config = await self.get_or_create_config(session, guild_id)

            # Update fields
            for key, value in updates.items():
                if hasattr(config, key):
                    setattr(config, key, value)

            config.updated_at = datetime.now(timezone.utc)
            await session.flush()

            return config
        except DatabaseOperationError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to update AoC config: {e}") from e

    async def get_active_configs(
        self,
        session: AsyncSession
    ) -> List[AdventOfCodeConfig]:
        """Get all active Advent of Code configurations.

        Args:
            session: Database session

        Returns:
            List of active AdventOfCodeConfig objects

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(AdventOfCodeConfig).where(
                AdventOfCodeConfig.is_active == True,
                AdventOfCodeConfig.forum_channel_id.isnot(None)
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get active AoC configs: {e}") from e

    async def get_posted_thread(
        self,
        session: AsyncSession,
        guild_id: str,
        year: int,
        day: int
    ) -> Optional[AdventOfCodeThread]:
        """Check if a thread has already been posted for a specific day.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            year: Advent of Code year
            day: Day of the challenge (1-25)

        Returns:
            AdventOfCodeThread if exists, None otherwise

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(AdventOfCodeThread).where(
                AdventOfCodeThread.guild_id == guild_id,
                AdventOfCodeThread.year == year,
                AdventOfCodeThread.day == day
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            raise DatabaseOperationError(f"Failed to check posted thread: {e}") from e

    async def record_posted_thread(
        self,
        session: AsyncSession,
        guild_id: str,
        year: int,
        day: int,
        thread_id: str,
        thread_title: str
    ) -> AdventOfCodeThread:
        """Record that a thread has been created for a specific day.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            year: Advent of Code year
            day: Day of the challenge (1-25)
            thread_id: Discord thread/post snowflake ID
            thread_title: Title of the created thread

        Returns:
            Created AdventOfCodeThread record

        Raises:
            DatabaseOperationError: If creation fails
            ConflictError: If thread already recorded
        """
        try:
            thread = AdventOfCodeThread(
                guild_id=guild_id,
                year=year,
                day=day,
                thread_id=thread_id,
                thread_title=thread_title
            )
            session.add(thread)
            await session.flush()
            return thread
        except IntegrityError as e:
            raise ConflictError(f"Thread already recorded for guild {guild_id}, year {year}, day {day}") from e
        except Exception as e:
            raise DatabaseOperationError(f"Failed to record posted thread: {e}") from e

    async def get_guild_threads(
        self,
        session: AsyncSession,
        guild_id: str,
        year: Optional[int] = None
    ) -> List[AdventOfCodeThread]:
        """Get all posted threads for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            year: Optional year filter

        Returns:
            List of AdventOfCodeThread records

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(AdventOfCodeThread).where(
                AdventOfCodeThread.guild_id == guild_id
            )
            if year is not None:
                stmt = stmt.where(AdventOfCodeThread.year == year)
            stmt = stmt.order_by(AdventOfCodeThread.year.desc(), AdventOfCodeThread.day.desc())
            result = await session.execute(stmt)
            return list(result.scalars().all())
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get guild threads: {e}") from e

    async def delete_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> bool:
        """Delete Advent of Code configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            bool: True if deleted, False if not found

        Raises:
            DatabaseOperationError: If deletion fails
        """
        try:
            result = await session.execute(
                delete(AdventOfCodeConfig).where(AdventOfCodeConfig.guild_id == guild_id)
            )
            return result.rowcount > 0
        except Exception as e:
            raise DatabaseOperationError(f"Failed to delete AoC config: {e}") from e


class AttachmentFilterConfigOperations:
    """Database operations for attachment filter configuration management.

    Handles CRUD operations for guild-specific attachment filtering settings.
    Manages blocked file extensions and action configuration.
    """

    async def get_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> Optional[AttachmentFilterConfig]:
        """Get attachment filter configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            AttachmentFilterConfig or None if not configured

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(AttachmentFilterConfig).where(AttachmentFilterConfig.guild_id == guild_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get attachment filter config: {e}") from e

    async def get_or_create_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> AttachmentFilterConfig:
        """Get or create attachment filter configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            AttachmentFilterConfig: Guild configuration (existing or newly created)

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            config = await self.get_config(session, guild_id)
            if config is None:
                config = AttachmentFilterConfig(guild_id=guild_id)
                session.add(config)
                await session.flush()
            return config
        except DatabaseOperationError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get or create attachment filter config: {e}") from e

    async def update_config(
        self,
        session: AsyncSession,
        guild_id: str,
        **updates
    ) -> AttachmentFilterConfig:
        """Update attachment filter configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID
            **updates: Fields to update

        Returns:
            AttachmentFilterConfig: Updated configuration

        Raises:
            DatabaseOperationError: If update fails
        """
        try:
            config = await self.get_or_create_config(session, guild_id)

            # Update fields
            for key, value in updates.items():
                if hasattr(config, key):
                    setattr(config, key, value)

            config.updated_at = datetime.now(timezone.utc)
            await session.flush()

            return config
        except DatabaseOperationError:
            raise
        except Exception as e:
            raise DatabaseOperationError(f"Failed to update attachment filter config: {e}") from e

    async def get_active_configs(
        self,
        session: AsyncSession
    ) -> List[AttachmentFilterConfig]:
        """Get all active attachment filter configurations.

        Args:
            session: Database session

        Returns:
            List of active AttachmentFilterConfig objects

        Raises:
            DatabaseOperationError: If query fails
        """
        try:
            stmt = select(AttachmentFilterConfig).where(
                AttachmentFilterConfig.is_active == True
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())
        except Exception as e:
            raise DatabaseOperationError(f"Failed to get active attachment filter configs: {e}") from e

    async def delete_config(
        self,
        session: AsyncSession,
        guild_id: str
    ) -> bool:
        """Delete attachment filter configuration for a guild.

        Args:
            session: Database session
            guild_id: Discord guild snowflake ID

        Returns:
            bool: True if deleted, False if not found

        Raises:
            DatabaseOperationError: If deletion fails
        """
        try:
            result = await session.execute(
                delete(AttachmentFilterConfig).where(AttachmentFilterConfig.guild_id == guild_id)
            )
            return result.rowcount > 0
        except Exception as e:
            raise DatabaseOperationError(f"Failed to delete attachment filter config: {e}") from e
