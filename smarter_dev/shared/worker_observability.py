"""Logfire bootstrap imported by the Skrift worker runtime."""

from smarter_dev.shared.observability import configure_observability

configure_observability("smarter-dev-agent-worker")
