"""Service-specific exceptions for the Discord bot services.

This module defines a comprehensive exception hierarchy for bot services,
following the principle of explicit error handling and providing clear
error contexts for different failure scenarios.
"""

from __future__ import annotations

from typing import Any


class ServiceError(Exception):
    """Base exception for all service layer errors.

    This base class provides common functionality for all service errors
    including error codes, context data, and user-friendly messages.
    """

    def __init__(
        self,
        message: str,
        error_code: str | None = None,
        context: dict[str, Any] | None = None,
        user_message: str | None = None
    ):
        """Initialize service error.

        Args:
            message: Technical error message for logging
            error_code: Unique error code for categorization
            context: Additional context data for debugging
            user_message: User-friendly error message for Discord responses
        """
        super().__init__(message)
        self.error_code = error_code or self.__class__.__name__
        self.context = context or {}
        self.user_message = user_message or message

    def __str__(self) -> str:
        """Return technical error message."""
        return super().__str__()

    def get_user_message(self) -> str:
        """Get user-friendly error message."""
        return self.user_message


class APIError(ServiceError):
    """Exception for API communication errors.

    Raised when there are issues communicating with the backend API,
    including network errors, HTTP errors, and response parsing failures.
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        response_body: str | None = None,
        **kwargs
    ):
        """Initialize API error.

        Args:
            message: Error message
            status_code: HTTP status code
            response_body: Response body content
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(message, **kwargs)
        self.status_code = status_code
        self.response_body = response_body


class NetworkError(APIError):
    """Exception for network-related errors.

    Raised when there are underlying network issues such as
    timeouts, connection failures, or DNS resolution problems.
    """

    def __init__(self, message: str, **kwargs):
        super().__init__(
            message,
            user_message="Unable to connect to the server. Please try again later.",
            **kwargs
        )


class RateLimitError(APIError):
    """Exception for rate limiting errors.

    Raised when the API rate limit is exceeded and requests
    are being throttled or rejected.
    """

    def __init__(self, retry_after: int | None = None, **kwargs):
        """Initialize rate limit error.

        Args:
            retry_after: Seconds to wait before retrying
            **kwargs: Additional arguments passed to parent
        """
        message = f"Rate limit exceeded. Retry after {retry_after} seconds."
        super().__init__(
            message,
            status_code=429,
            user_message="Too many requests. Please wait a moment and try again.",
            **kwargs
        )
        self.retry_after = retry_after


class AuthenticationError(APIError):
    """Exception for authentication failures.

    Raised when API requests fail due to invalid or missing
    authentication credentials.
    """

    def __init__(self, **kwargs):
        super().__init__(
            "Authentication failed",
            status_code=401,
            error_code="AUTH_FAILED",
            user_message="Authentication error. Please contact an administrator.",
            **kwargs
        )


class ValidationError(ServiceError):
    """Exception for data validation errors.

    Raised when input data doesn't meet validation requirements
    such as invalid formats, missing fields, or constraint violations.
    """

    def __init__(self, field: str, message: str, **kwargs):
        """Initialize validation error.

        Args:
            field: Field name that failed validation
            message: Validation error message
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(
            f"Validation failed for {field}: {message}",
            error_code="VALIDATION_FAILED",
            context={"field": field},
            user_message=message,
            **kwargs
        )
        self.field = field


class ResourceNotFoundError(ServiceError):
    """Exception for missing resources.

    Raised when a requested resource (user, guild, squad, etc.)
    cannot be found in the system.
    """

    def __init__(self, resource_type: str, resource_id: str, **kwargs):
        """Initialize resource not found error.

        Args:
            resource_type: Type of resource (e.g., "user", "guild", "squad")
            resource_id: ID of the missing resource
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(
            f"{resource_type.title()} not found: {resource_id}",
            error_code="RESOURCE_NOT_FOUND",
            context={"resource_type": resource_type, "resource_id": resource_id},
            user_message=f"{resource_type.title()} not found.",
            **kwargs
        )
        self.resource_type = resource_type
        self.resource_id = resource_id


class InsufficientBalanceError(ServiceError):
    """Exception for insufficient balance errors.

    Raised when a user doesn't have enough bytes to perform
    an operation such as transfers or squad switching.
    """

    def __init__(
        self,
        required: int,
        available: int,
        operation: str = "operation",
        **kwargs
    ):
        """Initialize insufficient balance error.

        Args:
            required: Required amount for the operation
            available: Available balance
            operation: Description of the operation being attempted
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(
            f"Insufficient balance for {operation}: required {required}, available {available}",
            error_code="INSUFFICIENT_BALANCE",
            context={
                "required": required,
                "available": available,
                "operation": operation
            },
            user_message=f"Insufficient balance! You need {required} bytes but only have {available}.",
            **kwargs
        )
        self.required = required
        self.available = available
        self.operation = operation


class AlreadyClaimedError(ServiceError):
    """Exception for duplicate daily claim attempts.

    Raised when a user tries to claim their daily reward
    when they have already claimed it today.
    """

    def __init__(self, next_claim_time: str | None = None, **kwargs):
        """Initialize already claimed error.

        Args:
            next_claim_time: When the user can claim again
            **kwargs: Additional arguments passed to parent
        """
        message = "Daily reward already claimed today"
        user_msg = "You've already claimed your daily bytes today!"

        if next_claim_time:
            user_msg += f" You can claim again {next_claim_time}."

        super().__init__(
            message,
            error_code="ALREADY_CLAIMED",
            user_message=user_msg,
            **kwargs
        )
        self.next_claim_time = next_claim_time


class SquadError(ServiceError):
    """Base exception for squad-related errors."""
    pass


class AlreadyInSquadError(SquadError):
    """Exception for users already in a squad.

    Raised when a user tries to join a squad but is
    already a member of the same or different squad.
    """

    def __init__(self, current_squad_name: str, **kwargs):
        """Initialize already in squad error.

        Args:
            current_squad_name: Name of the squad user is currently in
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(
            f"User is already in squad: {current_squad_name}",
            error_code="ALREADY_IN_SQUAD",
            user_message=f"You're already in the {current_squad_name} squad!",
            **kwargs
        )
        self.current_squad_name = current_squad_name


class SquadFullError(SquadError):
    """Exception for squad capacity limits.

    Raised when a user tries to join a squad that has
    reached its maximum member capacity.
    """

    def __init__(self, squad_name: str, max_capacity: int, **kwargs):
        """Initialize squad full error.

        Args:
            squad_name: Name of the full squad
            max_capacity: Maximum capacity of the squad
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(
            f"Squad {squad_name} is full (capacity: {max_capacity})",
            error_code="SQUAD_FULL",
            user_message=f"The {squad_name} squad is full! (Maximum: {max_capacity} members)",
            **kwargs
        )
        self.squad_name = squad_name
        self.max_capacity = max_capacity


class NotInSquadError(SquadError):
    """Exception for users not in any squad.

    Raised when a user tries to perform squad-specific
    operations without being a member of any squad.
    """

    def __init__(self, **kwargs):
        super().__init__(
            "User is not in any squad",
            error_code="NOT_IN_SQUAD",
            user_message="You're not currently in any squad!",
            **kwargs
        )


class CacheError(ServiceError):
    """Exception for caching-related errors.

    Raised when there are issues with cache operations
    such as Redis connection problems or serialization failures.
    """

    def __init__(self, operation: str, **kwargs):
        """Initialize cache error.

        Args:
            operation: Cache operation that failed
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(
            f"Cache operation failed: {operation}",
            error_code="CACHE_ERROR",
            user_message="Temporary service issue. Please try again.",
            **kwargs
        )
        self.operation = operation


class ConfigurationError(ServiceError):
    """Exception for configuration-related errors.

    Raised when there are issues with service configuration
    such as missing settings or invalid values.
    """

    def __init__(self, setting: str, **kwargs):
        """Initialize configuration error.

        Args:
            setting: Configuration setting that caused the error
            **kwargs: Additional arguments passed to parent
        """
        super().__init__(
            f"Configuration error: {setting}",
            error_code="CONFIG_ERROR",
            user_message="Service configuration issue. Please contact an administrator.",
            **kwargs
        )
        self.setting = setting
