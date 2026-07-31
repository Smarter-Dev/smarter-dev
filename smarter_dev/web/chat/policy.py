"""Structural intelligence-mode policy for web Chat tools and sub-agents."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IntelligenceMode(str, Enum):
    MAXIMIZE_EFFICIENCY = "maximize_efficiency"
    EFFICIENT = "efficient"
    INTELLIGENCE = "intelligence"
    MAXIMIZE_INTELLIGENCE = "maximize_intelligence"
    ULTRA_INTELLIGENCE = "ultra_intelligence"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


@dataclass(frozen=True, slots=True)
class IntelligencePolicy:
    mode: IntelligenceMode
    max_tool_calls: int | None
    max_searches: int | None
    max_search_results: int | None
    web_summary_required: bool
    max_subagents: int | None
    configured_compaction: bool

    @property
    def subagents_enabled(self) -> bool:
        return self.max_subagents is None or self.max_subagents > 0

    @property
    def uses_framework_model_limits(self) -> bool:
        return self.max_tool_calls is None


POLICIES: dict[IntelligenceMode, IntelligencePolicy] = {
    IntelligenceMode.MAXIMIZE_EFFICIENCY: IntelligencePolicy(
        IntelligenceMode.MAXIMIZE_EFFICIENCY, 5, 2, 3, True, 0, True
    ),
    IntelligenceMode.EFFICIENT: IntelligencePolicy(
        IntelligenceMode.EFFICIENT, 10, 2, 3, True, 0, True
    ),
    IntelligenceMode.INTELLIGENCE: IntelligencePolicy(
        IntelligenceMode.INTELLIGENCE, 10, 5, 5, False, 3, True
    ),
    IntelligenceMode.MAXIMIZE_INTELLIGENCE: IntelligencePolicy(
        IntelligenceMode.MAXIMIZE_INTELLIGENCE, None, None, 10, False, 10, False
    ),
    IntelligenceMode.ULTRA_INTELLIGENCE: IntelligencePolicy(
        IntelligenceMode.ULTRA_INTELLIGENCE, None, None, None, False, None, False
    ),
}


def parse_intelligence_mode(value: str | IntelligenceMode) -> IntelligenceMode:
    if isinstance(value, IntelligenceMode):
        return value
    # Accept human-facing spellings without weakening persisted canonical values.
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return IntelligenceMode(normalized)


def policy_for(value: str | IntelligenceMode) -> IntelligencePolicy:
    return POLICIES[parse_intelligence_mode(value)]


def compaction_model_key(
    mode: str | IntelligenceMode, *, selected_model_key: str, configured_model_key: str
) -> str:
    policy = policy_for(mode)
    return configured_model_key if policy.configured_compaction else selected_model_key
