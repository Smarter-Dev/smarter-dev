"""Executes the data-driven expectations against the booted app.

Two HTTP identities:
- API checks: Bearer keys against the mounted FastAPI /api.
- Skrift admin: a real login through the dev dummy auth provider (creates the
  admin user + role, so no user seeding is required).

The legacy Starlette /bot-admin mount was removed in phase 03, so its
forged-cookie identity is gone; the /bot-admin tree is now asserted absent
(404) with the anonymous client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from scripts.local_harness import config
from scripts.local_harness.expectations import (
    API_CHECKS,
    BOT_ADMIN_GONE_PAGES,
    SKRIFT_ADMIN_PAGES,
    UNAUTHENTICATED_PAGES,
    AdminPageCheck,
    ApiCheck,
)

_AUTH_HEADERS: dict[str, dict[str, str]] = {
    # "bot" is the Skrift-native sk_ service key — the bot's only key shape
    # after the phase-02 DB consolidation.
    "bot": {"Authorization": f"Bearer {config.SKRIFT_BOT_API_KEY}"},
    # Retired legacy sk- key: never seeded post-flip, must always 401.
    "legacy_bot": {"Authorization": f"Bearer {config.LEGACY_BOT_API_KEY}"},
    "unknown_key": {"Authorization": f"Bearer {config.UNKNOWN_API_KEY}"},
    "unknown_skrift_key": {"Authorization": f"Bearer {config.UNKNOWN_SKRIFT_API_KEY}"},
    "malformed_key": {"Authorization": "Bearer not-a-valid-key"},
    "none": {},
}

_CSRF_INPUT = re.compile(r'name="_csrf"\s+value="([^"]+)"')
_SAVED_PLACEHOLDER = re.compile(r"\{saved:([a-z_]+)\}")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str

    def line(self) -> str:
        marker = "PASS" if self.passed else "FAIL"
        return f"[{marker}] {self.name}: {self.detail}"


def _fill_saved(value: object, saved: dict[str, str]) -> object:
    if isinstance(value, str):
        return _SAVED_PLACEHOLDER.sub(lambda m: saved[m.group(1)], value)
    if isinstance(value, dict):
        return {k: _fill_saved(v, saved) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill_saved(v, saved) for v in value]
    return value


def run_api_checks() -> list[CheckResult]:
    results: list[CheckResult] = []
    saved: dict[str, str] = {}
    with httpx.Client(base_url=config.API_BASE_URL, timeout=30.0) as client:
        for check in API_CHECKS:
            results.append(_run_api_check(client, check, saved))
    return results


def _run_api_check(
    client: httpx.Client, check: ApiCheck, saved: dict[str, str]
) -> CheckResult:
    path = str(_fill_saved(check.path, saved))
    body = _fill_saved(check.body, saved) if check.body is not None else None
    response = client.request(
        check.method,
        path,
        headers=_AUTH_HEADERS[check.auth],
        json=body,
    )
    if response.status_code not in check.expect_status:
        return CheckResult(
            check.name, False,
            f"{check.method} {path} -> {response.status_code} "
            f"(expected {check.expect_status}): {response.text[:300]}",
        )
    if check.validate is not None or check.save_key is not None:
        try:
            payload = response.json()
        except ValueError:
            return CheckResult(
                check.name, False,
                f"{check.method} {path} -> non-JSON body: {response.text[:200]}",
            )
        if check.validate is not None:
            error = check.validate(payload)
            if error is not None:
                return CheckResult(
                    check.name, False, f"{check.method} {path} -> {error}"
                )
        if check.save_key is not None:
            saved[check.save_key] = str(payload[check.save_field])
    return CheckResult(check.name, True, f"{check.method} {path} -> {response.status_code}")


def build_skrift_admin_client() -> httpx.Client:
    """Log in through the dev dummy provider with the admin toggle set."""
    client = httpx.Client(base_url=config.APP_BASE_URL, timeout=30.0, follow_redirects=True)
    login_page = client.get("/auth/dummy/login")
    login_page.raise_for_status()
    csrf_match = _CSRF_INPUT.search(login_page.text)
    if csrf_match is None:
        raise RuntimeError("dummy login page did not contain a _csrf field")
    submit = client.post(
        "/auth/dummy-login",
        data={
            "email": config.ADMIN_EMAIL,
            "name": config.ADMIN_NAME,
            "is_admin": "on",
            "_csrf": csrf_match.group(1),
        },
    )
    submit.raise_for_status()
    return client


def run_admin_page_checks(
    client: httpx.Client, pages: tuple[AdminPageCheck, ...]
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for page in pages:
        response = client.get(page.path)
        if response.status_code not in page.expect_status:
            results.append(CheckResult(
                page.name, False,
                f"GET {page.path} -> {response.status_code} "
                f"(expected {page.expect_status}): {response.text[:200]}",
            ))
            continue
        if page.expect_substring is not None and page.expect_substring not in response.text:
            results.append(CheckResult(
                page.name, False,
                f"GET {page.path} -> missing expected content {page.expect_substring!r}",
            ))
            continue
        results.append(CheckResult(page.name, True, f"GET {page.path} -> {response.status_code}"))
    return results


def run_all_checks() -> list[CheckResult]:
    results = run_api_checks()

    skrift_admin = build_skrift_admin_client()
    try:
        results.extend(run_admin_page_checks(skrift_admin, SKRIFT_ADMIN_PAGES))
    finally:
        skrift_admin.close()

    with httpx.Client(base_url=config.APP_BASE_URL, timeout=30.0) as anonymous:
        results.extend(run_admin_page_checks(anonymous, BOT_ADMIN_GONE_PAGES))
        results.extend(run_admin_page_checks(anonymous, UNAUTHENTICATED_PAGES))

    return results
