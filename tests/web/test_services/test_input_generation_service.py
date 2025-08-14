"""Test cases for Input Generation Service."""

from __future__ import annotations

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from unittest.mock import AsyncMock, Mock, patch
import json

from web.services.input_generation_service import (
    InputGenerationService,
    InputGenerationResult,
    InputGenerationStatus,
    ScriptExecutionError,
    ScriptTimeoutError,
    ScriptValidationError
)


class TestInputGenerationResult:
    """Test cases for InputGenerationResult data structure."""
    
    def test_input_generation_result_success(self):
        """Test InputGenerationResult creation for successful generation."""
        input_data = {"n": 100, "data": [1, 2, 3]}
        expected_result = "expected_output"
        
        result = InputGenerationResult(
            status=InputGenerationStatus.SUCCESS,
            input_data=input_data,
            expected_result=expected_result,
            execution_time_ms=150,
            cached=False
        )
        
        assert result.status == InputGenerationStatus.SUCCESS
        assert result.input_data == input_data
        assert result.expected_result == expected_result
        assert result.execution_time_ms == 150
        assert result.cached is False
        assert result.error_message is None
    
    def test_input_generation_result_cached(self):
        """Test InputGenerationResult for cached input."""
        result = InputGenerationResult(
            status=InputGenerationStatus.SUCCESS,
            input_data={"cached": True},
            expected_result="cached_result",
            execution_time_ms=0,
            cached=True
        )
        
        assert result.cached is True
        assert result.execution_time_ms == 0  # No execution time for cached results
    
    def test_input_generation_result_error(self):
        """Test InputGenerationResult for error case."""
        result = InputGenerationResult(
            status=InputGenerationStatus.ERROR,
            error_message="Script execution failed",
            execution_time_ms=50
        )
        
        assert result.status == InputGenerationStatus.ERROR
        assert result.input_data is None
        assert result.expected_result is None
        assert result.error_message == "Script execution failed"
        assert result.cached is False


class TestInputGenerationService:
    """Test cases for Input Generation Service functionality."""
    
    def setup_method(self):
        """Set up test fixtures."""
        self.mock_submission_repo = AsyncMock()
        self.service = InputGenerationService(
            submission_repository=self.mock_submission_repo,
            script_timeout_seconds=5,
            max_output_size=1024 * 1024  # 1MB
        )
        
        # Sample challenge
        self.sample_challenge = Mock(
            id=uuid4(),
            generation_script='print(json.dumps({"input": [1, 2, 3], "expected": "6"}))',
            script_updated_at=datetime.now(timezone.utc)
        )
        
        # Sample participant
        self.participant_id = "player123"
        self.participant_type = "player"
    
    async def test_generate_input_success_new_generation(self):
        """Test successful input generation without cache."""
        # Mock no existing cache
        self.mock_submission_repo.get_input_cache.return_value = None
        
        # Mock cache creation
        mock_cache = Mock(
            input_json={"input": [1, 2, 3]},
            expected_result="6"
        )
        self.mock_submission_repo.create_or_get_input_cache.return_value = mock_cache
        
        # Act
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = Mock(
                stdout='{"input": [1, 2, 3], "expected": "6"}',
                stderr='',
                returncode=0
            )
            
            result = await self.service.generate_input(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type=self.participant_type
            )
        
        # Assert
        assert result.status == InputGenerationStatus.SUCCESS
        assert result.input_data == [1, 2, 3]  # The "input" field content, not the whole JSON
        assert result.expected_result == "6"
        assert result.cached is False
        assert result.execution_time_ms >= 0  # Mocked execution can be 0ms
        
        # Verify cache creation was called
        self.mock_submission_repo.create_or_get_input_cache.assert_called_once()
    
    async def test_generate_input_success_from_cache(self):
        """Test successful input generation from cache."""
        # Mock existing valid cache
        mock_cache = Mock(
            input_json={"cached": True, "value": 42},
            expected_result="cached_result",
            is_valid=True
        )
        self.mock_submission_repo.get_input_cache.return_value = mock_cache
        
        # Act
        result = await self.service.generate_input(
            challenge=self.sample_challenge,
            participant_id=self.participant_id,
            participant_type=self.participant_type
        )
        
        # Assert
        assert result.status == InputGenerationStatus.SUCCESS
        assert result.input_data == {"cached": True, "value": 42}
        assert result.expected_result == "cached_result"
        assert result.cached is True
        assert result.execution_time_ms == 0
        
        # Verify no new cache creation
        self.mock_submission_repo.create_or_get_input_cache.assert_not_called()
    
    async def test_generate_input_script_timeout(self):
        """Test input generation with script timeout."""
        # Mock no existing cache
        self.mock_submission_repo.get_input_cache.return_value = None
        
        # Act
        with patch('subprocess.run') as mock_subprocess:
            # Simulate timeout
            import subprocess
            mock_subprocess.side_effect = subprocess.TimeoutExpired("python", 5)
            
            result = await self.service.generate_input(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type=self.participant_type
            )
        
        # Assert
        assert result.status == InputGenerationStatus.TIMEOUT
        assert result.input_data is None
        assert result.expected_result is None
        assert "Script execution timed out" in result.error_message
        assert result.cached is False
    
    async def test_generate_input_script_error(self):
        """Test input generation with script execution error."""
        # Mock no existing cache
        self.mock_submission_repo.get_input_cache.return_value = None
        
        # Act
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = Mock(
                stdout='',
                stderr='NameError: name "undefined_var" is not defined',
                returncode=1
            )
            
            result = await self.service.generate_input(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type=self.participant_type
            )
        
        # Assert
        assert result.status == InputGenerationStatus.ERROR
        assert result.input_data is None
        assert result.expected_result is None
        assert "Script execution failed" in result.error_message
        assert result.cached is False
    
    async def test_generate_input_invalid_json_output(self):
        """Test input generation with invalid JSON output."""
        # Mock no existing cache
        self.mock_submission_repo.get_input_cache.return_value = None
        
        # Act
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = Mock(
                stdout='This is not valid JSON',
                stderr='',
                returncode=0
            )
            
            result = await self.service.generate_input(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type=self.participant_type
            )
        
        # Assert
        assert result.status == InputGenerationStatus.ERROR
        assert result.input_data is None
        assert result.expected_result is None
        assert "Invalid JSON output" in result.error_message
        assert result.cached is False
    
    async def test_generate_input_missing_required_fields(self):
        """Test input generation with missing required fields in output."""
        # Mock no existing cache
        self.mock_submission_repo.get_input_cache.return_value = None
        
        # Act
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = Mock(
                stdout='{"input": [1, 2, 3]}',  # Missing "expected" field
                stderr='',
                returncode=0
            )
            
            result = await self.service.generate_input(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type=self.participant_type
            )
        
        # Assert
        assert result.status == InputGenerationStatus.ERROR
        assert result.input_data is None
        assert result.expected_result is None
        assert "Missing required field 'expected'" in result.error_message
        assert result.cached is False
    
    async def test_execute_generation_script_success(self):
        """Test successful script execution."""
        script = 'import json; print(json.dumps({"input": 42, "expected": "42"}))'
        
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = Mock(
                stdout='{"input": 42, "expected": "42"}',
                stderr='',
                returncode=0
            )
            
            output = await self.service._execute_generation_script(script)
        
        assert output == '{"input": 42, "expected": "42"}'
    
    async def test_execute_generation_script_with_imports(self):
        """Test script execution with common imports available."""
        script = '''
import json
import random
import math
data = {"value": math.pi, "random": random.randint(1, 10)}
print(json.dumps(data))
'''
        
        with patch('subprocess.run') as mock_subprocess:
            mock_subprocess.return_value = Mock(
                stdout='{"value": 3.141592653589793, "random": 5}',
                stderr='',
                returncode=0
            )
            
            output = await self.service._execute_generation_script(script)
        
        assert '"value": 3.141592653589793' in output
        assert '"random":' in output
    
    async def test_validate_script_output_success(self):
        """Test successful script output validation."""
        output = '{"input": [1, 2, 3], "expected": "sum is 6"}'
        
        input_data, expected_result = self.service._validate_script_output(output)
        
        assert input_data == [1, 2, 3]
        assert expected_result == "sum is 6"
    
    async def test_validate_script_output_complex_data(self):
        """Test script output validation with complex data structures."""
        output = '''
        {
            "input": {
                "matrix": [[1, 2], [3, 4]], 
                "params": {"n": 100, "mode": "strict"}
            },
            "expected": "determinant: -2"
        }
        '''
        
        input_data, expected_result = self.service._validate_script_output(output)
        
        assert input_data == {
            "matrix": [[1, 2], [3, 4]],
            "params": {"n": 100, "mode": "strict"}
        }
        assert expected_result == "determinant: -2"
    
    async def test_invalidate_cache_for_challenge(self):
        """Test cache invalidation for a challenge."""
        challenge_id = self.sample_challenge.id
        
        # Mock repository method
        self.mock_submission_repo.invalidate_input_cache.return_value = 5
        
        # Act
        invalidated_count = await self.service.invalidate_cache_for_challenge(challenge_id)
        
        # Assert
        assert invalidated_count == 5
        self.mock_submission_repo.invalidate_input_cache.assert_called_once_with(challenge_id=challenge_id)
    
    async def test_invalidate_cache_for_participant(self):
        """Test cache invalidation for a specific participant."""
        challenge_id = self.sample_challenge.id
        
        # Mock repository method
        self.mock_submission_repo.invalidate_input_cache.return_value = 1
        
        # Act
        invalidated_count = await self.service.invalidate_cache_for_participant(
            challenge_id=challenge_id,
            participant_id=self.participant_id,
            participant_type=self.participant_type
        )
        
        # Assert
        assert invalidated_count == 1
        self.mock_submission_repo.invalidate_input_cache.assert_called_once_with(
            challenge_id=challenge_id,
            participant_id=self.participant_id,
            participant_type=self.participant_type
        )
    
    async def test_service_validation_errors(self):
        """Test service input validation."""
        # Test None challenge
        with pytest.raises(ValueError, match="Challenge cannot be None"):
            await self.service.generate_input(
                challenge=None,
                participant_id=self.participant_id,
                participant_type=self.participant_type
            )
        
        # Test empty participant_id
        with pytest.raises(ValueError, match="Participant ID cannot be empty"):
            await self.service.generate_input(
                challenge=self.sample_challenge,
                participant_id="",
                participant_type=self.participant_type
            )
        
        # Test invalid participant_type
        with pytest.raises(ValueError, match="Participant type must be"):
            await self.service.generate_input(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type="invalid"
            )
    
    async def test_service_with_custom_timeout(self):
        """Test service with custom timeout configuration."""
        custom_service = InputGenerationService(
            submission_repository=self.mock_submission_repo,
            script_timeout_seconds=1  # Very short timeout
        )
        
        # Mock no existing cache
        self.mock_submission_repo.get_input_cache.return_value = None
        
        # Act with a script that would normally succeed but times out
        with patch('subprocess.run') as mock_subprocess:
            import subprocess
            mock_subprocess.side_effect = subprocess.TimeoutExpired("python", 1)
            
            result = await custom_service.generate_input(
                challenge=self.sample_challenge,
                participant_id=self.participant_id,
                participant_type=self.participant_type
            )
        
        # Assert
        assert result.status == InputGenerationStatus.TIMEOUT
    
    async def test_get_cached_input_exists(self):
        """Test getting existing cached input."""
        mock_cache = Mock(
            input_json={"cached_data": True},
            expected_result="cached_result"
        )
        self.mock_submission_repo.get_input_cache.return_value = mock_cache
        
        # Act
        cached_input = await self.service.get_cached_input(
            challenge_id=self.sample_challenge.id,
            participant_id=self.participant_id,
            participant_type=self.participant_type
        )
        
        # Assert
        assert cached_input is not None
        assert cached_input.input_json == {"cached_data": True}
        assert cached_input.expected_result == "cached_result"
    
    async def test_get_cached_input_not_exists(self):
        """Test getting cached input when none exists."""
        self.mock_submission_repo.get_input_cache.return_value = None
        
        # Act
        cached_input = await self.service.get_cached_input(
            challenge_id=self.sample_challenge.id,
            participant_id=self.participant_id,
            participant_type=self.participant_type
        )
        
        # Assert
        assert cached_input is None


class TestInputGenerationExceptions:
    """Test cases for input generation exceptions."""
    
    def test_script_execution_error(self):
        """Test ScriptExecutionError exception."""
        error = ScriptExecutionError("Script failed", "NameError: undefined")
        
        assert str(error) == "Script failed"
        assert error.stderr_output == "NameError: undefined"
    
    def test_script_timeout_error(self):
        """Test ScriptTimeoutError exception."""
        error = ScriptTimeoutError(5)
        
        assert "5 seconds" in str(error)
        assert error.timeout_seconds == 5
    
    def test_script_validation_error(self):
        """Test ScriptValidationError exception."""
        error = ScriptValidationError("Invalid JSON output")
        
        assert str(error) == "Invalid JSON output"


class TestInputGenerationStatus:
    """Test cases for InputGenerationStatus enum."""
    
    def test_input_generation_status_values(self):
        """Test InputGenerationStatus enum values."""
        assert InputGenerationStatus.SUCCESS.value == "success"
        assert InputGenerationStatus.ERROR.value == "error"
        assert InputGenerationStatus.TIMEOUT.value == "timeout"
    
    def test_input_generation_status_comparison(self):
        """Test InputGenerationStatus comparison."""
        assert InputGenerationStatus.SUCCESS != InputGenerationStatus.ERROR
        assert InputGenerationStatus.ERROR != InputGenerationStatus.TIMEOUT
        assert InputGenerationStatus.SUCCESS.value == "success"