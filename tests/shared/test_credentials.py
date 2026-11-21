"""Tests for smarter_dev.shared.credentials.

The vectors below are shared with services/media/test/unit/credentials.test.ts
and the proactive-agent port; change them in all three places or none.
"""

from __future__ import annotations

import logging
import os
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


def _clear(monkeypatch: pytest.MonkeyPatch, logical: str, environment: str) -> None:
    opaque, legacy = REGISTRY[logical]
    for name in ((opaque,) if opaque else ()) + legacy:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ENVIRONMENT", environment)
    credentials._logged.discard(logical)


def test_values_are_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "media_api_key", "production")
    monkeypatch.setenv("HVTH7T", encode_value("opaque-value\n"))
    assert get_credential("media_api_key") == "opaque-value"
    _clear(monkeypatch, "media_api_key", "development")
    monkeypatch.setenv("MEDIA_API_KEY", "  legacy-value \n")
    assert get_credential("media_api_key") == "legacy-value"


@pytest.mark.parametrize("environment", ["production", "staging", "prod", ""])
def test_legacy_names_and_plain_values_are_ignored_outside_dev(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """Every credential with an opaque name, not just one."""
    for logical, (opaque, legacy) in REGISTRY.items():
        _clear(monkeypatch, logical, environment)
        for name in legacy:
            monkeypatch.setenv(name, "legacy-value")
        assert get_credential(logical) is None, logical
        if opaque:
            monkeypatch.setenv(opaque, "plain-value-without-prefix")
            assert get_credential(logical) is None, logical
            monkeypatch.setenv(opaque, encode_value("opaque-value"))
            assert get_credential(logical) == "opaque-value", logical
        for name in legacy:
            monkeypatch.delenv(name)


def test_unset_environment_is_not_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "jina", "production")
    monkeypatch.delenv("ENVIRONMENT")
    monkeypatch.setenv("JINA_API_KEY", "legacy")
    assert get_credential("jina") is None


@pytest.mark.parametrize("environment", ["development", "test", "testing", "Development "])
def test_dev_accepts_legacy_names_and_plain_opaque_values(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    _clear(monkeypatch, "jina", environment)
    monkeypatch.setenv("JINA_API_KEY", "legacy")
    assert get_credential("jina") == "legacy"
    monkeypatch.setenv("IDNDWL", "plain-in-opaque-name")
    assert get_credential("jina") == "plain-in-opaque-name"


def test_blank_opaque_is_ignored_in_production_and_falls_back_in_dev(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear(monkeypatch, "media_api_key", "production")
    monkeypatch.setenv("HVTH7T", encode_value("  \n"))
    monkeypatch.setenv("MEDIA_API_KEY", "legacy-value")
    with caplog.at_level(logging.WARNING, logger=credentials.logger.name):
        assert get_credential("media_api_key") is None
    assert any(
        r.levelno == logging.WARNING and "HVTH7T" in r.getMessage()
        for r in caplog.records
    )
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert get_credential("media_api_key") == "legacy-value"


def test_opaque_wins_over_legacy_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "media_api_key", "development")
    monkeypatch.setenv("MEDIA_API_KEY", "legacy-value")
    monkeypatch.setenv("HVTH7T", encode_value("opaque-value"))
    assert get_credential("media_api_key") == "opaque-value"


def test_dev_legacy_names_in_lookup_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "gemini", "development")
    monkeypatch.setenv("GOOGLE_API_KEY", "second")
    assert get_credential("gemini") == "second"
    monkeypatch.setenv("GEMINI_API_KEY", "first")
    assert get_credential("gemini") == "first"


def test_missing_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "jina", "production")
    assert get_credential("jina") is None


def test_invalid_v1_opaque_is_none_in_production_and_legacy_in_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear(monkeypatch, "jina", "production")
    monkeypatch.setenv("IDNDWL", "v1.!!!")
    monkeypatch.setenv("JINA_API_KEY", "legacy")
    assert get_credential("jina") is None
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert get_credential("jina") == "legacy"


def test_settings_drop_plain_env_values_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    from smarter_dev.shared.config import Settings

    _clear(monkeypatch, "media_api_key", "production")
    monkeypatch.setenv("MEDIA_API_KEY", "legacy-value")
    assert Settings(_env_file=None).media_api_key == ""
    monkeypatch.setenv("HVTH7T", encode_value("opaque-value"))
    assert Settings(_env_file=None).media_api_key == "opaque-value"
    _clear(monkeypatch, "media_api_key", "development")
    monkeypatch.setenv("MEDIA_API_KEY", "legacy-value")
    assert Settings(_env_file=None).media_api_key == "legacy-value"


def test_source_logged_once_and_never_the_value(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear(monkeypatch, "youtube", "production")
    monkeypatch.setenv("WFZ62P", encode_value("super-secret-value"))
    with caplog.at_level(logging.INFO, logger=credentials.logger.name):
        get_credential("youtube")
        get_credential("youtube")
    assert [r.getMessage() for r in caplog.records] == [
        "credential youtube: source=opaque"
    ]
    assert "super-secret-value" not in caplog.text


def test_no_writeback_to_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch, "typesafe", "development")
    monkeypatch.setenv("JEV_API_KEY", "legacy-test-key")
    assert get_credential("typesafe") == "legacy-test-key"
    import os

    assert "TYPESAFE_API_KEY" not in os.environ


def test_registry_names_are_unique_and_opaque() -> None:
    opaque = [o for o, _ in REGISTRY.values() if o]
    assert len(set(opaque)) == len(opaque)
    assert all(re.fullmatch(r"[A-Z0-9]{6}", o) for o in opaque)


def test_digitalocean_and_zen_are_not_registered() -> None:
    assert "digitalocean_inference" not in REGISTRY
    assert "opencode_zen" not in REGISTRY


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


def _legacy_names() -> list[str]:
    return sorted(
        {n for _, legacy in REGISTRY.values() for n in legacy}, key=len, reverse=True
    )


def test_legacy_env_names_only_read_through_the_registry() -> None:
    """No non-test Python names a legacy credential env var, anywhere."""
    # "OPEN_ROUTER" is a prefix of other words; match whole quoted literals only.
    pattern = re.compile(r"""["'](""" + "|".join(_legacy_names()) + r""")["']""")
    # The local harness *writes* dummy dev values into the env of the child
    # processes it launches (ENVIRONMENT=development); it reads nothing.
    exempt = {"scripts/local_harness/config.py"}
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


def test_manifests_and_app_yaml_use_no_legacy_credential_names() -> None:
    """k8s env entries and Skrift ``$VAR`` references name opaque vars only."""
    names = "|".join(_legacy_names())
    pattern = re.compile(r"(?:name:\s*|\$)(" + names + r")\b")
    offenders: list[str] = []
    files = [*(REPO / "k8s").glob("*.yaml"), *REPO.glob("app*.yaml")]
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for match in pattern.finditer(line):
                offenders.append(f"{path.relative_to(REPO)}:{number} {match.group(1)}")
    assert not offenders, "\n".join(offenders)


def test_manifests_do_not_mount_legacy_secret_keys() -> None:
    legacy_keys = {n.lower().replace("_", "-") for n in _legacy_names()}
    pattern = re.compile(r"^\s*key:\s*(\S+)\s*$")
    offenders = [
        f"{path.relative_to(REPO)}:{number} {match.group(1)}"
        for path in (REPO / "k8s").glob("*.yaml")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if (match := pattern.match(line)) and match.group(1) in legacy_keys
    ]
    assert not offenders, "\n".join(offenders)


def test_media_service_reads_only_the_opaque_name_outside_dev() -> None:
    source = (REPO / "services/media/src/config.ts").read_text(encoding="utf-8")
    assert "PHASE 1" not in source
    assert source.count("MEDIA_API_KEY") == 1  # the development-only branch


def _non_test_sources():
    for root in ("smarter_dev", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            rel = path.relative_to(REPO).as_posix()
            if rel == "smarter_dev/shared/credentials.py":
                continue
            yield rel, path.read_text(encoding="utf-8")


def test_no_bare_provider_model_strings_in_non_test_code() -> None:
    """A ``"provider:model"`` string makes pydantic-ai build the provider and
    read the SDK's env var itself. Only skrift_models names agents that way
    (it resolves them with an explicit key)."""
    bare = re.compile(
        r"""["'](anthropic|openai|openai-responses|openai-chat|google-gla|google-vertex"""
        r"""|openrouter|gemini|groq|mistral)\:[A-Za-z0-9][A-Za-z0-9._/-]*["']"""
    )
    exempt = {"smarter_dev/shared/skrift_models.py"}
    offenders = [
        f"{rel}:{text.count(chr(10), 0, m.start()) + 1} {m.group(0)}"
        for rel, text in _non_test_sources()
        if rel not in exempt
        for m in bare.finditer(text)
        # llm_pricing documents the name shape in a docstring.
        if rel != "smarter_dev/web/llm_pricing.py"
    ]
    assert not offenders, "\n".join(offenders)


def test_no_provider_built_without_an_api_key() -> None:
    bare = re.compile(
        r"\b(Anthropic|OpenAI|Google|OpenRouter|Groq|Mistral)Provider\(\s*\)"
    )
    offenders = [
        f"{rel}:{text.count(chr(10), 0, m.start()) + 1}"
        for rel, text in _non_test_sources()
        for m in bare.finditer(text)
    ]
    assert not offenders, "\n".join(offenders)


def test_unkeyed_infer_model_and_model_name_constructors_are_gone() -> None:
    """``infer_model(<str>)`` and ``OpenAIResponsesModel(name)`` with no
    provider read the SDK env; every remaining call site is on this list."""
    allowed_infer = {"smarter_dev/shared/usage_meter.py"}
    offenders: list[str] = []
    for rel, text in _non_test_sources():
        if "infer_model(" in text and rel not in allowed_infer:
            offenders.append(f"{rel}: infer_model(")
        for m in re.finditer(
            r"\b(OpenAIResponsesModel|OpenAIChatModel|AnthropicModel|GoogleModel)\(([^()]*(?:\([^()]*\)[^()]*)*)\)",
            text,
        ):
            if "provider" not in m.group(2):
                offenders.append(
                    f"{rel}:{text.count(chr(10), 0, m.start()) + 1} {m.group(1)}"
                )
    assert not offenders, "\n".join(offenders)


def test_skrift_agent_names_resolve_with_explicit_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smarter_dev.shared.agent_model_resolver import resolve_model_name
    from smarter_dev.shared.skrift_models import agent_capabilities

    for env in (
        "ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "OPENAI_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv("KMSVP8", encode_value("sk-ant-test-key"))
    monkeypatch.setenv("IRBCKR", encode_value("sk-openai-test-key"))
    monkeypatch.setenv("IN8OID", encode_value("gem-test-key"))

    anthropic = resolve_model_name("anthropic:claude-haiku-5-5")
    assert anthropic.client.api_key == "sk-ant-test-key"
    openai = resolve_model_name("openai-responses:gpt-6")
    assert openai.client.api_key == "sk-openai-test-key"
    google = resolve_model_name("google-gla:gemini-3.1-flash-lite-preview")
    assert google.client._api_client.api_key == "gem-test-key"
    with pytest.raises(ValueError):
        resolve_model_name("mystery:model")

    # Skrift passes this to the pydantic-ai Agent in the worker.
    (capability,) = list(agent_capabilities())
    assert capability.has_resolve_model_id


def test_usage_meter_wraps_a_model_name_with_the_explicit_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smarter_dev.shared.usage_meter import UsageMeter

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("KMSVP8", encode_value("sk-ant-test-key"))
    wrapped = UsageMeter().wrap("anthropic:claude-haiku-5-5")
    assert wrapped.wrapped.client.api_key == "sk-ant-test-key"


def test_every_settings_field_named_like_a_legacy_name_is_registry_backed() -> None:
    """A pydantic-settings field ``foo_key`` reads plain ``FOO_KEY`` and ``.env``.

    Any field whose upper-cased name is a legacy credential name (or an opaque
    one) must be in ``CREDENTIAL_FIELDS`` so prod drops the plain value.
    """
    from smarter_dev.shared.config import CREDENTIAL_FIELDS
    from smarter_dev.shared.config import Settings

    names = set(_legacy_names()) | {o for o, _ in REGISTRY.values() if o}
    fields = {f for f in Settings.model_fields if f.upper() in names}
    assert fields <= set(CREDENTIAL_FIELDS), sorted(
        fields - set(CREDENTIAL_FIELDS)
    )
    assert set(CREDENTIAL_FIELDS.values()) <= set(REGISTRY)


def test_settings_ignore_plain_env_and_dotenv_for_every_credential_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from smarter_dev.shared.config import CREDENTIAL_FIELDS
    from smarter_dev.shared.config import Settings

    for logical, (opaque, legacy) in REGISTRY.items():
        _clear(monkeypatch, logical, "production")
    env_file = tmp_path / ".env"
    lines = []
    for field in CREDENTIAL_FIELDS:
        monkeypatch.setenv(field.upper(), "plain-env-value-" + "x" * 40)
        lines.append(f"{field.upper()}=plain-dotenv-value-" + "x" * 40)
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for source in (None, env_file):
        settings = Settings(_env_file=source)
        for field in CREDENTIAL_FIELDS:
            assert "plain" not in str(getattr(settings, field)), field


def test_model_and_client_builders_raise_on_a_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty key would make the SDK read the plain env var itself."""
    from smarter_dev.shared import agent_model_resolver
    from smarter_dev.shared.credentials import MissingCredentialError
    from smarter_dev.shared.credentials import require_credential

    for logical in REGISTRY:
        _clear(monkeypatch, logical, "production")
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.setenv(name, "plain-env-value-" + "x" * 40)
    with pytest.raises(MissingCredentialError):
        require_credential("openai")
    for model_id in ("anthropic:x", "openai-responses:x", "google-gla:x"):
        with pytest.raises(MissingCredentialError):
            agent_model_resolver.resolve_model_name(model_id)


def test_no_builder_passes_an_empty_default_api_key() -> None:
    """``api_key=get_credential(...)`` or ``... or ""`` lets SDKs read the env."""
    pattern = re.compile(r"""[(,]\s*api_key=get_credential\(""")
    offenders = [
        f"{path.relative_to(REPO)}:{text.count(chr(10), 0, m.start()) + 1}"
        for root in ("smarter_dev", "scripts")
        for path in (REPO / root).rglob("*.py")
        if path.name != "credentials.py"
        for text in [path.read_text(encoding="utf-8")]
        for m in pattern.finditer(text)
    ]
    assert not offenders, "use require_credential() for SDK keys:\n" + "\n".join(offenders)


def _production_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for logical in REGISTRY:
        _clear(monkeypatch, logical, "production")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("AOR1KG", encode_value("s" * 40))


def test_skrift_secret_key_comes_from_the_opaque_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Skrift requires ``secret_key``; prod has no ``SECRET_KEY`` env var."""
    script = (
        "import os\n"
        "from smarter_dev.shared.skrift_secrets import install\n"
        "install()\n"
        "from skrift.config import get_settings\n"
        "s = get_settings()\n"
        "assert s.secret_key == 's' * 40, 'wrong secret_key'\n"
        "assert 'SECRET_KEY' not in os.environ\n"
        "print('ok')\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "ENVIRONMENT": "production",
        "AOR1KG": encode_value("s" * 40),
        "DATABASE_URL": "sqlite+aiosqlite:///x.db",
        "REDIS_URL": "redis://localhost:6379/0",
        "DISCORD_APPLICATION_ID": "1",
        "GITHUB_CLIENT_ID": "1",
        "GOOGLE_CLIENT_ID": "1",
        **{n: encode_value("v-" + n) for n in ("IJ2XAT", "RLOJVC", "RY7CUN", "SYJN54", "ZVHJYO")},
    }
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr[-1500:]
    assert result.stdout.strip() == "ok"
    # A plain SECRET_KEY is ignored in production, and a missing one is an error.
    env2 = {**env, "SECRET_KEY": "plain-" + "x" * 40}
    del env2["AOR1KG"]
    bad = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO, env=env2, capture_output=True, text=True
    )
    assert bad.returncode != 0
    assert "MissingCredentialError" in bad.stderr


def test_signing_secrets_are_blank_not_public_defaults_outside_dev(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from smarter_dev.shared.config import Settings
    from smarter_dev.shared.credentials import MissingCredentialError
    from smarter_dev.shared.credentials import require_nonblank

    _production_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.api_secret_key == ""
    assert settings.web_session_secret == ""
    with pytest.raises(MissingCredentialError):
        require_nonblank(settings.api_secret_key, "api_secret_key")
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert require_nonblank("", "api_secret_key") == ""
