"""
Web Services Package.

This package contains all service layer implementations for the web application,
following the service layer pattern to separate business logic from controllers
and data access layers.
"""

from .scoring_strategies import (
    ScoringStrategy,
    ScoringResult,
    TimeBasedScoringStrategy,
    PointBasedScoringStrategy,
    create_scoring_strategy
)
from .challenge_release_service import (
    ChallengeReleaseService,
    ChallengeReleaseInfo,
    ChallengeReleaseStatus
)
from .input_generation_service import (
    InputGenerationService,
    InputGenerationResult,
    InputGenerationStatus,
    ScriptExecutionError,
    ScriptTimeoutError,
    ScriptValidationError
)
from .submission_validation_service import (
    SubmissionValidationService,
    ValidationResult,
    ValidationStatus,
    ValidationError
)
from .rate_limiting_service import (
    RateLimitingService,
    RateLimitResult,
    RateLimitStatus,
    RateLimitConfig,
    RateLimitExceededError
)
from .squad_integration_service import (
    SquadIntegrationService,
    SquadChallengeResult,
    SquadChallengeRole,
    SquadMemberInfo
)

__all__ = [
    "ScoringStrategy",
    "ScoringResult", 
    "TimeBasedScoringStrategy",
    "PointBasedScoringStrategy",
    "create_scoring_strategy",
    "ChallengeReleaseService",
    "ChallengeReleaseInfo",
    "ChallengeReleaseStatus",
    "InputGenerationService",
    "InputGenerationResult",
    "InputGenerationStatus",
    "ScriptExecutionError",
    "ScriptTimeoutError",
    "ScriptValidationError",
    "SubmissionValidationService",
    "ValidationResult",
    "ValidationStatus",
    "ValidationError",
    "RateLimitingService",
    "RateLimitResult",
    "RateLimitStatus",
    "RateLimitConfig",
    "RateLimitExceededError",
    "SquadIntegrationService",
    "SquadChallengeResult",
    "SquadChallengeRole",
    "SquadMemberInfo"
]