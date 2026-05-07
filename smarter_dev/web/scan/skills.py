"""Skill loader for Scan research and synthesis agents.

Each mode (quick_answer, quick_research, standard, deep) has a research
skill and a synthesis skill — markdown documents that define agent behavior,
strategy, quality bars, and output expectations.
"""

from __future__ import annotations

import functools
from pathlib import Path

_SKILLS_DIR = Path(__file__).parent / "skills"

_MODE_FILE_MAP = {
    "quick_answer": "quick-answer",
    "quick_research": "quick-research",
    "standard": "standard",
    "deep": "deep",
}


@functools.lru_cache(maxsize=8)
def load_research_skill(mode: str) -> str:
    """Return the research skill markdown for the given mode."""
    file_key = _MODE_FILE_MAP[mode]
    return (_SKILLS_DIR / f"research-{file_key}.md").read_text()


@functools.lru_cache(maxsize=8)
def load_synthesis_skill(mode: str) -> str:
    """Return the synthesis skill markdown for the given mode."""
    file_key = _MODE_FILE_MAP[mode]
    return (_SKILLS_DIR / f"synthesis-{file_key}.md").read_text()
