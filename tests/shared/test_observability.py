"""Tests for optional Logfire LLM instrumentation."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
import yaml

from smarter_dev.shared import observability

REPO_ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def reset_observability(monkeypatch):
    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setattr(observability, "_failover_counter", None)


def test_configure_observability_is_disabled_without_token(monkeypatch):
    monkeypatch.delenv("LOGFIRE_TOKEN", raising=False)

    with patch.object(observability.logfire, "configure") as configure:
        assert observability.configure_observability("test-service") is False

    configure.assert_not_called()


def test_configure_observability_enables_metadata_only_instrumentation(monkeypatch):
    monkeypatch.setenv("LOGFIRE_TOKEN", "write-token")
    monkeypatch.setenv("LOGFIRE_SERVICE_NAME", "configured-service")
    monkeypatch.setenv("LOGFIRE_SERVICE_VERSION", "abc1234")
    monkeypatch.setenv("ENVIRONMENT", "production")
    counter = MagicMock()

    with (
        patch.object(observability.logfire, "configure") as configure,
        patch.object(observability.logfire, "instrument_pydantic_ai") as instrument,
        patch.object(
            observability.logfire, "metric_counter", return_value=counter
        ) as metric_counter,
    ):
        assert observability.configure_observability("default-service") is True
        assert observability.configure_observability("ignored-service") is True

    configure.assert_called_once_with(
        token="write-token",
        service_name="configured-service",
        service_version="abc1234",
        environment="production",
        send_to_logfire=True,
        console=False,
        distributed_tracing=False,
        inspect_arguments=False,
    )
    instrument.assert_called_once_with(
        include_content=False,
        include_binary_content=False,
    )
    metric_counter.assert_called_once_with(
        "smarter_dev.llm.failover",
        unit="{failover}",
        description="LLM calls moved from a primary model to a fallback model",
    )
    assert observability._failover_counter is counter


def test_configure_observability_fails_open(monkeypatch, caplog):
    monkeypatch.setenv("LOGFIRE_TOKEN", "bad-token")

    with (
        patch.object(
            observability.logfire,
            "configure",
            side_effect=RuntimeError("configuration failed"),
        ),
        caplog.at_level("ERROR", logger=observability.__name__),
    ):
        assert observability.configure_observability("test-service") is False

    assert observability._configured is False
    assert "telemetry is disabled" in caplog.text


def test_record_llm_failover_emits_metric_and_metadata_only_error(monkeypatch):
    counter = MagicMock()
    monkeypatch.setattr(observability, "_configured", True)
    monkeypatch.setattr(observability, "_failover_counter", counter)
    error = RuntimeError("provider response body must not be exported")

    with patch.object(observability.logfire, "error") as log_error:
        observability.record_llm_failover(
            operation="web_summarizer",
            primary_model="poolside/laguna-s-2.1",
            fallback_model="gemini-3.1-flash-lite",
            error=error,
        )

    attributes = {
        "smarter_dev.llm.operation": "web_summarizer",
        "gen_ai.request.model": "poolside/laguna-s-2.1",
        "smarter_dev.llm.fallback_model": "gemini-3.1-flash-lite",
        "error.type": "RuntimeError",
    }
    counter.add.assert_called_once_with(1, attributes)
    log_error.assert_called_once()
    assert "provider response body" not in str(log_error.call_args)


@pytest.mark.parametrize(
    ("manifest_name", "service_name"),
    (
        ("deploy-bot.yaml", "smarter-dev-bot"),
        ("deploy.yaml", "smarter-dev-web"),
        ("deploy-worker.yaml", "smarter-dev-agent-worker"),
    ),
)
def test_kubernetes_workloads_have_optional_logfire_configuration(
    manifest_name: str, service_name: str
):
    manifest = yaml.safe_load((REPO_ROOT / "k8s" / manifest_name).read_text())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in container["env"]}

    token_ref = env["LOGFIRE_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert token_ref == {
        "name": "smarter-dev-secrets",
        "key": "logfire-token",
        "optional": True,
    }
    assert env["LOGFIRE_SERVICE_NAME"]["value"] == service_name
    assert env["LOGFIRE_SERVICE_VERSION"]["value"] == "<IMAGE_VERSION>"


def test_worker_runtime_imports_observability_bootstrap_first():
    app_config = yaml.safe_load((REPO_ROOT / "app.yaml").read_text())
    assert app_config["workers"]["imports"][0] == (
        "smarter_dev.shared.worker_observability"
    )


def test_pydantic_ai_instrumentation_emits_usage_without_content():
    """Exercise the real integration in a subprocess to isolate global OTel state."""
    script = textwrap.dedent(
        """
        import asyncio
        import json

        import logfire
        from logfire.testing import TestExporter
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from pydantic_ai import Agent
        from pydantic_ai.models.test import TestModel

        exporter = TestExporter()
        metrics_reader = InMemoryMetricReader()
        logfire.configure(
            send_to_logfire=False,
            console=False,
            additional_span_processors=[SimpleSpanProcessor(exporter)],
            metrics=logfire.MetricsOptions(additional_readers=[metrics_reader]),
        )
        logfire.instrument_pydantic_ai(
            include_content=False,
            include_binary_content=False,
        )
        agent = Agent(
            TestModel(custom_output_text="SECRET_RESPONSE"),
            system_prompt="SECRET_SYSTEM",
        )
        asyncio.run(agent.run("SECRET_PROMPT"))

        spans = exporter.exported_spans_as_dict(parse_json_attributes=True)
        serialized_spans = json.dumps(spans)
        metrics = metrics_reader.get_metrics_data().to_json()
        assert any(span["name"] == "chat test" for span in spans)
        assert all(span["end_time"] > span["start_time"] for span in spans)
        assert "gen_ai.client.token.usage" in metrics
        assert not any(
            secret in serialized_spans
            for secret in ("SECRET_PROMPT", "SECRET_RESPONSE", "SECRET_SYSTEM")
        )
        """
    )

    subprocess.run([sys.executable, "-c", script], check=True)
