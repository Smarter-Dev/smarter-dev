"""
Model comparison runner for evaluating different LLMs on mention agent scenarios.

Runs the same scenarios through multiple models and collects comprehensive metrics.
"""

import asyncio
import logging
import os
import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import dspy

from smarter_dev.bot.agents.mention_agent import ConversationalMentionSignature
from smarter_dev.bot.agents.tools import create_mention_tools
from tests.evaluation.metrics_tracker import (
    ModelRunMetrics,
    TokenUsage,
    LatencyMetrics,
    ToolUsageMetrics,
    MetricsAggregator,
)
from tests.evaluation.llm_judge import LLMJudge

logger = logging.getLogger(__name__)


class MockBot:
    """Mock Discord bot for testing."""

    def __init__(self):
        self.rest = AsyncMock()
        self.cache = MagicMock()
        self._message_count = 0

    async def send_message(self, channel_id: str, content: str, reply_to: Optional[str] = None):
        """Mock send message - returns success."""
        self._message_count += 1
        logger.debug(f"Mock: Sent message #{self._message_count}: {content[:50]}...")
        return {"id": f"msg_{self._message_count}", "content": content}

    async def add_reaction(self, channel_id: str, message_id: str, emoji: str):
        """Mock add reaction - returns success."""
        logger.debug(f"Mock: Added reaction {emoji} to {message_id}")
        return True


class ToolCallTracker:
    """Tracks tool calls made during agent execution."""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []
        self.first_action_time: Optional[float] = None

    def track_call(self, tool_name: str, args: Dict[str, Any], result: Any):
        """Track a tool call."""
        import time

        if self.first_action_time is None:
            self.first_action_time = time.time()

        self.calls.append({
            "tool": tool_name,
            "args": args,
            "result": result,
            "timestamp": time.time(),
        })

    def get_tool_names(self) -> List[str]:
        """Get list of tool names used."""
        return [call["tool"] for call in self.calls]

    def get_tool_sequence(self) -> List[str]:
        """Get ordered sequence of tool names."""
        return self.get_tool_names()


def load_scenario(scenario_path: Path) -> Dict[str, Any]:
    """Load a scenario from a YAML file."""
    with open(scenario_path, "r") as f:
        return yaml.safe_load(f)


def load_all_scenarios(scenarios_dir: Path) -> List[Dict[str, Any]]:
    """Load all scenarios from a directory."""
    scenarios = []

    for yaml_file in scenarios_dir.glob("*.yaml"):
        scenario = load_scenario(yaml_file)
        scenario["_file"] = yaml_file.name
        scenarios.append(scenario)

    return sorted(scenarios, key=lambda s: s.get("id", ""))


def build_conversation_context(scenario: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build conversation context from scenario.

    Converts scenario format into the context format expected by the mention agent.
    """
    messages = scenario.get("conversation", [])

    # Build conversation timeline
    timeline_parts = []
    for msg in messages:
        timestamp = msg.get("timestamp", "12:00:00")
        user = msg.get("user", "Unknown")
        user_id = msg.get("user_id", "123456789")
        content = msg.get("content", "")

        timeline_parts.append(f"[{timestamp}] {user} (ID: {user_id}): {content}")

    conversation_timeline = "\n".join(timeline_parts)

    # Build users list
    users = []
    seen_users = set()

    for msg in messages:
        user_id = msg.get("user_id", "123456789")
        if user_id not in seen_users:
            users.append({
                "user_id": user_id,
                "discord_name": msg.get("user", "Unknown"),
                "nickname": msg.get("nickname", msg.get("user", "Unknown")),
                "roles": msg.get("roles", []),
                "is_bot": msg.get("is_bot", False),
            })
            seen_users.add(user_id)

    # Build channel info
    channel = {
        "name": scenario.get("channel_name", "general"),
        "description": scenario.get("channel_description", "General discussion"),
    }

    # Bot info
    me = {
        "bot_name": "SmarterDev",
        "bot_id": "987654321",
    }

    return {
        "conversation_timeline": conversation_timeline,
        "users": users,
        "channel": channel,
        "me": me,
        "recent_search_queries": scenario.get("recent_search_queries", []),
        "messages_remaining": scenario.get("messages_remaining", 50),
        "is_continuation": scenario.get("is_continuation", False),
    }


async def run_scenario_with_model(
    scenario: Dict[str, Any],
    model_name: str,
    tracker: ToolCallTracker,
) -> Dict[str, Any]:
    """
    Run a scenario with a specific model.

    Args:
        scenario: Scenario configuration
        model_name: Name of the model to use (e.g., "gemini/gemini-2.0-flash-exp")
        tracker: Tool call tracker

    Returns:
        Dictionary with agent output including thought process and actions
    """
    import time
    start_time = time.time()

    # Build context
    logger.debug(f"Building conversation context...")
    context = build_conversation_context(scenario)

    # Create mock bot
    logger.debug(f"Creating mock bot and tools...")
    bot = MockBot()

    # Create tools
    tools, _ = create_mention_tools(
        bot=bot,
        channel_id="123456789",
        guild_id="987654321",
        trigger_message_id="111111111",
    )
    logger.debug(f"Created {len(tools)} tools")

    # Mock web-based tools to prevent actual HTTP requests
    # This makes evaluation fast and doesn't require real API keys
    for tool in tools:
        if tool.name == "lookup_fact":
            original_func = tool.func
            async def mock_lookup_fact(query: str):
                logger.debug(f"Mock: lookup_fact({query})")
                return f"Mock answer for: {query}"
            tool.func = mock_lookup_fact
        elif tool.name == "search_web":
            original_func = tool.func
            async def mock_search(query: str, max_results: int = 3):
                logger.debug(f"Mock: search_web({query}, max_results={max_results})")
                return f"Mock search results for: {query}"
            tool.func = mock_search
        elif tool.name == "open_url":
            original_func = tool.func
            async def mock_open_url(url: str, question: str):
                logger.debug(f"Mock: open_url({url}, {question})")
                return f"Mock content from URL: {url}"
            tool.func = mock_open_url
        elif tool.name == "generate_engagement_plan":
            original_func = tool.func
            async def mock_plan():
                logger.debug(f"Mock: generate_engagement_plan()")
                return "Mock engagement plan: Respond naturally to the conversation."
            tool.func = mock_plan
        elif tool.name == "generate_in_depth_response":
            original_func = tool.func
            async def mock_generate(prompt_summary: str, prompt: str):
                logger.debug(f"Mock: generate_in_depth_response({prompt_summary})")
                return f"Mock technical response for: {prompt_summary}"
            tool.func = mock_generate
        elif tool.name == "wait_for_messages":
            original_func = tool.func
            async def mock_wait():
                logger.debug(f"Mock: wait_for_messages()")
                return "No new messages"
            tool.func = mock_wait
        elif tool.name == "fetch_new_messages":
            original_func = tool.func
            async def mock_fetch():
                logger.debug(f"Mock: fetch_new_messages()")
                return "No new messages"
            tool.func = mock_fetch
        elif tool.name == "stop_monitoring":
            original_func = tool.func
            async def mock_stop():
                logger.debug(f"Mock: stop_monitoring()")
                return "Stopped monitoring"
            tool.func = mock_stop

    logger.debug(f"Mocked web-based tools to prevent actual HTTP requests")

    # Create LLM for this model
    logger.debug(f"Initializing LLM: {model_name}")
    lm = dspy.LM(model_name)

    # Create ReAct agent with limited iterations for evaluation
    logger.debug(f"Creating ReAct agent...")
    signature = ConversationalMentionSignature
    react_agent = dspy.ReAct(
        signature,
        tools=tools,
        max_iters=20,  # Limit iterations for evaluation (not 1000!)
    )

    # Run agent with this model (with timeout)
    logger.debug(f"Running agent with 60s timeout...")
    try:
        async with asyncio.timeout(60):  # 60 second timeout per scenario
            with dspy.context(lm=lm, track_usage=True):
                result = await react_agent.acall(**context)
    except asyncio.TimeoutError:
        logger.error(f"Agent execution timed out after 60 seconds")
        raise TimeoutError("Agent execution timed out after 60 seconds")

    elapsed = time.time() - start_time
    logger.debug(f"Agent completed in {elapsed:.1f}s")

    # Extract thought process and tool calls from result
    thought_process = getattr(result, "rationale", str(result))
    logger.debug(f"Extracted thought process: {len(thought_process)} chars")

    # Try to extract tool calls from the result history
    tool_calls = []
    if hasattr(result, 'history'):
        logger.debug(f"Extracting tool calls from history...")
        for item in result.history:
            if hasattr(item, 'tool_name'):
                tool_calls.append({
                    "tool": item.tool_name,
                    "args": getattr(item, 'args', {}),
                    "result": getattr(item, 'result', None),
                })
                tracker.track_call(item.tool_name, getattr(item, 'args', {}), getattr(item, 'result', None))
        logger.debug(f"Extracted {len(tool_calls)} tool calls")
    else:
        logger.debug("No history attribute found on result")

    return {
        "thought_process": thought_process,
        "response": result.response if hasattr(result, 'response') else str(result),
        "actions": tool_calls,
        "raw_result": result,
    }


async def evaluate_model_on_scenario(
    scenario: Dict[str, Any],
    model_name: str,
    judge: LLMJudge,
) -> ModelRunMetrics:
    """
    Evaluate a specific model on a specific scenario.

    Args:
        scenario: Scenario configuration
        model_name: Model to evaluate
        judge: LLM judge for quality evaluation

    Returns:
        ModelRunMetrics with complete evaluation results
    """
    scenario_id = scenario.get("id", "unknown")

    logger.info(f"🔄 Starting: {model_name} on {scenario_id}")
    print(f"  🔄 Running {model_name} on {scenario_id}...")

    metrics = ModelRunMetrics(
        model_name=model_name,
        scenario_id=scenario_id,
    )

    # Start latency tracking
    metrics.latency.start_time = asyncio.get_event_loop().time()

    try:
        # Create tracker
        tracker = ToolCallTracker()

        # Run scenario
        print(f"     → Executing agent with {model_name}...")
        agent_output = await run_scenario_with_model(scenario, model_name, tracker)
        print(f"     ✓ Agent execution complete")

        # Mark first action time
        if tracker.first_action_time:
            metrics.latency.first_action_time = tracker.first_action_time

        # Mark completion
        metrics.latency.mark_completion()

        # Extract tool usage
        metrics.tool_usage.actual_tools = list(set(tracker.get_tool_names()))
        metrics.tool_usage.actual_sequence = tracker.get_tool_sequence()

        expected_behavior = scenario.get("expected_behavior", {})
        metrics.tool_usage.expected_tools = expected_behavior.get("expected_tools", [])
        metrics.tool_usage.expected_sequence = expected_behavior.get("expected_sequence", [])

        # Extract token usage from DSPy
        raw_result = agent_output.get("raw_result")
        if hasattr(raw_result, "get_lm_usage"):
            usage = raw_result.get_lm_usage()
            metrics.token_usage.input_tokens = usage.get("prompt_tokens", 0)
            metrics.token_usage.output_tokens = usage.get("completion_tokens", 0)
            print(f"     ✓ Tokens: {metrics.token_usage.total_tokens}")

        # Store agent output
        metrics.agent_output = agent_output

        # Evaluate quality with LLM judge
        print(f"     → Evaluating quality with LLM judge...")
        metrics.quality = judge.evaluate_from_run_metrics(scenario, agent_output)
        print(f"     ✓ Quality score: {metrics.quality.weighted_score:.2f}/10")

        elapsed = metrics.latency.total_time or 0
        cost = metrics.estimated_cost
        print(f"  ✅ Completed: {model_name} on {scenario_id} ({elapsed:.1f}s, ${cost:.4f})")
        logger.info(f"✅ Completed: {model_name} on {scenario_id}")

    except Exception as e:
        logger.error(f"Error evaluating {model_name} on scenario {scenario_id}: {e}", exc_info=True)
        metrics.error = str(e)
        metrics.latency.mark_completion()
        print(f"  ❌ Error: {model_name} on {scenario_id}: {str(e)[:100]}")

    return metrics


class ModelComparisonRunner:
    """Runs model comparisons across scenarios."""

    def __init__(
        self,
        models: List[str],
        scenarios_dir: Path,
        judge_model: Optional[str] = None,
    ):
        """
        Initialize the comparison runner.

        Args:
            models: List of model names to compare
            scenarios_dir: Directory containing scenario YAML files
            judge_model: Model to use for judging (defaults to LLM_JUDGE_MODEL)
        """
        self.models = models
        self.scenarios_dir = scenarios_dir
        self.scenarios = load_all_scenarios(scenarios_dir)
        self.judge = LLMJudge(judge_model)
        self.aggregator = MetricsAggregator()

    async def run_all(self) -> MetricsAggregator:
        """
        Run all scenarios through all models.

        Returns:
            MetricsAggregator with all results
        """
        total = len(self.models) * len(self.scenarios)
        current = 0

        print(f"\n{'='*80}")
        print(f"Running {total} evaluations ({len(self.models)} models × {len(self.scenarios)} scenarios)")
        print(f"{'='*80}\n")

        for scenario_idx, scenario in enumerate(self.scenarios, 1):
            scenario_id = scenario.get("id", "unknown")
            print(f"\n📋 Scenario {scenario_idx}/{len(self.scenarios)}: {scenario_id}")
            print(f"   Category: {scenario.get('category', 'unknown')}")
            print(f"   Description: {scenario.get('description', 'N/A')}\n")

            for model_name in self.models:
                current += 1
                print(f"[{current}/{total}] Evaluating {model_name}")

                metrics = await evaluate_model_on_scenario(scenario, model_name, self.judge)
                self.aggregator.add_run(metrics)

            # Print mini summary after each scenario
            scenario_runs = [r for r in self.aggregator.runs if r.scenario_id == scenario_id]
            successful = len([r for r in scenario_runs if r.error is None])
            print(f"\n   Summary: {successful}/{len(scenario_runs)} successful")

        print(f"\n{'='*80}")
        print(f"ALL EVALUATIONS COMPLETE")
        print(f"{'='*80}\n")

        return self.aggregator

    async def run_scenario(self, scenario_id: str) -> MetricsAggregator:
        """
        Run a specific scenario through all models.

        Args:
            scenario_id: ID of scenario to run

        Returns:
            MetricsAggregator with results for this scenario
        """
        scenario = next((s for s in self.scenarios if s.get("id") == scenario_id), None)

        if not scenario:
            raise ValueError(f"Scenario {scenario_id} not found")

        print(f"\n{'='*80}")
        print(f"Running scenario: {scenario_id}")
        print(f"Testing {len(self.models)} models")
        print(f"{'='*80}\n")

        aggregator = MetricsAggregator()

        for idx, model_name in enumerate(self.models, 1):
            print(f"[{idx}/{len(self.models)}] Evaluating {model_name}")
            metrics = await evaluate_model_on_scenario(scenario, model_name, self.judge)
            aggregator.add_run(metrics)

        print(f"\n{'='*80}")
        print(f"SCENARIO EVALUATION COMPLETE")
        print(f"{'='*80}\n")

        return aggregator

    async def run_model(self, model_name: str) -> MetricsAggregator:
        """
        Run a specific model through all scenarios.

        Args:
            model_name: Model to evaluate

        Returns:
            MetricsAggregator with results for this model
        """
        print(f"\n{'='*80}")
        print(f"Running model: {model_name}")
        print(f"Testing on {len(self.scenarios)} scenarios")
        print(f"{'='*80}\n")

        aggregator = MetricsAggregator()

        for idx, scenario in enumerate(self.scenarios, 1):
            scenario_id = scenario.get("id", "unknown")
            print(f"[{idx}/{len(self.scenarios)}] Scenario: {scenario_id}")
            metrics = await evaluate_model_on_scenario(scenario, model_name, self.judge)
            aggregator.add_run(metrics)

        print(f"\n{'='*80}")
        print(f"MODEL EVALUATION COMPLETE")
        print(f"{'='*80}\n")

        return aggregator
