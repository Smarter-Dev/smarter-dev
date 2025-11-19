"""
LLM-as-judge evaluation for mention agent responses.

Uses an LLM to evaluate response quality across multiple criteria.
"""

import os
import dspy
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field

from tests.evaluation.metrics_tracker import QualityMetrics


class JudgeEvaluation(BaseModel):
    """Structured output from the LLM judge."""

    appropriateness: int = Field(
        ...,
        ge=1,
        le=10,
        description="Was the decision to respond/skip/redirect appropriate? (1-10)",
    )
    quality: int = Field(
        ...,
        ge=1,
        le=10,
        description="If responded, was the response helpful and well-crafted? (1-10)",
    )
    community_tone: int = Field(
        ...,
        ge=1,
        le=10,
        description="Does it maintain a welcoming developer community vibe? (1-10)",
    )
    contextual_awareness: int = Field(
        ...,
        ge=1,
        le=10,
        description="Did it consider conversation history appropriately? (1-10)",
    )
    effectiveness: int = Field(
        ...,
        ge=1,
        le=10,
        description="Does it handle the situation as intended? (1-10)",
    )
    reasoning: str = Field(
        ...,
        description="Detailed explanation of the scores",
    )


class MentionAgentJudgeSignature(dspy.Signature):
    """
    Evaluate a mention agent's response to a conversation.

    You are evaluating an AI bot that participates in a developer Discord community.
    The bot should be helpful, technically accurate, casual, and maintain a welcoming tone.

    Evaluate the bot's action plan and resulting behavior across these criteria:

    1. APPROPRIATENESS (1-10): Was the decision to respond/skip/redirect appropriate?
       - Did it correctly identify when to engage vs stay quiet?
       - If redirecting sensitive topics, was it done gracefully?
       - Did it avoid over-engaging or under-engaging?

    2. QUALITY (1-10): If responded, was the response helpful and well-crafted?
       - Technical accuracy and helpfulness
       - Clear and concise communication
       - Appropriate level of detail
       - Good use of tools (web search, URL analysis, etc.)

    3. COMMUNITY_TONE (1-10): Does it maintain welcoming developer community vibe?
       - Casual but professional
       - Encouraging and supportive
       - Not overly formal or robotic
       - Appropriate emoji/reaction usage

    4. CONTEXTUAL_AWARENESS (1-10): Did it consider conversation history?
       - Understood the full context
       - Referenced relevant prior messages
       - Adapted to conversation flow
       - Recognized user relationships/roles

    5. EFFECTIVENESS (1-10): Does it handle the situation as intended?
       - Achieved the right outcome
       - Tool usage was appropriate
       - Timing and pacing were good
       - Would improve the conversation

    Provide scores (1-10) for each criterion and detailed reasoning.
    """

    scenario_description: str = dspy.InputField(
        description="Description of what this scenario is testing"
    )
    conversation_context: str = dspy.InputField(
        description="The conversation timeline with messages from users"
    )
    agent_plan: str = dspy.InputField(
        description="The agent's thought process and action plan"
    )
    agent_actions: str = dspy.InputField(
        description="The actual tool calls and actions taken by the agent"
    )
    expected_behavior: str = dspy.InputField(
        description="What we expect the agent to do in this scenario"
    )

    evaluation: JudgeEvaluation = dspy.OutputField(
        description="Structured evaluation with scores and reasoning"
    )


class LLMJudge:
    """Evaluates mention agent responses using an LLM judge."""

    def __init__(self, judge_model: Optional[str] = None):
        """
        Initialize the LLM judge.

        Args:
            judge_model: Model to use for judging. Defaults to LLM_JUDGE_MODEL env var.
        """
        self.judge_model = judge_model or os.environ.get("LLM_JUDGE_MODEL", "gemini/gemini-2.5-flash")
        self.judge_lm = dspy.LM(self.judge_model)
        self.judge_predictor = dspy.Predict(MentionAgentJudgeSignature)

    def evaluate(
        self,
        scenario_description: str,
        conversation_context: str,
        agent_plan: str,
        agent_actions: str,
        expected_behavior: str,
    ) -> QualityMetrics:
        """
        Evaluate an agent's response.

        Args:
            scenario_description: What this scenario is testing
            conversation_context: The conversation timeline
            agent_plan: The agent's thought process
            agent_actions: The actual actions taken
            expected_behavior: What we expect to happen

        Returns:
            QualityMetrics with scores and reasoning
        """
        with dspy.context(lm=self.judge_lm):
            try:
                result = self.judge_predictor(
                    scenario_description=scenario_description,
                    conversation_context=conversation_context,
                    agent_plan=agent_plan,
                    agent_actions=agent_actions,
                    expected_behavior=expected_behavior,
                )

                # Handle both structured and unstructured responses
                if hasattr(result, 'evaluation') and isinstance(result.evaluation, JudgeEvaluation):
                    evaluation = result.evaluation
                else:
                    # Parse from text if needed
                    import re
                    eval_text = str(result.evaluation if hasattr(result, 'evaluation') else result)

                    # Extract scores using regex
                    def extract_score(text: str, field: str) -> int:
                        pattern = rf"{field}[:\s]+(\d+)"
                        match = re.search(pattern, text, re.IGNORECASE)
                        return int(match.group(1)) if match else 5

                    return QualityMetrics(
                        appropriateness=extract_score(eval_text, "appropriateness"),
                        quality=extract_score(eval_text, "quality"),
                        community_tone=extract_score(eval_text, "community_tone"),
                        contextual_awareness=extract_score(eval_text, "contextual_awareness"),
                        effectiveness=extract_score(eval_text, "effectiveness"),
                        judge_reasoning=eval_text,
                    )

                return QualityMetrics(
                    appropriateness=evaluation.appropriateness,
                    quality=evaluation.quality,
                    community_tone=evaluation.community_tone,
                    contextual_awareness=evaluation.contextual_awareness,
                    effectiveness=evaluation.effectiveness,
                    judge_reasoning=evaluation.reasoning,
                )

            except Exception as e:
                # If evaluation fails, return neutral scores with error message
                return QualityMetrics(
                    appropriateness=5,
                    quality=5,
                    community_tone=5,
                    contextual_awareness=5,
                    effectiveness=5,
                    judge_reasoning=f"Evaluation failed: {str(e)}",
                )

    def evaluate_from_run_metrics(
        self,
        scenario: Dict[str, Any],
        agent_output: Dict[str, Any],
    ) -> QualityMetrics:
        """
        Convenience method to evaluate from scenario and agent output.

        Args:
            scenario: Scenario dict with conversation, expected behavior, etc.
            agent_output: Agent's output including actions and thought process

        Returns:
            QualityMetrics with scores and reasoning
        """
        # Format conversation context
        conversation_lines = []
        for msg in scenario.get("conversation", []):
            timestamp = msg.get("timestamp", "")
            user = msg.get("user", "Unknown")
            content = msg.get("content", "")
            conversation_lines.append(f"[{timestamp}] {user}: {content}")

        conversation_context = "\n".join(conversation_lines)

        # Format agent plan (thought process)
        agent_plan = agent_output.get("thought_process", "No thought process recorded")

        # Format agent actions
        actions = agent_output.get("actions", [])
        if actions:
            action_lines = []
            for action in actions:
                tool = action.get("tool", "unknown")
                args = action.get("args", {})
                action_lines.append(f"- {tool}({args})")
            agent_actions = "\n".join(action_lines)
        else:
            agent_actions = "No actions taken"

        # Format expected behavior
        expected_behavior = scenario.get("expected_behavior", {})
        expected_lines = []

        if "expected_tools" in expected_behavior:
            expected_lines.append(f"Expected tools: {expected_behavior['expected_tools']}")
        if "expected_response_type" in expected_behavior:
            expected_lines.append(f"Response type: {expected_behavior['expected_response_type']}")
        if "description" in expected_behavior:
            expected_lines.append(f"Description: {expected_behavior['description']}")

        expected_str = "\n".join(expected_lines) if expected_lines else "No specific expectations defined"

        return self.evaluate(
            scenario_description=scenario.get("description", "Unknown scenario"),
            conversation_context=conversation_context,
            agent_plan=agent_plan,
            agent_actions=agent_actions,
            expected_behavior=expected_str,
        )
