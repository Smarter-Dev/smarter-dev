"""Tests for smarter_dev.shared.credentials.

The vectors below are shared with services/media/test/unit/credentials.test.ts
and the proactive-agent port; change them in all three places or none.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

from smarter_dev.shared import credentials
from smarter_dev.shared.credentials import REGISTRY
from smarter_dev.shared.credentials import decode_value
from smarter_dev.shared.credentials import encode_value
from smarter_dev.shared.credentials import get_credential

HUNDRED = ("abcdefghijklmnopqrstuvwxyz" * 4)[:100]
VECTORS = [
    ("hello", "v1.-PEJJ30"),
    (
        HUNDRED,
        "v1.8fYGL3ffnLgeWOKWu7SXGM0HxWDKe99rYkhhqXx6esSxwJ1exa0o3_tIAHdkoi1LK_ib1ffado-_J6v18XOqbewvuyfC5Qq_UrICBwjGMhZEUxpOvbVm85RMj_Rruyn6ttJxJg",
    ),
]
REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("plain", "encoded"), VECTORS)
def test_fixed_vectors(plain: str, encoded: str) -> None:
    assert encode_value(plain) == encoded
    assert decode_value(encoded) == plain


def test_round_trip_multibyte() -> None:
    plain = "ключ-🔑-" * 20
    assert decode_value(encode_value(plain)) == plain


@pytest.mark.parametrize("bad", ["plain", "v2.abc", ""])
def test_decode_requires_prefix(bad: str) -> None:
    with pytest.raises(ValueError):
        decode_value(bad)


@pytest.mark.parametrize("body", ["!!!", "a+b", "a/b", "a=b"])
def test_decode_rejects_non_base64url_body(body: str) -> None:
    with pytest.raises(ValueError):
        decode_value("v1." + body)


def test_values_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "media_api_key")
    monkeypatch.setenv("HVTH7T", encode_value("opaque-value\n"))
    assert get_credential("media_api_key") == "opaque-value"
    monkeypatch.delenv("HVTH7T")
    credentials._logged.discard("media_api_key")
    monkeypatch.setenv("MEDIA_API_KEY", "  legacy-value \n")
    assert get_credential("media_api_key") == "legacy-value"


def test_blank_opaque_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear(monkeypatch, "media_api_key")
    monkeypatch.setenv("HVTH7T", encode_value("  \n"))
    monkeypatch.setenv("MEDIA_API_KEY", "legacy-value")
    with caplog.at_level(logging.WARNING, logger=credentials.logger.name):
        assert get_credential("media_api_key") == "legacy-value"
    assert any(
        r.levelno == logging.WARNING and "HVTH7T" in r.getMessage()
        for r in caplog.records
    )


def _clear(monkeypatch: pytest.MonkeyPatch, logical: str) -> None:
    opaque, legacy = REGISTRY[logical]
    for name in ((opaque,) if opaque else ()) + legacy:
        monkeypatch.delenv(name, raising=False)
    credentials._logged.discard(logical)


def test_opaque_wins_over_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "media_api_key")
    monkeypatch.setenv("MEDIA_API_KEY", "legacy-value")
    monkeypatch.setenv("HVTH7T", encode_value("opaque-value"))
    assert get_credential("media_api_key") == "opaque-value"


def test_legacy_fallback_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "second")
    assert get_credential("gemini") == "second"
    monkeypatch.setenv("GEMINI_API_KEY", "first")
    assert get_credential("gemini") == "first"


def test_missing_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "jina")
    assert get_credential("jina") is None


def test_invalid_opaque_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "jina")
    monkeypatch.setenv("IDNDWL", "not-encoded")
    monkeypatch.setenv("JINA_API_KEY", "legacy")
    assert get_credential("jina") == "legacy"


def test_source_logged_once_and_never_the_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear(monkeypatch, "youtube")
    monkeypatch.setenv("WFZ62P", encode_value("super-secret-value"))
    with caplog.at_level(logging.INFO, logger=credentials.logger.name):
        get_credential("youtube")
        get_credential("youtube")
    assert [r.getMessage() for r in caplog.records] == [
        "credential youtube: source=opaque"
    ]
    assert "super-secret-value" not in caplog.text


def test_no_writeback_to_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "typesafe")
    monkeypatch.setenv("JEV_API_KEY", "legacy-test-key")
    assert get_credential("typesafe") == "legacy-test-key"
    import os

    assert "TYPESAFE_API_KEY" not in os.environ


def test_registry_names_are_unique_and_opaque() -> None:
    opaque = [o for o, _ in REGISTRY.values() if o]
    assert len(set(opaque)) == len(opaque)
    assert all(re.fullmatch(r"[A-Z0-9]{6}", o) for o in opaque)


def test_digitalocean_and_zen_are_legacy_only(monkeypatch: pytest.MonkeyPatch) -> None:
    assert REGISTRY["digitalocean_inference"][0] is None
    assert REGISTRY["opencode_zen"][0] is None
    _clear(monkeypatch, "opencode_zen")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "zen")
    assert get_credential("opencode_zen") == "zen"


def test_cli_encode_and_decode_check() -> None:
    cmd = [sys.executable, "-m", "smarter_dev.shared.credentials"]
    encoded = subprocess.run(
        [*cmd, "encode"], input="hello\n", capture_output=True, text=True, check=True, cwd=REPO
    ).stdout.strip()
    assert encoded == "v1.-PEJJ30"
    ok = subprocess.run(
        [*cmd, "decode", "--check"], input=encoded, capture_output=True, text=True, cwd=REPO
    )
    assert (ok.returncode, ok.stdout.strip()) == (0, "ok")
    assert "hello" not in ok.stdout
    bad = subprocess.run(
        [*cmd, "decode", "--check"], input="hello", capture_output=True, text=True, cwd=REPO
    )
    assert bad.returncode == 1


def test_legacy_env_names_only_read_through_the_registry() -> None:
    names = {n for _, legacy in REGISTRY.values() for n in legacy}
    # "OPEN_ROUTER" is a prefix of other words; match whole quoted literals only.
    pattern = re.compile(
        r"""["'](""" + "|".join(sorted(names, key=len, reverse=True)) + r""")["']"""
    )
    # Local dev/eval scripts and the manifest checker take plain names by design.
    exempt = {
        "scripts/pipeline_eval.py", "scripts/announce.py",
        "scripts/apply_handler_script.py", "scripts/chat_eval_matrix.py",
        "scripts/code_mode_eval.py", "scripts/identity_rollout_control.py",
        "scripts/image_palette_samples.py", "scripts/prompt_opt_eval.py",
        "scripts/local_harness/config.py", "scripts/proactive_eval/smoke_models.py",
    }
    offenders: list[str] = []
    for root in ("smarter_dev", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            if path.name == "credentials.py" and path.parent.name == "shared":
                continue
            if path.relative_to(REPO).as_posix() in exempt:
                continue
            text = path.read_text(encoding="utf-8")
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{path.relative_to(REPO)}:{line} {match.group(1)}")
    assert not offenders, "read credentials via get_credential():\n" + "\n".join(offenders)
