"""Performance tests for streak system.

These tests ensure the streak system meets performance requirements
for a 14k member Discord server, including latency, throughput, and
memory usage under various load conditions.
"""

from __future__ import annotations

import asyncio
import time
import psutil
import gc
from datetime import date, timedelta
from typing import Dict, Any, List
from unittest.mock import Mock

import pytest

from smarter_dev.bot.services.streak_service import StreakService
from smarter_dev.shared.date_provider import MockDateProvider


class TestStreakServicePerformance:
    """Performance tests for StreakService core calculations."""
    
    @pytest.fixture
    def streak_service(self) -> StreakService:
        """Create StreakService with mock date provider."""
        mock_provider = MockDateProvider(fixed_date=date(2024, 1, 15))
        return StreakService(date_provider=mock_provider)
    
    @pytest.fixture
    def complex_bonuses(self) -> Dict[str, int]:
        """Complex streak bonus configuration for performance testing."""
        return {
            "3": 1.2, "8": 1.5, "16": 2, "21": 2.5, "32": 3,
            "45": 3.5, "64": 4, "90": 5, "120": 6, "180": 8, "365": 10
        }
    
    def test_streak_calculation_performance_high_counts(self, streak_service: StreakService):
        """Test streak calculation performance with very high streak counts."""
        bonuses = {"365": 10, "1000": 20, "5000": 50}
        
        # Test various high streak counts
        test_streaks = [100, 500, 1000, 2500, 5000, 10000]
        
        start_time = time.perf_counter()
        
        for streak_count in test_streaks:
            for _ in range(100):  # 100 calculations per streak count
                bonus = streak_service.calculate_streak_bonus(streak_count, bonuses)
                assert bonus > 0, f"Invalid bonus for streak {streak_count}"
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Should handle 600 calculations (6 streaks * 100 iterations) in < 100ms
        assert total_time < 0.1, f"Streak bonus calculation too slow: {total_time:.3f}s"
        
        # Calculate operations per second
        operations = len(test_streaks) * 100
        ops_per_second = operations / total_time
        
        # Should achieve at least 10,000 ops/second
        assert ops_per_second > 10000, f"Too slow: {ops_per_second:.0f} ops/second"
        
        print(f"Streak bonus performance: {ops_per_second:.0f} ops/second")
    
    def test_complete_streak_result_performance(
        self, 
        streak_service: StreakService, 
        complex_bonuses: Dict[str, int]
    ):
        """Test complete streak result calculation performance."""
        # Test data variations
        test_scenarios = [
            (None, 0),                           # New user
            (date(2024, 1, 14), 5),             # Continuing streak
            (date(2024, 1, 12), 10),            # Broken streak
            (date(2024, 1, 14), 30),            # High streak bonus
            (date(2024, 1, 14), 365),           # Very high streak
        ]
        
        daily_amount = 25
        iterations = 1000
        
        start_time = time.perf_counter()
        
        for last_daily, current_streak in test_scenarios:
            for _ in range(iterations):
                result = streak_service.calculate_streak_result(
                    last_daily=last_daily,
                    current_streak=current_streak,
                    daily_amount=daily_amount,
                    streak_bonuses=complex_bonuses
                )
                assert result.new_streak_count > 0
                assert result.reward_amount > 0
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Should handle 5000 complete calculations in < 500ms
        total_operations = len(test_scenarios) * iterations
        assert total_time < 0.5, f"Complete streak calculation too slow: {total_time:.3f}s"
        
        ops_per_second = total_operations / total_time
        print(f"Complete streak calculation performance: {ops_per_second:.0f} ops/second")
    
    def test_memory_usage_large_scale_operations(
        self, 
        streak_service: StreakService
    ):
        """Test memory usage during large-scale operations."""
        # Measure initial memory
        gc.collect()  # Clean up first
        process = psutil.Process()
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB
        
        # Perform many streak calculations
        bonuses = {"8": 2, "16": 3, "32": 5, "64": 10, "365": 20}
        iterations = 10000
        
        results = []
        for i in range(iterations):
            last_daily = date(2024, 1, 14) if i % 2 == 0 else None
            current_streak = i % 100  # Vary streak count
            
            result = streak_service.calculate_streak_result(
                last_daily=last_daily,
                current_streak=current_streak,
                daily_amount=10,
                streak_bonuses=bonuses
            )
            
            # Store some results to prevent optimization
            if i % 1000 == 0:
                results.append(result)
        
        # Measure final memory
        final_memory = process.memory_info().rss / 1024 / 1024  # MB
        memory_increase = final_memory - initial_memory
        
        # Memory increase should be minimal (< 10MB for 10k operations)
        assert memory_increase < 10, f"Excessive memory usage: {memory_increase:.2f}MB increase"
        
        print(f"Memory usage for {iterations} operations: {memory_increase:.2f}MB increase")
    
    def test_streak_validation_performance(self, streak_service: StreakService):
        """Test performance of streak data validation."""
        # Generate test data
        test_cases = []
        base_date = date(2024, 1, 15)
        
        for i in range(1000):
            # Mix of valid and invalid data
            if i % 4 == 0:
                # Valid data
                last_daily = base_date - timedelta(days=1)
                streak_count = 5
            elif i % 4 == 1:
                # Invalid: negative streak
                last_daily = base_date - timedelta(days=1)
                streak_count = -1
            elif i % 4 == 2:
                # Invalid: future date
                last_daily = base_date + timedelta(days=1)
                streak_count = 5
            else:
                # Invalid: never claimed but has streak
                last_daily = None
                streak_count = 5
            
            test_cases.append((last_daily, streak_count))
        
        start_time = time.perf_counter()
        
        for last_daily, streak_count in test_cases:
            is_valid = streak_service.validate_streak_data(
                last_daily, streak_count, base_date
            )
            # Just verify it returns a boolean
            assert isinstance(is_valid, bool)
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Should validate 1000 records in < 50ms
        assert total_time < 0.05, f"Validation too slow: {total_time:.3f}s"
        
        ops_per_second = len(test_cases) / total_time
        print(f"Validation performance: {ops_per_second:.0f} validations/second")


class TestAPIPerformanceBenchmarks:
    """Performance benchmarks for API endpoints."""
    
    @pytest.mark.slow
    def test_daily_claim_latency_target(self):
        """Test that daily claim API meets latency requirements.
        
        Target: < 100ms end-to-end latency for daily claim.
        This test uses mocks to simulate the API without actual HTTP overhead.
        """
        from smarter_dev.bot.services.streak_service import StreakService
        from smarter_dev.shared.date_provider import MockDateProvider
        
        # Setup
        mock_provider = MockDateProvider(fixed_date=date(2024, 1, 15))
        streak_service = StreakService(date_provider=mock_provider)
        
        # Simulate API workflow
        iterations = 100
        total_time = 0
        
        for i in range(iterations):
            start_time = time.perf_counter()
            
            # Simulate the key operations in daily claim API
            # 1. Get balance (simulated)
            last_daily = date(2024, 1, 14) if i % 2 == 0 else None
            current_streak = i % 20
            
            # 2. Calculate streak result
            result = streak_service.calculate_streak_result(
                last_daily=last_daily,
                current_streak=current_streak,
                daily_amount=10,
                streak_bonuses={"8": 2, "16": 3, "32": 5}
            )
            
            # 3. Simulate database update (mock timing)
            await_simulation = 0.001  # 1ms for DB operation
            
            end_time = time.perf_counter()
            operation_time = (end_time - start_time) + await_simulation
            total_time += operation_time
        
        avg_latency = (total_time / iterations) * 1000  # Convert to ms
        
        # Target: < 10ms for core business logic (excluding network/DB I/O)
        assert avg_latency < 10, f"Average latency too high: {avg_latency:.2f}ms"
        
        print(f"Daily claim core logic latency: {avg_latency:.2f}ms average")
    
    def test_bulk_operations_throughput(self):
        """Test throughput for bulk streak operations.
        
        Simulates processing daily claims for many users simultaneously.
        Target: Handle 1000 users in < 5 seconds.
        """
        from smarter_dev.bot.services.streak_service import StreakService
        from smarter_dev.shared.date_provider import MockDateProvider
        
        # Setup
        mock_provider = MockDateProvider(fixed_date=date(2024, 1, 15))
        streak_service = StreakService(date_provider=mock_provider)
        
        # Simulate 1000 users with varying streak states
        user_count = 1000
        users_data = []
        
        for i in range(user_count):
            # Vary the data to test different scenarios
            if i % 3 == 0:
                last_daily = None  # New user
                current_streak = 0
            elif i % 3 == 1:
                last_daily = date(2024, 1, 14)  # Continuing streak
                current_streak = i % 30
            else:
                last_daily = date(2024, 1, 12)  # Broken streak
                current_streak = i % 50
            
            users_data.append((last_daily, current_streak))
        
        start_time = time.perf_counter()
        
        results = []
        for last_daily, current_streak in users_data:
            result = streak_service.calculate_streak_result(
                last_daily=last_daily,
                current_streak=current_streak,
                daily_amount=15,
                streak_bonuses={"8": 2, "16": 3, "32": 5, "64": 10}
            )
            results.append(result)
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        # Target: Process 1000 users in < 1 second
        assert total_time < 1.0, f"Bulk processing too slow: {total_time:.3f}s for {user_count} users"
        
        users_per_second = user_count / total_time
        print(f"Bulk processing throughput: {users_per_second:.0f} users/second")
        
        # Verify all results are valid
        assert len(results) == user_count
        for result in results:
            assert result.new_streak_count > 0
            assert result.reward_amount > 0
    
    def test_complex_bonus_configuration_performance(self):
        """Test performance with complex bonus configurations.
        
        Some guilds might have very detailed bonus configurations.
        """
        from smarter_dev.bot.services.streak_service import StreakService
        
        # Create very complex bonus configuration
        complex_bonuses = {}
        for day in range(1, 366):  # Daily bonuses for a full year
            if day % 7 == 0:  # Weekly bonuses
                complex_bonuses[str(day)] = 1 + (day // 7) * 0.1
        
        # Add special milestone bonuses
        milestones = [30, 60, 90, 120, 180, 270, 365, 500, 1000]
        for milestone in milestones:
            complex_bonuses[str(milestone)] = milestone // 10
        
        print(f"Testing with {len(complex_bonuses)} bonus tiers")
        
        streak_service = StreakService()
        
        # Test performance with complex configuration
        test_streaks = [1, 7, 14, 30, 60, 90, 180, 365, 500, 1000, 2000]
        iterations = 1000
        
        start_time = time.perf_counter()
        
        for _ in range(iterations):
            for streak_count in test_streaks:
                bonus = streak_service.calculate_streak_bonus(streak_count, complex_bonuses)
                assert bonus >= 1, f"Invalid bonus for streak {streak_count}"
        
        end_time = time.perf_counter()
        total_time = end_time - start_time
        
        total_operations = iterations * len(test_streaks)
        ops_per_second = total_operations / total_time
        
        # Should still achieve good performance with complex config
        assert ops_per_second > 5000, f"Complex config too slow: {ops_per_second:.0f} ops/second"
        
        print(f"Complex bonus config performance: {ops_per_second:.0f} ops/second")


class TestScalabilityBenchmarks:
    """Scalability tests for 14k member Discord server simulation."""
    
    @pytest.mark.slow
    def test_daily_reset_simulation_14k_users(self):
        """Simulate daily reset processing for 14k member server.
        
        This test simulates the worst-case scenario where all 14k members
        claim their daily reward within a short time window.
        """
        from smarter_dev.bot.services.streak_service import StreakService
        from smarter_dev.shared.date_provider import MockDateProvider
        
        # Setup
        mock_provider = MockDateProvider(fixed_date=date(2024, 1, 15))
        streak_service = StreakService(date_provider=mock_provider)
        
        # Simulate 14k users (use 1400 for test performance, scale by 10x)
        user_count = 1400  # 10% of 14k for test performance
        scale_factor = 10
        
        print(f"Simulating {user_count * scale_factor} users ({user_count} actual calculations)")
        
        # Generate realistic user distribution
        users_data = []
        for i in range(user_count):
            # Realistic distribution of streak states
            rand_val = i % 100
            
            if rand_val < 20:  # 20% new/returning users
                last_daily = None
                current_streak = 0
            elif rand_val < 70:  # 50% active users (1-30 day streaks)
                last_daily = date(2024, 1, 14)
                current_streak = (i % 30) + 1
            elif rand_val < 90:  # 20% dedicated users (30-90 day streaks)
                last_daily = date(2024, 1, 14)
                current_streak = 30 + (i % 60)
            else:  # 10% super users (90+ day streaks)
                last_daily = date(2024, 1, 14)
                current_streak = 90 + (i % 275)  # Up to 365 days
            
            users_data.append((last_daily, current_streak))
        
        # Realistic bonus configuration
        bonuses = {"8": 2, "16": 3, "32": 5, "64": 8, "90": 12, "180": 20, "365": 50}
        
        start_time = time.perf_counter()
        
        # Process all users
        results = []
        total_rewards = 0
        
        for last_daily, current_streak in users_data:
            result = streak_service.calculate_streak_result(
                last_daily=last_daily,
                current_streak=current_streak,
                daily_amount=20,  # Realistic daily amount
                streak_bonuses=bonuses
            )
            results.append(result)
            total_rewards += result.reward_amount
        
        end_time = time.perf_counter()
        processing_time = end_time - start_time
        
        # Scale results to full 14k
        scaled_time = processing_time * scale_factor
        scaled_rewards = total_rewards * scale_factor
        
        # Performance targets for 14k users
        assert scaled_time < 30, f"14k user processing too slow: {scaled_time:.2f}s (target: <30s)"
        
        users_per_second = (user_count * scale_factor) / scaled_time
        
        print(f"14k user simulation results:")
        print(f"  Processing time: {scaled_time:.2f}s")
        print(f"  Throughput: {users_per_second:.0f} users/second")
        print(f"  Total rewards distributed: {scaled_rewards:,}")
        print(f"  Average reward per user: {scaled_rewards / (user_count * scale_factor):.1f}")
        
        # Verify reasonable distribution
        streak_counts = [r.new_streak_count for r in results]
        avg_streak = sum(streak_counts) / len(streak_counts)
        max_streak = max(streak_counts)
        
        print(f"  Average streak: {avg_streak:.1f} days")
        print(f"  Maximum streak: {max_streak} days")
        
        assert 5 <= avg_streak <= 50, f"Unrealistic average streak: {avg_streak}"
        assert max_streak <= 366, f"Impossible streak count: {max_streak}"
    
    @pytest.mark.slow
    def test_memory_efficiency_large_scale(self):
        """Test memory efficiency with large-scale operations."""
        from smarter_dev.bot.services.streak_service import StreakService
        
        # Monitor memory usage
        process = psutil.Process()
        
        def get_memory_mb():
            return process.memory_info().rss / 1024 / 1024
        
        initial_memory = get_memory_mb()
        
        # Create multiple service instances (simulating multi-guild usage)
        services = []
        for _ in range(100):  # 100 guilds
            service = StreakService()
            services.append(service)
        
        mid_memory = get_memory_mb()
        
        # Perform operations with all services
        bonus_configs = [
            {"8": 2, "16": 3},
            {"5": 1.5, "10": 2, "20": 3, "32": 5},
            {"3": 1.2, "8": 1.5, "16": 2, "32": 4, "64": 8},
        ]
        
        for i, service in enumerate(services):
            bonuses = bonus_configs[i % len(bonus_configs)]
            
            # 100 operations per service
            for j in range(100):
                last_daily = date(2024, 1, 14) if j % 2 == 0 else None
                streak = j % 30
                
                result = service.calculate_streak_result(
                    last_daily=last_daily,
                    current_streak=streak,
                    daily_amount=15,
                    streak_bonuses=bonuses
                )
                
                # Verify result to prevent optimization
                assert result.new_streak_count > 0
        
        final_memory = get_memory_mb()
        
        # Memory increases
        service_creation_memory = mid_memory - initial_memory
        operation_memory = final_memory - mid_memory
        total_memory_increase = final_memory - initial_memory
        
        print(f"Memory efficiency test:")
        print(f"  Initial memory: {initial_memory:.1f}MB")
        print(f"  After creating 100 services: {mid_memory:.1f}MB (+{service_creation_memory:.1f}MB)")
        print(f"  After 10k operations: {final_memory:.1f}MB (+{operation_memory:.1f}MB)")
        print(f"  Total increase: {total_memory_increase:.1f}MB")
        
        # Memory efficiency targets
        assert service_creation_memory < 5, f"Service creation too memory intensive: {service_creation_memory:.1f}MB"
        assert operation_memory < 10, f"Operations too memory intensive: {operation_memory:.1f}MB"
        assert total_memory_increase < 15, f"Total memory increase too high: {total_memory_increase:.1f}MB"
    
    def test_concurrent_processing_performance(self):
        """Test performance under concurrent processing load."""
        import asyncio
        from smarter_dev.bot.services.streak_service import StreakService
        
        async def process_user_batch(batch_size: int, service: StreakService) -> float:
            """Process a batch of users and return processing time."""
            start_time = time.perf_counter()
            
            tasks = []
            for i in range(batch_size):
                # Create async task for each user (simulating concurrent processing)
                async def process_user(user_index):
                    last_daily = date(2024, 1, 14) if user_index % 2 == 0 else None
                    streak = user_index % 25
                    
                    return service.calculate_streak_result(
                        last_daily=last_daily,
                        current_streak=streak,
                        daily_amount=12,
                        streak_bonuses={"8": 2, "16": 3, "32": 5}
                    )
                
                tasks.append(process_user(i))
            
            # Process all users concurrently
            results = await asyncio.gather(*tasks)
            
            end_time = time.perf_counter()
            
            # Verify all results
            assert len(results) == batch_size
            for result in results:
                assert result.new_streak_count > 0
            
            return end_time - start_time
        
        async def run_concurrent_test():
            service = StreakService()
            
            # Test different batch sizes
            batch_sizes = [100, 500, 1000]
            
            for batch_size in batch_sizes:
                processing_time = await process_user_batch(batch_size, service)
                
                users_per_second = batch_size / processing_time
                
                print(f"Concurrent processing - {batch_size} users: {processing_time:.3f}s ({users_per_second:.0f} users/s)")
                
                # Performance targets
                assert users_per_second > 1000, f"Concurrent processing too slow for batch {batch_size}: {users_per_second:.0f} users/s"
        
        # Run the async test
        asyncio.run(run_concurrent_test())