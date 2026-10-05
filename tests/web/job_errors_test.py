"""Tests for removing message text from the errors Skrift records for a job."""

from __future__ import annotations

import traceback
from types import SimpleNamespace

import pytest
from skrift.workers.runtime import PermanentFailure

from smarter_dev.shared.message_content import MESSAGE_CONTENT_PLACEHOLDER
from smarter_dev.web import admin_handlers_jobs
from smarter_dev.web import handlers_jobs
from smarter_dev.web.job_errors import RedactedJobError
from smarter_dev.web.job_errors import RedactedPermanentFailure
from smarter_dev.web.job_errors import redacted_job_errors


def _what_skrift_stores(error: BaseException) -> str:
    # runtime._attempt_record: str(exc) and format_exception(exc).
    return str(error) + "".join(traceback.format_exception(error))


def test_an_error_leaves_with_its_types_and_frames_only():
    with pytest.raises(RedactedJobError) as raised:
        with redacted_job_errors():
            try:
                raise ValueError("what someone said")
            except ValueError as cause:
                raise RuntimeError("(parameters: 'what someone said')") from cause
    stored = _what_skrift_stores(raised.value)
    assert "what someone said" not in stored
    assert f"ValueError: {MESSAGE_CONTENT_PLACEHOLDER}" in stored
    assert f"RuntimeError: {MESSAGE_CONTENT_PLACEHOLDER}" in stored


def test_a_permanent_failure_stays_permanent():
    with pytest.raises(PermanentFailure) as raised:
        with redacted_job_errors():
            raise PermanentFailure("what someone said")
    assert isinstance(raised.value, RedactedPermanentFailure)
    assert "what someone said" not in _what_skrift_stores(raised.value)


def test_a_result_passes_through():
    with redacted_job_errors():
        result = {"status": "ok"}
    assert result == {"status": "ok"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "job"),
    [
        (handlers_jobs, "run_handler_fire"),
        (admin_handlers_jobs, "run_admin_handler_fire"),
    ],
)
async def test_both_fire_jobs_redact_at_their_boundary(monkeypatch, module, job):
    async def fails(payload, context):
        raise ValueError("what someone said")

    monkeypatch.setattr(module, f"_{job}", fails)
    with pytest.raises(RedactedJobError) as raised:
        await getattr(module, job)(
            SimpleNamespace(context_ref=None),
            SimpleNamespace(job=SimpleNamespace(attempt=1, max_attempts=3)),
        )
    assert "what someone said" not in _what_skrift_stores(raised.value)
