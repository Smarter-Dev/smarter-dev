"""Exact billing origins, settings validation, and URL parser ambiguity checks."""

import pytest
from pydantic import ValidationError

from smarter_dev.shared.billing_urls import billing_url_origin
from smarter_dev.shared.config import Settings


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://smarter.dev", ("https", "smarter.dev", 443)),
        (
            "https://NEXT.smarter.dev:443/sudo/confirm?checkout=123",
            ("https", "next.smarter.dev", 443),
        ),
        (
            "https://next.smarter.dev:8443/account/billing",
            ("https", "next.smarter.dev", 8443),
        ),
        ("http://localhost:8000/sudo/cancel", ("http", "localhost", 8000)),
        ("http://[::1]:8000/account/billing", ("http", "::1", 8000)),
    ],
)
def test_url_origins(url, expected):
    assert billing_url_origin(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "//next.smarter.dev/path",
        "/path",
        "https:next.smarter.dev",
        "javascript:alert(1)",
        "ftp://next.smarter.dev/path",
        "https://user@next.smarter.dev/path",
        "https://:password@next.smarter.dev/path",
        "https://next.smarter.dev/path#fragment",
        "https://next.smarter.dev/#",
        "https://next.smarter.dev\\@evil.test/path",
        " https://next.smarter.dev/path",
        "https://next.smarter.dev/\nx",
        "https://next.smarter.dev/\tx",
        "https://next.smarter.dev/\x00x",
        "https://next.smarter.dev:bad/path",
        "https://next.smarter.dev:/path",
        "https://next.smarter.dev:0/path",
        "https://next.smarter.dev:65536/path",
        "https://next.smarter.dev./path",
        "https://next..smarter.dev/path",
        "https://*.smarter.dev/path",
        "https://%6eext.smarter.dev/path",
        "https://[::1]garbage/path",
        "https://[::1%25eth0]/path",
        "https://-next.smarter.dev/path",
    ],
)
def test_unsafe_or_malformed_urls_are_rejected(url):
    with pytest.raises(ValueError):
        billing_url_origin(url)


@pytest.mark.parametrize(
    "origin",
    [
        "https://*.smarter.dev",
        "https://next.smarter.dev/path",
        "https://next.smarter.dev?",
        "https://next.smarter.dev#",
        "https://user@next.smarter.dev",
        "javascript:alert(1)",
        "https://next.smarter.dev:bad",
        "https://next.smarter.dev:",
    ],
)
def test_settings_reject_non_origins(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, site_billing_allowed_origins=[origin])


def test_setting_default_explicit_empty_and_json_environment(monkeypatch):
    monkeypatch.delenv("SITE_BILLING_ALLOWED_ORIGINS", raising=False)
    assert Settings(_env_file=None).site_billing_allowed_origins is None
    assert (
        Settings(
            _env_file=None, site_billing_allowed_origins=[]
        ).site_billing_allowed_origins
        == []
    )
    monkeypatch.setenv(
        "SITE_BILLING_ALLOWED_ORIGINS",
        '["https://smarter.dev", "https://next.smarter.dev"]',
    )
    settings = Settings(_env_file=None, site_base_url="https://smarter.dev")
    assert settings.site_billing_allowed_origins == [
        "https://smarter.dev",
        "https://next.smarter.dev",
    ]
    assert settings.site_base_url == "https://smarter.dev"
