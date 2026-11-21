"""Resolve credential ``$VAR`` references in Skrift's app.yaml through get_credential.

Skrift fills ``$VAR`` placeholders from ``os.environ`` and offers no hook for
that, and the credential env vars now hold ``v1.``-encoded values under opaque
names. :func:`install` swaps ``skrift.config.interpolate_env_vars`` (looked up
by ``load_app_config`` at call time) for one that sends a placeholder naming a
registered credential to :func:`get_credential`. Every other placeholder
(``$DATABASE_URL``, ``$REDIS_URL``, ids) still reads ``os.environ``.

Decoded values are handed to Skrift's settings object only; nothing is written
back into ``os.environ``.

Call :func:`install` before Skrift loads its settings: ``main.py`` does, and
``python -m smarter_dev.shared.skrift_secrets <skrift args>`` runs the ``skrift``
CLI (workers, migrations) with it installed.
"""

from __future__ import annotations

import os
import sys

from smarter_dev.shared.credentials import REGISTRY
from smarter_dev.shared.credentials import get_credential

_BY_NAME: dict[str, str] = {}
for _logical, (_opaque, _legacy) in REGISTRY.items():
    for _name in ((_opaque,) if _opaque else ()) + _legacy:
        _BY_NAME[_name] = _logical

_installed = False


def _lookup(var: str) -> str | None:
    logical = _BY_NAME.get(var)
    if logical is not None:
        return get_credential(logical)
    return os.environ.get(var)


def interpolate_env_vars(value, strict: bool = True, _path: str = ""):
    """Skrift's ``interpolate_env_vars`` with credential names resolved."""
    from skrift.config import ENV_VAR_PATTERN

    if isinstance(value, str):

        def replace(match):
            var = match.group(1)
            val = _lookup(var)
            if val is None:
                if strict:
                    location = f" (in {_path})" if _path else ""
                    raise ValueError(
                        f"Environment variable ${var} is not set{location}.\n"
                        f"  Hint: Set it in your .env file or shell environment."
                    )
                return match.group(0)
            return val

        return ENV_VAR_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {
            k: interpolate_env_vars(v, strict, f"{_path}.{k}" if _path else k)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            interpolate_env_vars(item, strict, f"{_path}[{i}]")
            for i, item in enumerate(value)
        ]
    return value


def _with_credential_secret_key(base):
    """``base`` with ``secret_key`` taken from the ``secret_key`` credential.

    Skrift's ``Settings.secret_key`` is required and read by pydantic-settings
    straight from env ``SECRET_KEY``, which no longer exists in prod. Outside
    dev the opaque value is the only source (a plain ``SECRET_KEY`` is ignored);
    in dev ``get_credential`` also accepts the plain name.
    """
    from pydantic import model_validator

    class SkriftSettings(base):
        @model_validator(mode="before")
        @classmethod
        def _secret_key_from_credential(cls, data):
            data = dict(data) if isinstance(data, dict) else {}
            value = get_credential("secret_key")
            if value:
                data["secret_key"] = value
            else:
                from smarter_dev.shared.credentials import require_credential

                require_credential("secret_key")  # raises MissingCredentialError
            return data

    SkriftSettings.__name__ = base.__name__
    return SkriftSettings


def install() -> None:
    """Patch Skrift's interpolation and secret key once; safe to call again."""
    global _installed
    if _installed:
        return
    import skrift.config

    skrift.config.interpolate_env_vars = interpolate_env_vars
    original = skrift.config._settings_model
    cache: dict = {}

    def _settings_model():
        base = original()
        if base not in cache:
            cache[base] = _with_credential_secret_key(base)
        return cache[base]

    skrift.config._settings_model = _settings_model
    skrift.config.get_settings.cache_clear()
    _installed = True


def main(argv: list[str]) -> int:
    install()
    from skrift.cli import cli

    cli(args=argv[1:], prog_name="skrift")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
