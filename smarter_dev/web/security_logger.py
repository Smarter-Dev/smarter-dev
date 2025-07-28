"""Security logging implementation for audit trail and monitoring.

This module provides comprehensive security logging capabilities for the API
including authentication events, API key operations, rate limiting violations,
and administrative actions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Dict, Any
from uuid import UUID

from fastapi import Request
from sqlalchemy import select, delete, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from smarter_dev.web.models import SecurityLog, APIKey

logger = logging.getLogger(__name__)


class SecurityLogger:
    """Comprehensive security logging and audit trail system."""

    def __init__(self):
        """Initialize the security logger."""
        self.logger = logging.getLogger(f"{__name__}.SecurityLogger")

    async def log_event(
        self,
        session: Optional[AsyncSession],
        action: str,
        success: bool,
        details: str,
        api_key_id: Optional[UUID] = None,
        user_identifier: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
        event_metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[SecurityLog]:
        """Log a security event.
        
        Args:
            session: Database session (optional - will create new session if None)
            action: Type of action performed
            success: Whether the action was successful
            details: Detailed description of the event
            api_key_id: API key involved (if applicable)
            user_identifier: User identifier
            ip_address: Client IP address
            user_agent: Client user agent
            request_id: Request correlation ID
            event_metadata: Additional structured metadata
            
        Returns:
            SecurityLog: The created log entry, or None if logging failed
        """
        # Log to application logger first for immediate visibility
        log_level = logging.INFO if success else logging.WARNING
        self.logger.log(
            log_level,
            f"Security event: {action} - {'SUCCESS' if success else 'FAILURE'} - {details}"
        )
        
        try:
            security_log = SecurityLog(
                action=action,
                api_key_id=api_key_id,
                user_identifier=user_identifier,
                ip_address=ip_address,
                user_agent=user_agent,
                request_id=request_id,
                success=success,
                details=details,
                event_metadata=event_metadata
            )
            
            # If no session provided, create a new one for reliable logging
            if session is None:
                from smarter_dev.shared.database import get_db_session
                async for db_session in get_db_session():
                    try:
                        db_session.add(security_log)
                        await db_session.commit()
                        return security_log
                    except Exception as db_error:
                        await db_session.rollback()
                        self.logger.error(f"Failed to log security event to database: {db_error}")
                        return None
            else:
                # Use provided session
                session.add(security_log)
                await session.flush()  # Flush to get the ID
                
                # Try to commit, but don't fail if it doesn't work
                try:
                    await session.commit()
                except Exception as commit_error:
                    self.logger.warning(f"Security log commit failed (expected in some cases): {commit_error}")
                    # Don't rollback as it might interfere with the main transaction
                
                return security_log
            
        except Exception as e:
            # Don't let logging failures break the application
            self.logger.error(f"Failed to log security event: {e}")
            return None

    async def log_api_key_created(
        self,
        session: AsyncSession,
        api_key: APIKey,
        user_identifier: str,
        request: Optional[Request] = None
    ) -> SecurityLog:
        """Log API key creation event."""
        return await self.log_event(
            session=session,
            action="api_key_created",
            success=True,
            details=f"API key '{api_key.name}' created with scopes: {', '.join(api_key.scopes)}",
            api_key_id=api_key.id,
            user_identifier=user_identifier,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
            request_id=request.headers.get("x-request-id") if request else None,
            event_metadata={
                "api_key_name": api_key.name,
                "scopes": api_key.scopes,
                "rate_limit": api_key.rate_limit_per_hour
            }
        )

    async def log_api_key_deleted(
        self,
        session: AsyncSession,
        api_key_id: UUID,
        api_key_name: str,
        user_identifier: str,
        request: Optional[Request] = None
    ) -> SecurityLog:
        """Log API key deletion event."""
        return await self.log_event(
            session=session,
            action="api_key_deleted",
            success=True,
            details=f"API key '{api_key_name}' deleted",
            api_key_id=api_key_id,
            user_identifier=user_identifier,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
            request_id=request.headers.get("x-request-id") if request else None,
            event_metadata={"api_key_name": api_key_name}
        )

    async def log_api_key_used(
        self,
        session: AsyncSession,
        api_key: APIKey,
        request: Request,
        success: bool = True,
        error_message: Optional[str] = None
    ) -> SecurityLog:
        """Log API key usage event."""
        details = f"API key '{api_key.key_prefix}***' used successfully"
        if not success and error_message:
            details = f"API key '{api_key.key_prefix}***' usage failed: {error_message}"
        
        return await self.log_event(
            session=session,
            action="api_key_used",
            success=success,
            details=details,
            api_key_id=api_key.id,
            user_identifier=api_key.created_by,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "unknown"),
            request_id=request.headers.get("x-request-id"),
            event_metadata={
                "api_key_prefix": api_key.key_prefix,
                "endpoint": str(request.url.path),
                "method": request.method
            }
        )

    async def log_authentication_failed(
        self,
        session: Optional[AsyncSession],
        failed_key_prefix: str,
        request: Request,
        reason: str
    ) -> Optional[SecurityLog]:
        """Log failed authentication attempt."""
        return await self.log_event(
            session=session,
            action="authentication_failed",
            success=False,
            details=f"Authentication failed for key '{failed_key_prefix}***': {reason}",
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "unknown"),
            request_id=request.headers.get("x-request-id"),
            event_metadata={
                "failed_key_prefix": failed_key_prefix,
                "endpoint": str(request.url.path),
                "method": request.method,
                "reason": reason
            }
        )

    async def log_api_request(
        self,
        session: AsyncSession,
        api_key: APIKey,
        request: Request,
        success: bool = True
    ) -> SecurityLog:
        """Log an API request for rate limiting tracking."""
        return await self.log_event(
            session=session,
            action="api_request",
            success=success,
            details=f"API request to {request.method} {request.url.path}",
            api_key_id=api_key.id,
            user_identifier=api_key.created_by,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            event_metadata={
                "method": request.method,
                "path": str(request.url.path),
                "query_params": dict(request.query_params)
            }
        )
    
    async def log_rate_limit_exceeded(
        self,
        session: AsyncSession,
        api_key: APIKey,
        request: Request,
        current_usage: int,
        limit: int,
        window: str = "hour"
    ) -> SecurityLog:
        """Log rate limit violation."""
        return await self.log_event(
            session=session,
            action="rate_limit_exceeded",
            success=False,
            details=f"Rate limit exceeded for API key '{api_key.key_prefix}***' ({current_usage}/{limit} requests per {window})",
            api_key_id=api_key.id,
            user_identifier=api_key.created_by,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "unknown"),
            request_id=request.headers.get("x-request-id"),
            event_metadata={
                "api_key_prefix": api_key.key_prefix,
                "current_usage": current_usage,
                "rate_limit": limit,
                "endpoint": str(request.url.path)
            }
        )

    async def log_admin_operation(
        self,
        session: AsyncSession,
        operation: str,
        user_identifier: str,
        request: Request,
        success: bool = True,
        details: Optional[str] = None
    ) -> SecurityLog:
        """Log administrative operation."""
        if not details:
            details = f"Admin operation: {operation}"
        
        return await self.log_event(
            session=session,
            action="admin_operation",
            success=success,
            details=details,
            user_identifier=user_identifier,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent", "unknown"),
            request_id=request.headers.get("x-request-id"),
            event_metadata={
                "operation": operation,
                "endpoint": str(request.url.path),
                "method": request.method
            }
        )

    async def log_suspicious_activity(
        self,
        session: AsyncSession,
        activity_type: str,
        details: str,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        request_id: Optional[str] = None,
        event_metadata: Optional[Dict[str, Any]] = None
    ) -> SecurityLog:
        """Log suspicious activity detection."""
        return await self.log_event(
            session=session,
            action="suspicious_activity",
            success=False,
            details=f"Suspicious activity detected: {activity_type} - {details}",
            ip_address=ip_address,
            user_agent=user_agent,
            request_id=request_id,
            event_metadata=event_metadata or {"activity_type": activity_type}
        )

    async def get_logs_for_api_key(
        self,
        session: AsyncSession,
        api_key_id: UUID,
        limit: int = 100
    ) -> List[SecurityLog]:
        """Get security logs for a specific API key."""
        stmt = (
            select(SecurityLog)
            .where(SecurityLog.api_key_id == api_key_id)
            .order_by(SecurityLog.timestamp.desc())
            .limit(limit)
        )
        
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_recent_logs(
        self,
        session: AsyncSession,
        action: Optional[str] = None,
        success: Optional[bool] = None,
        limit: int = 100,
        hours_back: int = 24
    ) -> List[SecurityLog]:
        """Get recent security logs with optional filtering."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_back)
        
        conditions = [SecurityLog.timestamp >= cutoff_time]
        
        if action is not None:
            conditions.append(SecurityLog.action == action)
        
        if success is not None:
            conditions.append(SecurityLog.success == success)
        
        stmt = (
            select(SecurityLog)
            .where(and_(*conditions))
            .order_by(SecurityLog.timestamp.desc())
            .limit(limit)
        )
        
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_logs_by_action(
        self,
        session: AsyncSession,
        action: str,
        limit: int = 100
    ) -> List[SecurityLog]:
        """Get logs filtered by action type."""
        stmt = (
            select(SecurityLog)
            .where(SecurityLog.action == action)
            .order_by(SecurityLog.timestamp.desc())
            .limit(limit)
        )
        
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_logs_by_success(
        self,
        session: AsyncSession,
        success: bool,
        limit: int = 100
    ) -> List[SecurityLog]:
        """Get logs filtered by success status."""
        stmt = (
            select(SecurityLog)
            .where(SecurityLog.success == success)
            .order_by(SecurityLog.timestamp.desc())
            .limit(limit)
        )
        
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def get_logs_by_date_range(
        self,
        session: AsyncSession,
        start_date: datetime,
        end_date: datetime,
        limit: int = 1000
    ) -> List[SecurityLog]:
        """Get logs within a specific date range."""
        stmt = (
            select(SecurityLog)
            .where(
                and_(
                    SecurityLog.timestamp >= start_date,
                    SecurityLog.timestamp <= end_date
                )
            )
            .order_by(SecurityLog.timestamp.desc())
            .limit(limit)
        )
        
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def cleanup_old_logs(
        self,
        session: AsyncSession,
        retention_days: int = 90
    ) -> int:
        """Clean up old security logs beyond retention period."""
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=retention_days)
        
        stmt = delete(SecurityLog).where(SecurityLog.timestamp < cutoff_date)
        result = await session.execute(stmt)
        deleted_count = result.rowcount
        
        await session.commit()
        
        if deleted_count > 0:
            self.logger.info(f"Cleaned up {deleted_count} security log entries older than {retention_days} days")
        
        return deleted_count

    async def detect_suspicious_patterns(
        self,
        session: AsyncSession,
        ip_address: str,
        time_window_minutes: int = 15,
        failure_threshold: int = 5
    ) -> bool:
        """Detect suspicious authentication patterns."""
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=time_window_minutes)
        
        # Count failed authentication attempts from the same IP
        stmt = (
            select(func.count(SecurityLog.id))
            .where(
                and_(
                    SecurityLog.ip_address == ip_address,
                    SecurityLog.action == "authentication_failed",
                    SecurityLog.timestamp >= cutoff_time
                )
            )
        )
        
        result = await session.execute(stmt)
        failure_count = result.scalar() or 0
        
        if failure_count >= failure_threshold:
            # Log suspicious activity
            await self.log_suspicious_activity(
                session=session,
                activity_type="rapid_auth_failures",
                details=f"Rapid authentication failures detected from IP {ip_address}: {failure_count} failures in {time_window_minutes} minutes",
                ip_address=ip_address,
                event_metadata={
                    "failure_count": failure_count,
                    "time_window_minutes": time_window_minutes,
                    "threshold": failure_threshold
                }
            )
            return True
        
        return False


# Global security logger instance
security_logger = SecurityLogger()


def get_security_logger() -> SecurityLogger:
    """Get the global security logger instance."""
    return security_logger