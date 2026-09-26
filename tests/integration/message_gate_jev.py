"""Jev arm of the message-gate quality eval.

Runs the production gate's own ``judge_messages`` — same questions, policy,
instructions, material and single request — on a model the eval builds, so a
different Jev version or boolean threshold can be scored without touching the
gate. Like ``filter_messages`` it fails open: an error or timeout allows every
candidate and is recorded as an error so the report can count it.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from dataclasses import field

from pydantic_ai.models import Model

from smarter_dev.bot.agents.message_gate import GATE_BOOLEAN_THRESHOLD
from smarter_dev.bot.agents.message_gate import GATE_MODEL_ID
from smarter_dev.bot.agents.message_gate import GateMessage
from smarter_dev.bot.agents.message_gate import judge_messages

# The eval allows slower answers than the gate so a slow call is scored, not
# timed out.
JEV_TIMEOUT_SECONDS = 30.0
# The watcher abstains below this per-field confidence; the eval only reports it.
JEV_LOW_CONFIDENCE = 0.2


@dataclass
class JevGateResult:
    allowed_ids: list[str]
    latency_seconds: float
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    # candidate id -> Jev's confidence in its verdict (0 undecided, 1 certain)
    confidence: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass
class JevGate:
    """Runs one gate decision on Jev, failing open like ``filter_messages``."""

    model: Model
    boolean_threshold: float = GATE_BOOLEAN_THRESHOLD
    timeout_seconds: float = JEV_TIMEOUT_SECONDS

    async def decide(
        self,
        response_filter: str,
        candidates: list[GateMessage],
        grounding: list[GateMessage],
        channel_name: str | None = None,
    ) -> JevGateResult:
        started = time.monotonic()
        try:
            judgment = await judge_messages(
                response_filter,
                candidates,
                grounding,
                channel_name,
                model=self.model,
                boolean_threshold=self.boolean_threshold,
                timeout_seconds=self.timeout_seconds,
            )
        except TimeoutError:
            result = JevGateResult(
                allowed_ids=[m.message_id for m in candidates],
                latency_seconds=0.0,
                errors=["timeout"],
            )
        except Exception as error:  # noqa: BLE001 - fail open, like the gate
            result = JevGateResult(
                allowed_ids=[m.message_id for m in candidates],
                latency_seconds=0.0,
                errors=[f"{type(error).__name__}: {error}"[:300]],
            )
        else:
            result = JevGateResult(
                allowed_ids=judgment.allowed_ids,
                latency_seconds=0.0,
                input_tokens=judgment.input_tokens,
                output_tokens=judgment.output_tokens,
                requests=judgment.requests,
                confidence=judgment.confidence,
            )
        result.latency_seconds = round(time.monotonic() - started, 3)
        return result


def jev_model_id() -> str:
    return os.getenv("MESSAGE_GATE_JEV_MODEL", GATE_MODEL_ID)


def jev_boolean_threshold() -> float:
    """``typesafe_boolean_threshold`` for the eval: env override, else the gate's."""
    raw = os.getenv("MESSAGE_GATE_JEV_BOOLEAN_THRESHOLD", "").strip()
    return float(raw) if raw else GATE_BOOLEAN_THRESHOLD


def build_jev_model(model_id: str | None = None) -> Model:
    """Build Jev exactly as the gate and proactive watcher do (needs a TypeSafe key)."""
    from smarter_dev.bot.proactive.models import build_twopass_model

    return build_twopass_model(model_id or jev_model_id())
