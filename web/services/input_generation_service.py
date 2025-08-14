"""
Input Generation Service - Following SOLID principles.

This service handles Python script execution for generating challenge inputs,
with caching, timeout handling, and comprehensive error management.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Any, Optional, Protocol, Tuple
from uuid import UUID
import logging

logger = logging.getLogger(__name__)


class InputGenerationStatus(Enum):
    """Status of input generation."""
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class InputGenerationResult:
    """
    Result of input generation attempt.
    
    Contains the generated input data, expected result, and metadata
    about the generation process.
    """
    status: InputGenerationStatus
    input_data: Optional[Dict[str, Any]] = None
    expected_result: Optional[str] = None
    execution_time_ms: int = 0
    cached: bool = False
    error_message: Optional[str] = None
    script_output: Optional[str] = None


class ScriptExecutionError(Exception):
    """Exception raised when script execution fails."""
    
    def __init__(self, message: str, stderr_output: str = ""):
        super().__init__(message)
        self.stderr_output = stderr_output


class ScriptTimeoutError(Exception):
    """Exception raised when script execution times out."""
    
    def __init__(self, timeout_seconds: int):
        super().__init__(f"Script execution timed out after {timeout_seconds} seconds")
        self.timeout_seconds = timeout_seconds


class ScriptValidationError(Exception):
    """Exception raised when script output validation fails."""
    pass


class ChallengeProtocol(Protocol):
    """Protocol defining the interface for challenge objects."""
    id: UUID
    generation_script: str
    script_updated_at: datetime


class InputCacheProtocol(Protocol):
    """Protocol defining the interface for input cache objects."""
    input_json: Dict[str, Any]
    expected_result: str
    is_valid: bool


class SubmissionRepositoryProtocol(Protocol):
    """Protocol defining the interface for submission repository."""
    
    async def get_input_cache(
        self, 
        challenge_id: UUID, 
        participant_id: str, 
        participant_type: str
    ) -> Optional[InputCacheProtocol]:
        """Get cached input for a participant."""
        pass
    
    async def create_or_get_input_cache(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str,
        input_json: Dict[str, Any],
        expected_result: str
    ) -> InputCacheProtocol:
        """Create or get cached input."""
        pass
    
    async def invalidate_input_cache(
        self,
        challenge_id: UUID,
        participant_id: Optional[str] = None,
        participant_type: Optional[str] = None
    ) -> int:
        """Invalidate cached inputs."""
        pass


class InputGenerationService:
    """
    Service for generating challenge inputs through Python script execution.
    
    Following SRP: Only handles input generation and caching logic.
    Following DIP: Depends on abstractions (repository protocol).
    Following OCP: Extensible for different script execution strategies.
    """
    
    def __init__(
        self,
        submission_repository: SubmissionRepositoryProtocol,
        script_timeout_seconds: int = 30,
        max_output_size: int = 1024 * 1024  # 1MB
    ):
        """
        Initialize service with repository dependency and configuration.
        
        Args:
            submission_repository: Repository for submission data access
            script_timeout_seconds: Maximum script execution time
            max_output_size: Maximum allowed script output size in bytes
        """
        self.submission_repository = submission_repository
        self.script_timeout_seconds = script_timeout_seconds
        self.max_output_size = max_output_size
    
    async def generate_input(
        self,
        challenge: ChallengeProtocol,
        participant_id: str,
        participant_type: str
    ) -> InputGenerationResult:
        """
        Generate input for a challenge participant.
        
        First checks for cached input, then executes generation script if needed.
        
        Args:
            challenge: Challenge containing generation script
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            
        Returns:
            InputGenerationResult with generated or cached input
            
        Raises:
            ValueError: If input validation fails
        """
        start_time = time.time()
        
        # Input validation
        if challenge is None:
            raise ValueError("Challenge cannot be None")
        
        if not participant_id or not participant_id.strip():
            raise ValueError("Participant ID cannot be empty")
        
        if participant_type not in ["player", "squad"]:
            raise ValueError("Participant type must be 'player' or 'squad'")
        
        try:
            # Check for existing cached input
            cached_input = await self.submission_repository.get_input_cache(
                challenge_id=challenge.id,
                participant_id=participant_id,
                participant_type=participant_type
            )
            
            if cached_input and cached_input.is_valid:
                logger.info(
                    f"Using cached input for challenge {challenge.id}, "
                    f"participant {participant_id} ({participant_type})"
                )
                
                return InputGenerationResult(
                    status=InputGenerationStatus.SUCCESS,
                    input_data=cached_input.input_json,
                    expected_result=cached_input.expected_result,
                    execution_time_ms=0,
                    cached=True
                )
            
            # Generate new input
            logger.info(
                f"Generating new input for challenge {challenge.id}, "
                f"participant {participant_id} ({participant_type})"
            )
            
            # Execute generation script
            script_output = await self._execute_generation_script(challenge.generation_script)
            
            # Validate and parse output
            input_data, expected_result = self._validate_script_output(script_output)
            
            # Cache the generated input
            await self.submission_repository.create_or_get_input_cache(
                challenge_id=challenge.id,
                participant_id=participant_id,
                participant_type=participant_type,
                input_json=input_data,
                expected_result=expected_result
            )
            
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            logger.info(
                f"Successfully generated input for challenge {challenge.id} "
                f"in {execution_time_ms}ms"
            )
            
            return InputGenerationResult(
                status=InputGenerationStatus.SUCCESS,
                input_data=input_data,
                expected_result=expected_result,
                execution_time_ms=execution_time_ms,
                cached=False,
                script_output=script_output
            )
            
        except ScriptTimeoutError as e:
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            logger.warning(
                f"Script timeout for challenge {challenge.id}: {str(e)}"
            )
            
            return InputGenerationResult(
                status=InputGenerationStatus.TIMEOUT,
                execution_time_ms=execution_time_ms,
                error_message=f"Script execution timed out after {e.timeout_seconds} seconds"
            )
            
        except (ScriptExecutionError, ScriptValidationError) as e:
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            logger.error(
                f"Input generation failed for challenge {challenge.id}: {str(e)}"
            )
            
            return InputGenerationResult(
                status=InputGenerationStatus.ERROR,
                execution_time_ms=execution_time_ms,
                error_message=str(e)
            )
            
        except Exception as e:
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            logger.exception(
                f"Unexpected error during input generation for challenge {challenge.id}"
            )
            
            return InputGenerationResult(
                status=InputGenerationStatus.ERROR,
                execution_time_ms=execution_time_ms,
                error_message=f"Unexpected error: {str(e)}"
            )
    
    async def _execute_generation_script(self, script: str) -> str:
        """
        Execute the Python generation script in a secure subprocess.
        
        Args:
            script: Python script to execute
            
        Returns:
            Script output as string
            
        Raises:
            ScriptExecutionError: If script execution fails
            ScriptTimeoutError: If script execution times out
        """
        try:
            # Run script in subprocess with timeout
            result = subprocess.run(
                ["python", "-c", script],
                capture_output=True,
                text=True,
                timeout=self.script_timeout_seconds,
                # Security: run with limited environment
                env={"PYTHONPATH": ""}
            )
            
            if result.returncode != 0:
                raise ScriptExecutionError(
                    f"Script execution failed with return code {result.returncode}",
                    result.stderr
                )
            
            # Check output size
            if len(result.stdout) > self.max_output_size:
                raise ScriptExecutionError(
                    f"Script output too large: {len(result.stdout)} bytes "
                    f"(max {self.max_output_size} bytes)"
                )
            
            return result.stdout.strip()
            
        except subprocess.TimeoutExpired:
            raise ScriptTimeoutError(self.script_timeout_seconds)
        
        except subprocess.SubprocessError as e:
            raise ScriptExecutionError(f"Subprocess error: {str(e)}")
    
    def _validate_script_output(self, output: str) -> Tuple[Dict[str, Any], str]:
        """
        Validate and parse script output.
        
        Expected format: {"input": <data>, "expected": <result>}
        
        Args:
            output: Raw script output
            
        Returns:
            Tuple of (input_data, expected_result)
            
        Raises:
            ScriptValidationError: If output format is invalid
        """
        try:
            # Parse JSON output
            parsed_output = json.loads(output)
            
            if not isinstance(parsed_output, dict):
                raise ScriptValidationError("Script output must be a JSON object")
            
            # Check for required fields
            if "input" not in parsed_output:
                raise ScriptValidationError("Missing required field 'input' in script output")
            
            if "expected" not in parsed_output:
                raise ScriptValidationError("Missing required field 'expected' in script output")
            
            input_data = parsed_output["input"]
            expected_result = str(parsed_output["expected"])
            
            # Validate input_data is JSON serializable
            try:
                json.dumps(input_data)
            except (TypeError, ValueError) as e:
                raise ScriptValidationError(f"Input data is not JSON serializable: {str(e)}")
            
            return input_data, expected_result
            
        except json.JSONDecodeError as e:
            raise ScriptValidationError(f"Invalid JSON output: {str(e)}")
    
    async def get_cached_input(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str
    ) -> Optional[InputCacheProtocol]:
        """
        Get cached input for a participant if it exists.
        
        Args:
            challenge_id: Challenge UUID
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            
        Returns:
            Cached input or None if not found
        """
        return await self.submission_repository.get_input_cache(
            challenge_id=challenge_id,
            participant_id=participant_id,
            participant_type=participant_type
        )
    
    async def invalidate_cache_for_challenge(self, challenge_id: UUID) -> int:
        """
        Invalidate all cached inputs for a challenge.
        
        Useful when challenge script is updated.
        
        Args:
            challenge_id: Challenge UUID
            
        Returns:
            Number of cache entries invalidated
        """
        invalidated_count = await self.submission_repository.invalidate_input_cache(
            challenge_id=challenge_id
        )
        
        logger.info(
            f"Invalidated {invalidated_count} cache entries for challenge {challenge_id}"
        )
        
        return invalidated_count
    
    async def invalidate_cache_for_participant(
        self,
        challenge_id: UUID,
        participant_id: str,
        participant_type: str
    ) -> int:
        """
        Invalidate cached input for a specific participant.
        
        Args:
            challenge_id: Challenge UUID
            participant_id: Player ID or Squad ID
            participant_type: 'player' or 'squad'
            
        Returns:
            Number of cache entries invalidated (0 or 1)
        """
        invalidated_count = await self.submission_repository.invalidate_input_cache(
            challenge_id=challenge_id,
            participant_id=participant_id,
            participant_type=participant_type
        )
        
        logger.info(
            f"Invalidated cache for challenge {challenge_id}, "
            f"participant {participant_id} ({participant_type})"
        )
        
        return invalidated_count
    
    async def validate_generation_script(self, script: str) -> Dict[str, Any]:
        """
        Validate a generation script by running it and checking output format.
        
        Useful for admin interfaces to test scripts before saving.
        
        Args:
            script: Python script to validate
            
        Returns:
            Dictionary with validation result and details
        """
        validation_result = {
            "valid": False,
            "error_message": None,
            "execution_time_ms": 0,
            "sample_output": None
        }
        
        start_time = time.time()
        
        try:
            # Execute script
            output = await self._execute_generation_script(script)
            
            # Validate output format
            input_data, expected_result = self._validate_script_output(output)
            
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            validation_result.update({
                "valid": True,
                "execution_time_ms": execution_time_ms,
                "sample_output": {
                    "input": input_data,
                    "expected": expected_result
                }
            })
            
            logger.info(f"Script validation successful in {execution_time_ms}ms")
            
        except (ScriptExecutionError, ScriptTimeoutError, ScriptValidationError) as e:
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            validation_result.update({
                "valid": False,
                "error_message": str(e),
                "execution_time_ms": execution_time_ms
            })
            
            logger.warning(f"Script validation failed: {str(e)}")
            
        except Exception as e:
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            validation_result.update({
                "valid": False,
                "error_message": f"Unexpected error: {str(e)}",
                "execution_time_ms": execution_time_ms
            })
            
            logger.exception("Unexpected error during script validation")
        
        return validation_result
    
    async def get_generation_statistics(
        self, 
        challenge_id: UUID
    ) -> Dict[str, Any]:
        """
        Get statistics about input generation for a challenge.
        
        Args:
            challenge_id: Challenge UUID
            
        Returns:
            Dictionary with generation statistics
        """
        # This would typically query the submission repository for statistics
        # For now, return a basic structure that can be extended
        return {
            "challenge_id": str(challenge_id),
            "cache_statistics": {
                "total_cached_inputs": 0,  # Would query repository
                "valid_cached_inputs": 0,  # Would query repository
                "invalid_cached_inputs": 0  # Would query repository
            },
            "performance_statistics": {
                "average_generation_time_ms": 0,  # Would calculate from logs/metrics
                "max_generation_time_ms": 0,
                "min_generation_time_ms": 0
            }
        }