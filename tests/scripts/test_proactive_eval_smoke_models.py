from __future__ import annotations

import pytest

from scripts.proactive_eval import smoke_models


def test_credential_check_accepts_jev_alias(monkeypatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setenv("JEV_API_KEY", "test-key")

    smoke_models._check_credentials("typesafe:jev-latest")


def test_credential_check_requires_complete_glm_route(monkeypatch) -> None:
    for name in (
        "LITELLM_ENDPOINT",
        "LITELLM_API_KEY",
        "OPENROUTER_API_KEY",
        "OPEN_ROUTER_API_KEY",
        "OPEN_ROUTER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LITELLM_ENDPOINT", "https://proxy.example.test")

    with pytest.raises(SystemExit, match="GLM needs"):
        smoke_models._check_credentials("z-ai/glm-5.3-flash")


def test_credential_check_accepts_openrouter_alias(monkeypatch) -> None:
    monkeypatch.delenv("LITELLM_ENDPOINT", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_ROUTER_API_KEY", "test-key")

    smoke_models._check_credentials("z-ai/glm-5.3-flash")
