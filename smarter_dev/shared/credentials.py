"""Opaque, encoded credentials.

Obfuscation, not encryption: it keeps env/regex scanners (``*_API_KEY``,
``sk-...``, ``AIza...``) from recognising our keys by name or shape. It does
not stop anyone who reads this file.

Each credential has an opaque env var name (``KMSVP8``) and a value stored as
``v1.`` + base64url(utf8(value) XOR keystream). The encoding must stay byte for
byte identical to the proactive-agent repo and ``services/media/src/credentials.ts``.

Every SDK/client constructor takes its key from :func:`get_credential`; nothing
else may read the legacy env names (``tests/shared/test_credentials.py``
enforces that) and nothing writes keys back into ``os.environ``.

CLI::

    printf %s "$RAW" | python -m smarter_dev.shared.credentials encode
    printf %s "$ENCODED" | python -m smarter_dev.shared.credentials decode --check
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import sys

logger = logging.getLogger(__name__)

PREFIX = "v1."
MASK = bytes.fromhex("f57d64f1e6341340da83b07d00003b1cb3478f4083d21eda")

# logical name -> (opaque env var, legacy env vars in lookup order).
# The k8s secret key is the opaque env var lowercased.
# The opaque name is ``None`` for a legacy-only entry (none at present).
REGISTRY: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "anthropic": ("KMSVP8", ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY")),
    "openai": ("IRBCKR", ("OPENAI_API_KEY",)),
    "gemini": ("IN8OID", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "openrouter": (
        "RCGW3A",
        ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY", "OPEN_ROUTER"),
    ),
    "typesafe": ("DAWTIY", ("TYPESAFE_API_KEY", "JEV_API_KEY")),
    "jina": ("IDNDWL", ("JINA_API_KEY",)),
    "brave_search": ("AO6HUE", ("BRAVE_SEARCH_API_KEY",)),
    "youtube": ("WFZ62P", ("YOUTUBE_API_KEY",)),
    "resend": ("QMH07U", ("RESEND_API_KEY",)),
    "polar_access_token": ("SIKL1V", ("POLAR_ACCESS_TOKEN",)),
    "polar_webhook_secret": ("YHVXCY", ("POLAR_WEBHOOK_SECRET",)),
    "stripe_secret_key": ("LHB3U8", ("STRIPE_SECRET_KEY",)),
    "stripe_webhook_secret": ("UM6HK8", ("STRIPE_WEBHOOK_SECRET",)),
    "logfire": ("PB9FQN", ("LOGFIRE_TOKEN",)),
    "spaces_access_key": ("RY7CUN", ("SPACES_ACCESS_KEY",)),
    "spaces_secret_key": ("IJ2XAT", ("SPACES_SECRET_KEY",)),
    "discord_bot_token": ("FHI1OT", ("DISCORD_BOT_TOKEN",)),
    "discord_client_secret": (
        "SYJN54",
        ("DISCORD_CLIENT_SECRET", "DISCORD_APPLICATION_SECRET"),
    ),
    "github_client_secret": ("RLOJVC", ("GITHUB_CLIENT_SECRET",)),
    "google_client_secret": ("ZVHJYO", ("GOOGLE_CLIENT_SECRET",)),
    "api_secret_key": ("QT9YEP", ("API_SECRET_KEY",)),
    "bot_api_key": ("FB3OSY", ("BOT_API_KEY",)),
    "media_api_key": ("HVTH7T", ("MEDIA_API_KEY",)),
    "proactive_agent_api_key": ("FUC9HA", ("PROACTIVE_AGENT_API_KEY",)),
    "site_api_key": ("TB0R54", ("SITE_API_KEY",)),
    "site_identity_fence_key": ("JSGALT", ("SITE_IDENTITY_FENCE_KEY",)),
    "secret_key": ("AOR1KG", ("SECRET_KEY",)),
    "web_session_secret": ("URH8MJ", ("WEB_SESSION_SECRET",)),
    "admin_password": ("HDEAGJ", ("ADMIN_PASSWORD",)),
}

_logged: set[str] = set()


def _keystream(length: int) -> bytes:
    out = bytearray()
    i = 0
    while len(out) < length:
        out += hashlib.sha256(MASK + i.to_bytes(4, "big")).digest()
        i += 1
    return bytes(out[:length])


def _xor(data: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, _keystream(len(data)), strict=True))


def encode_value(value: str) -> str:
    """Encode a raw credential for storage."""
    raw = _xor(value.encode("utf-8"))
    return PREFIX + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_value(encoded: str) -> str:
    """Inverse of :func:`encode_value`. Raises ``ValueError`` without the prefix."""
    if not encoded.startswith(PREFIX):
        raise ValueError("encoded credential must start with 'v1.'")
    body = encoded[len(PREFIX) :]
    if not re.fullmatch(r"[A-Za-z0-9_-]*", body):
        raise ValueError("encoded credential is malformed")
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        return _xor(raw).decode("utf-8")
    except Exception as exc:  # binascii.Error, UnicodeDecodeError
        raise ValueError("encoded credential is malformed") from exc


def _note_source(logical: str, source: str) -> None:
    if logical not in _logged:
        _logged.add(logical)
        logger.info("credential %s: source=%s", logical, source)


def get_credential(logical: str) -> str | None:
    """Return the credential for ``logical``, or ``None`` when not configured."""
    opaque, legacy = REGISTRY[logical]
    encoded = os.environ.get(opaque, "").strip() if opaque else ""
    if encoded:
        try:
            value = decode_value(encoded).strip()
        except ValueError:
            logger.error("credential %s: %s is not a valid v1 value", logical, opaque)
        else:
            if value:
                _note_source(logical, "opaque")
                return value
            logger.warning(
                "credential %s: %s decodes to an empty value; using legacy names",
                logical,
                opaque,
            )
    # --- PHASE 1 LEGACY FALLBACK: delete this block in phase 2 ---
    for name in legacy:
        value = os.environ.get(name, "").strip()
        if value:
            _note_source(logical, "legacy")
            return value
    # --- END PHASE 1 LEGACY FALLBACK ---
    _note_source(logical, "missing")
    return None


def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in ("encode", "decode"):
        print("usage: credentials (encode | decode --check) < value", file=sys.stderr)
        return 2
    data = sys.stdin.read().rstrip("\r\n")
    if argv[1] == "encode":
        print(encode_value(data))
        return 0
    if "--check" not in argv[2:]:
        print("decode only supports --check (the value is never printed)", file=sys.stderr)
        return 2
    try:
        ok = encode_value(decode_value(data)) == data
    except ValueError:
        ok = False
    print("ok" if ok else "invalid")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
