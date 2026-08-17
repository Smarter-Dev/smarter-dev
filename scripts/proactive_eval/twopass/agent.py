"""Shim: the two-pass core moved to smarter_dev.bot.proactive.agent."""

from smarter_dev.bot.proactive.agent import (  # noqa: F401
    AGENT_SYSTEM_PROMPT,
    Agent,
    AgentDeps,
    BUDGET_EXHAUSTED,
    COMPACTION_KEEP_MESSAGES,
    HISTORY_TOKEN_LIMIT,
    KimiAgentRunner,
    MAX_SENDS_PER_WAKE,
    OPERATING_POLICY_BRIEF,
    TOOL_CALL_LIMIT,
    ToolBudget,
    build_agent_system_prompt,
    build_kimi_agent,
    compact_agent_history,
    estimated_history_tokens,
)
