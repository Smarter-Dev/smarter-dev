"""
Metrics tracker for evaluating mention agent model performance.

Tracks quality, cost, latency, and tool usage accuracy metrics across different LLM models.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime


@dataclass
class TokenUsage:
    """Token usage information for a model run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self.input_tokens + self.output_tokens

    def estimate_cost(self, model_name: str) -> float:
        """
        Estimate cost based on model pricing.

        Prices as of January 2025 (per million tokens):
        - Gemini 2.5 Flash: $0.075 input, $0.30 output
        - Gemini 2.5 Flash Lite: $0.05 input, $0.20 output
        - Claude Haiku 4.5: $1.00 input, $5.00 output
        - Claude Sonnet 4.5: $3.00 input, $15.00 output
        """
        pricing = {
            "gemini-2.5-flash": (0.075, 0.30),
            "gemini-2.5-flash-lite": (0.05, 0.20),
            "claude-haiku-4-5": (1.00, 5.00),
            "claude-sonnet-4-5": (3.00, 15.00),
            # Legacy model names (for backward compatibility)
            "claude-4.5-haiku": (1.00, 5.00),
            "claude-4.5-sonnet": (3.00, 15.00),
        }

        # Normalize model name
        model_key = model_name.lower()
        for key in pricing.keys():
            if key in model_key:
                input_price, output_price = pricing[key]
                break
        else:
            # Default to highest price if unknown
            input_price, output_price = (3.00, 15.00)

        input_cost = (self.input_tokens / 1_000_000) * input_price
        output_cost = (self.output_tokens / 1_000_000) * output_price

        return input_cost + output_cost


@dataclass
class LatencyMetrics:
    """Latency metrics for a model run."""

    start_time: float = field(default_factory=time.time)
    first_action_time: Optional[float] = None
    completion_time: Optional[float] = None

    @property
    def time_to_first_action(self) -> Optional[float]:
        """Time in seconds until first tool call or response."""
        if self.first_action_time is None:
            return None
        return self.first_action_time - self.start_time

    @property
    def total_time(self) -> Optional[float]:
        """Total time in seconds to completion."""
        if self.completion_time is None:
            return None
        return self.completion_time - self.start_time

    def mark_first_action(self):
        """Mark the time of the first action."""
        if self.first_action_time is None:
            self.first_action_time = time.time()

    def mark_completion(self):
        """Mark the completion time."""
        self.completion_time = time.time()


@dataclass
class ToolUsageMetrics:
    """Tool usage accuracy metrics."""

    expected_tools: List[str] = field(default_factory=list)
    actual_tools: List[str] = field(default_factory=list)
    expected_sequence: List[str] = field(default_factory=list)
    actual_sequence: List[str] = field(default_factory=list)

    @property
    def precision(self) -> float:
        """
        Precision: Of the tools used, how many were expected?
        """
        if not self.actual_tools:
            return 1.0 if not self.expected_tools else 0.0

        expected_set = set(self.expected_tools)
        actual_set = set(self.actual_tools)

        correct = len(actual_set & expected_set)
        return correct / len(actual_set)

    @property
    def recall(self) -> float:
        """
        Recall: Of the expected tools, how many were used?
        """
        if not self.expected_tools:
            return 1.0 if not self.actual_tools else 0.0

        expected_set = set(self.expected_tools)
        actual_set = set(self.actual_tools)

        correct = len(expected_set & actual_set)
        return correct / len(expected_set)

    @property
    def f1_score(self) -> float:
        """F1 score (harmonic mean of precision and recall)."""
        p = self.precision
        r = self.recall

        if p + r == 0:
            return 0.0

        return 2 * (p * r) / (p + r)

    @property
    def sequence_accuracy(self) -> float:
        """
        Sequence accuracy: How well does the actual sequence match expected?
        Uses longest common subsequence.
        """
        if not self.expected_sequence:
            return 1.0 if not self.actual_sequence else 0.0

        if not self.actual_sequence:
            return 0.0

        # Simple sequence match - check if actual is a valid subsequence
        expected = self.expected_sequence
        actual = self.actual_sequence

        # If sequences are identical, perfect score
        if expected == actual:
            return 1.0

        # Otherwise, calculate ratio of matching elements in order
        matches = 0
        exp_idx = 0

        for tool in actual:
            if exp_idx < len(expected) and tool == expected[exp_idx]:
                matches += 1
                exp_idx += 1

        return matches / max(len(expected), len(actual))


@dataclass
class QualityMetrics:
    """Quality metrics from LLM-as-judge evaluation."""

    appropriateness: int = 0  # 1-10
    quality: int = 0  # 1-10
    community_tone: int = 0  # 1-10
    contextual_awareness: int = 0  # 1-10
    effectiveness: int = 0  # 1-10

    judge_reasoning: str = ""

    @property
    def average_score(self) -> float:
        """Average of all quality metrics."""
        scores = [
            self.appropriateness,
            self.quality,
            self.community_tone,
            self.contextual_awareness,
            self.effectiveness,
        ]
        return sum(scores) / len(scores)

    @property
    def weighted_score(self) -> float:
        """
        Weighted score emphasizing appropriateness and effectiveness.
        """
        return (
            self.appropriateness * 0.25 +
            self.quality * 0.20 +
            self.community_tone * 0.15 +
            self.contextual_awareness * 0.15 +
            self.effectiveness * 0.25
        )


@dataclass
class ModelRunMetrics:
    """Complete metrics for a single model run on a scenario."""

    model_name: str
    scenario_id: str
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    # Sub-metrics
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    latency: LatencyMetrics = field(default_factory=LatencyMetrics)
    tool_usage: ToolUsageMetrics = field(default_factory=ToolUsageMetrics)
    quality: QualityMetrics = field(default_factory=QualityMetrics)

    # Agent output
    agent_output: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def estimated_cost(self) -> float:
        """Estimated cost in USD."""
        return self.token_usage.estimate_cost(self.model_name)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "model_name": self.model_name,
            "scenario_id": self.scenario_id,
            "timestamp": self.timestamp,
            "error": self.error,
            "cost": {
                "input_tokens": self.token_usage.input_tokens,
                "output_tokens": self.token_usage.output_tokens,
                "total_tokens": self.token_usage.total_tokens,
                "estimated_cost_usd": self.estimated_cost,
            },
            "latency": {
                "time_to_first_action_seconds": self.latency.time_to_first_action,
                "total_time_seconds": self.latency.total_time,
            },
            "tool_usage": {
                "expected_tools": self.tool_usage.expected_tools,
                "actual_tools": self.tool_usage.actual_tools,
                "expected_sequence": self.tool_usage.expected_sequence,
                "actual_sequence": self.tool_usage.actual_sequence,
                "precision": self.tool_usage.precision,
                "recall": self.tool_usage.recall,
                "f1_score": self.tool_usage.f1_score,
                "sequence_accuracy": self.tool_usage.sequence_accuracy,
            },
            "quality": {
                "appropriateness": self.quality.appropriateness,
                "quality": self.quality.quality,
                "community_tone": self.quality.community_tone,
                "contextual_awareness": self.quality.contextual_awareness,
                "effectiveness": self.quality.effectiveness,
                "average_score": self.quality.average_score,
                "weighted_score": self.quality.weighted_score,
                "judge_reasoning": self.quality.judge_reasoning,
            },
            "agent_output": self.agent_output,
        }


class MetricsAggregator:
    """Aggregates metrics across multiple runs for comparison."""

    def __init__(self):
        self.runs: List[ModelRunMetrics] = []

    def add_run(self, run: ModelRunMetrics):
        """Add a run to the aggregator."""
        self.runs.append(run)

    def get_model_stats(self, model_name: str) -> Dict[str, Any]:
        """Get aggregated statistics for a specific model."""
        model_runs = [r for r in self.runs if r.model_name == model_name]

        if not model_runs:
            return {}

        successful_runs = [r for r in model_runs if r.error is None]

        if not successful_runs:
            return {
                "model_name": model_name,
                "total_runs": len(model_runs),
                "successful_runs": 0,
                "error_rate": 1.0,
            }

        return {
            "model_name": model_name,
            "total_runs": len(model_runs),
            "successful_runs": len(successful_runs),
            "error_rate": 1 - (len(successful_runs) / len(model_runs)),
            "avg_cost_usd": sum(r.estimated_cost for r in successful_runs) / len(successful_runs),
            "total_cost_usd": sum(r.estimated_cost for r in successful_runs),
            "avg_tokens": sum(r.token_usage.total_tokens for r in successful_runs) / len(successful_runs),
            "avg_time_to_first_action": sum(
                r.latency.time_to_first_action for r in successful_runs
                if r.latency.time_to_first_action is not None
            ) / len([r for r in successful_runs if r.latency.time_to_first_action is not None]),
            "avg_total_time": sum(
                r.latency.total_time for r in successful_runs
                if r.latency.total_time is not None
            ) / len([r for r in successful_runs if r.latency.total_time is not None]),
            "avg_tool_precision": sum(r.tool_usage.precision for r in successful_runs) / len(successful_runs),
            "avg_tool_recall": sum(r.tool_usage.recall for r in successful_runs) / len(successful_runs),
            "avg_tool_f1": sum(r.tool_usage.f1_score for r in successful_runs) / len(successful_runs),
            "avg_sequence_accuracy": sum(r.tool_usage.sequence_accuracy for r in successful_runs) / len(successful_runs),
            "avg_quality_score": sum(r.quality.average_score for r in successful_runs) / len(successful_runs),
            "avg_weighted_quality": sum(r.quality.weighted_score for r in successful_runs) / len(successful_runs),
            "quality_breakdown": {
                "appropriateness": sum(r.quality.appropriateness for r in successful_runs) / len(successful_runs),
                "quality": sum(r.quality.quality for r in successful_runs) / len(successful_runs),
                "community_tone": sum(r.quality.community_tone for r in successful_runs) / len(successful_runs),
                "contextual_awareness": sum(r.quality.contextual_awareness for r in successful_runs) / len(successful_runs),
                "effectiveness": sum(r.quality.effectiveness for r in successful_runs) / len(successful_runs),
            },
        }

    def compare_models(self) -> Dict[str, Any]:
        """Compare all models across all metrics."""
        model_names = list(set(r.model_name for r in self.runs))

        return {
            "models": {name: self.get_model_stats(name) for name in model_names},
            "total_runs": len(self.runs),
            "scenarios": list(set(r.scenario_id for r in self.runs)),
        }
