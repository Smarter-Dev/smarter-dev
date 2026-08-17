"""Headless Claude Code invocation — the judge shared by labeling and scoring.

`claude -p <prompt> --output-format json` prints one JSON object whose
`result` field is the model's text and which carries `total_cost_usd`.
Runs on the local claude CLI login.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

JUDGE_TIMEOUT_SECONDS = 300
# The judge occasionally returns malformed output (~1% per item); callers
# re-ask this many times before failing their run.
JUDGE_PARSE_RETRIES = 2
DEFAULT_JUDGE_MODEL = "claude-sonnet-5"

_FENCED_JSON_PATTERN = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


@dataclass
class JudgeReply:
    result_text: str
    cost_usd: float
    raw: dict


def judge_reply_from_raw(raw: dict) -> JudgeReply:
    cost = raw.get("total_cost_usd")
    return JudgeReply(
        result_text=raw["result"],
        cost_usd=float(cost) if cost is not None else 0.0,
        raw=raw,
    )


def run_judge(prompt: str, model: str) -> JudgeReply:
    """One headless Claude Code invocation. The only subprocess in the eval."""
    command = ["claude", "-p", prompt, "--output-format", "json", "--model", model]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise SystemExit(
            "claude CLI not found on PATH — install and authenticate Claude Code"
        ) from error
    if completed.returncode != 0:
        raise SystemExit(
            f"claude exited {completed.returncode}: {completed.stderr[:500]}"
        )
    return judge_reply_from_raw(json.loads(completed.stdout))


def extract_fenced_json(result_text: str) -> str:
    """The first fenced JSON block of a judge reply, or fail fast."""
    fence_match = _FENCED_JSON_PATTERN.search(result_text)
    if fence_match is None:
        raise ValueError(
            f"no fenced JSON block in judge output: {result_text[:200]!r}"
        )
    return fence_match.group(1)
