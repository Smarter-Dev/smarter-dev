"""Optional, metadata-only Logfire instrumentation for LLM operations."""

from __future__ import annotations

import logging
import os
from typing import Any

import logfire

logger = logging.getLogger(__name__)

_configured = False
_failover_counter: Any | None = None


def configure_observability(default_service_name: str) -> bool:
    """Configure Logfire once when a write token is available.

    LLM content is deliberately excluded: Discord messages, model responses,
    tool arguments, and binary attachments must not leave the application as
    observability payloads. Configuration is fail-open so telemetry can never
    prevent a production workload from starting.
    """
    global _configured, _failover_counter

    if _configured:
        return True

    token = os.getenv("LOGFIRE_TOKEN", "").strip()
    if not token:
        return False

    service_name = os.getenv("LOGFIRE_SERVICE_NAME", default_service_name)
    service_version = os.getenv("LOGFIRE_SERVICE_VERSION") or None
    environment = os.getenv("ENVIRONMENT", "development")

    try:
        logfire.configure(
            token=token,
            service_name=service_name,
            service_version=service_version,
            environment=environment,
            send_to_logfire=True,
            console=False,
            distributed_tracing=False,
            inspect_arguments=False,
        )
        logfire.instrument_pydantic_ai(
            include_content=False,
            include_binary_content=False,
        )
        _failover_counter = logfire.metric_counter(
            "smarter_dev.llm.failover",
            unit="{failover}",
            description="LLM calls moved from a primary model to a fallback model",
        )
    except Exception:
        logger.exception("Logfire configuration failed; telemetry is disabled")
        return False

    _configured = True
    logger.info(
        "Logfire LLM telemetry enabled for service=%s environment=%s",
        service_name,
        environment,
    )
    return True


def record_llm_failover(
    *, operation: str, primary_model: str, fallback_model: str, error: Exception
) -> None:
    """Record a low-cardinality failover metric and structured error event."""
    if not _configured or _failover_counter is None:
        return

    attributes = {
        "smarter_dev.llm.operation": operation,
        "gen_ai.request.model": primary_model,
        "smarter_dev.llm.fallback_model": fallback_model,
        "error.type": type(error).__name__,
    }
    try:
        _failover_counter.add(1, attributes)
        logfire.error(
            "LLM failover: {primary_model} to {fallback_model}",
            primary_model=primary_model,
            fallback_model=fallback_model,
            **attributes,
        )
    except Exception:
        logger.debug("Could not emit Logfire failover telemetry", exc_info=True)
