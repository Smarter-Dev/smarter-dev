from __future__ import annotations

from datetime import UTC
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from openai import RateLimitError

from scripts.proactive_eval.glm_compact_paced_v2 import PLAN_VERSION
from scripts.proactive_eval.glm_compact_paced_v2 import build_plan
from scripts.proactive_eval.glm_compact_paced_v2 import retry_after_seconds
from scripts.proactive_eval.glm_compact_paced_v2 import run
from scripts.proactive_eval.glm_compact_paced_v2 import safe_error_diagnostics
from scripts.proactive_eval.glm_compact_paced_v2 import safe_response_metadata


def test_response_metadata_uses_exact_safe_allowlist() -> None:
    metadata = safe_response_metadata(
        {
            "Retry-After": "120",
            "X-RateLimit-Remaining-Requests": "0",
            "X-RateLimit-Reset-Tokens": "45s",
            "X-Request-ID": "req-safe",
            "Authorization": "Bearer secret",
            "Set-Cookie": "session=secret",
            "X-Provider-Error": "raw private body",
        },
        status_code=429,
    )

    assert metadata == {
        "status_code": 429,
        "retry_after": "120",
        "rate_limit_reset_tokens": "45s",
        "rate_limit_remaining_requests": "0",
        "request_id": "req-safe",
    }
    assert "secret" not in str(metadata)
    assert "private" not in str(metadata)


def test_response_metadata_rejects_control_characters_and_long_values() -> None:
    metadata = safe_response_metadata(
        {
            "Retry-After": "1\nAuthorization: secret",
            "X-Request-ID": "x" * 257,
        },
        status_code=429,
    )

    assert metadata == {"status_code": 429}


def test_error_diagnostics_exclude_error_body_and_unsafe_headers() -> None:
    secret = "private provider response"
    request = httpx.Request("POST", "https://llmproxy.zech.sh/v1/chat/completions")
    response = httpx.Response(
        429,
        request=request,
        headers={
            "Retry-After": "120",
            "X-Request-ID": "req-safe",
            "Set-Cookie": "secret-cookie",
        },
        json={"error": {"message": secret}},
    )
    error = RateLimitError(secret, response=response, body={"message": secret})

    diagnostics = safe_error_diagnostics(error)

    assert diagnostics == {
        "provider_error_category": "rate_limit",
        "status_code": 429,
        "response_metadata": {
            "status_code": 429,
            "retry_after": "120",
            "request_id": "req-safe",
        },
        "retry_after_seconds": 120.0,
    }
    assert secret not in str(diagnostics)
    assert "cookie" not in str(diagnostics).lower()


def test_retry_after_supports_seconds_and_http_dates() -> None:
    now = datetime(2026, 9, 21, 16, 0, tzinfo=UTC)
    future = format_datetime(datetime(2026, 9, 21, 16, 2, tzinfo=UTC), usegmt=True)

    assert retry_after_seconds({"retry_after": "90"}, now=now) == 90
    assert retry_after_seconds({"retry_after": future}, now=now) == 120
    assert retry_after_seconds({"retry_after": "invalid"}, now=now) is None


def test_plan_is_new_paced_trial_for_exact_three_remaining_cases() -> None:
    plan = build_plan()

    assert plan["plan_version"] == PLAN_VERSION
    assert plan["trial_kind"] == "new paced follow-up after cooldown; not a resume"
    assert plan["strata"] == {"required": 1, "negative": 2, "ambiguous": 0}
    assert len(plan["case_ids"]) == 3
    assert plan["controls"]["minimum_seconds_between_requests"] == 60.0
    assert plan["controls"]["stop_on_first_error"] is True
    assert plan["controls"]["overwrite_or_resume"] is False
    assert plan["workload"]["cumulative_conservative_maximum_usd"] < 5


@pytest.mark.asyncio
async def test_paced_run_refuses_to_resume_or_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing overwrite/resume"):
        await run(tmp_path / "plan.json", output)
