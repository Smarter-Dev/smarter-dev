"""Pure placeholder substitution + render-time validation.

Every install / config-edit / update funnels through :func:`render_bundle`,
which produces the concrete admin-handler artifacts (script, settings,
channel_ids) for a config and re-runs every static rail against the *actual*
config values. It has no I/O and no session: it raises :class:`RenderError`
before the caller does any DB work, so a failed render can never leave partial
rows behind.

The injection guard is that ``channel_id``/``role_id`` values are validated
against the Discord-snowflake regex and only then emitted as quoted literals —
a value can never contain a quote, newline, or code fragment. ``string`` values
go through :func:`json.dumps`, which handles all escaping.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

from smarter_dev.extensions.schema import ConfigField
from smarter_dev.extensions.schema import ExtensionManifest
from smarter_dev.extensions.schema import HandlerTemplate
from smarter_dev.web.handler_lint import lint_script
from smarter_dev.web.handler_schedule import ScheduleError
from smarter_dev.web.handler_schedule import first_fire_at
from smarter_dev.web.handler_schedule import validate_time_trigger_settings

# A Discord snowflake: 15–20 digits, nothing else. This is the injection guard
# for ids emitted as quoted literals.
_SNOWFLAKE_RE = re.compile(r"^[0-9]{15,20}$")
_INT_RE = re.compile(r"^-?[0-9]+$")

_PLACEHOLDER_RE = re.compile(r"\{\{\s*cfg\.([a-z][a-z0-9_]*)\s*\}\}")
# After substitution nothing beginning ``{{cfg.`` may survive — catches a typo
# like ``{{cfg.foo}`` that the strict placeholder regex skipped.
_LEFTOVER_MARKER = "{{cfg."

_MAX_STRING_LEN = 500

# The string-literal role id passed as the SECOND argument of add_role/remove_role
# (the grant target). Same family as handler_lint._ROLE_ID_LITERAL, but capturing
# the id so the allowlist closure can be checked.
_GRANTED_ROLE_RE = re.compile(
    r"\b(?:add_role|remove_role)\s*\(\s*[^,()]+,\s*['\"](?P<role>[^'\"]+)['\"]"
)

# Token whose presence means a handler spawns an agent (tighter interval floor).
_AGENT_TOKEN = "spawn_agent("

_TIME_TRIGGERS = ("schedule", "timer")


class RenderError(Exception):
    """A config or template could not be rendered into valid handler rows.

    ``str(exc)`` is a one-line, user-facing reason (flash-ready).
    """


@dataclass(frozen=True)
class RenderedHandler:
    """One handler's concrete, validated artifacts, ready to write to a row."""

    key: str
    name: str
    trigger_type: str
    description: str
    script: str
    settings: dict
    channel_ids: list[str]


def _fields_by_name(manifest: ExtensionManifest) -> dict[str, ConfigField]:
    return {field.name: field for field in manifest.config}


def validate_config_values(manifest: ExtensionManifest, config: dict) -> dict:
    """Coerce + validate a raw config against the schema; return the cleaned dict.

    Unknown keys and missing required fields raise :class:`RenderError`. A
    missing optional field falls back to its default (or is omitted when the
    default is None). Values arrive untyped from the install form (all-string
    except bools), so int fields accept a numeric string and are coerced.
    """
    fields = _fields_by_name(manifest)
    unknown = set(config) - set(fields)
    if unknown:
        raise RenderError(f"unknown config field(s): {', '.join(sorted(unknown))}")

    cleaned: dict = {}
    for name, field in fields.items():
        if name in config:
            cleaned[name] = _clean_value(field, config[name])
        elif field.required:
            raise RenderError(f"missing required config field {name!r}")
        elif field.default is not None:
            cleaned[name] = _clean_value(field, field.default)
    return cleaned


def _clean_value(field: ConfigField, value: object) -> str | int | bool:
    if field.type in ("channel_id", "role_id"):
        if not isinstance(value, str) or not _SNOWFLAKE_RE.fullmatch(value):
            raise RenderError(
                f"config field {field.name!r} must be a Discord id (15–20 digits)"
            )
        return value
    if field.type == "string":
        if not isinstance(value, str):
            raise RenderError(f"config field {field.name!r} must be a string")
        if len(value) > _MAX_STRING_LEN:
            raise RenderError(
                f"config field {field.name!r} is too long (max {_MAX_STRING_LEN})"
            )
        if "\x00" in value:
            raise RenderError(f"config field {field.name!r} contains a NUL byte")
        return value
    if field.type == "int":
        # bool is an int subclass — reject it before the int branch.
        if isinstance(value, bool):
            raise RenderError(f"config field {field.name!r} must be an int")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and _INT_RE.fullmatch(value.strip()):
            return int(value.strip())
        raise RenderError(f"config field {field.name!r} must be an int")
    # bool
    if not isinstance(value, bool):
        raise RenderError(f"config field {field.name!r} must be a boolean")
    return value


def _literal(field_type: str, value: str | int | bool) -> str:
    """Render one cleaned value as a Monty/Python literal token."""
    if field_type in ("channel_id", "role_id"):
        # value is snowflake-validated — safe to wrap in quotes directly.
        return '"' + str(value) + '"'
    if field_type == "string":
        return json.dumps(value)
    if field_type == "int":
        return str(int(value))
    return "True" if value else "False"


def render_script(template: str, manifest: ExtensionManifest, config: dict) -> str:
    """Substitute every ``{{cfg.*}}`` in a script with a typed literal token."""
    fields = _fields_by_name(manifest)

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        field = fields.get(name)
        if field is None:
            raise RenderError(f"script references undeclared config field {name!r}")
        return _literal(field.type, config[name])

    return _PLACEHOLDER_RE.sub(_replace, template)


def render_settings(
    settings_template: dict, manifest: ExtensionManifest, config: dict
) -> dict:
    """Recursively render a settings template.

    A string that is EXACTLY one placeholder is replaced by the *typed* value
    (so ``{"interval_seconds": "{{cfg.every}}"}`` yields a real int and
    ``["{{cfg.staff_role_id}}"]`` yields a list of str). A string with embedded
    placeholders gets ``str(value)`` spliced in.
    """
    return _render_node(settings_template, manifest, config)


def _render_node(node: object, manifest: ExtensionManifest, config: dict) -> object:
    if isinstance(node, dict):
        return {
            key: _render_node(value, manifest, config) for key, value in node.items()
        }
    if isinstance(node, list):
        return [_render_node(item, manifest, config) for item in node]
    if isinstance(node, str):
        return _render_string_value(node, manifest, config)
    return node


def _render_string_value(
    value: str, manifest: ExtensionManifest, config: dict
) -> str | int | bool:
    fields = _fields_by_name(manifest)
    exact = _PLACEHOLDER_RE.fullmatch(value.strip())
    if exact is not None:
        name = exact.group(1)
        field = fields.get(name)
        if field is None:
            raise RenderError(f"settings reference undeclared config field {name!r}")
        return config[name]

    def _replace(match: re.Match) -> str:
        name = match.group(1)
        field = fields.get(name)
        if field is None:
            raise RenderError(f"settings reference undeclared config field {name!r}")
        return str(config[name])

    return _PLACEHOLDER_RE.sub(_replace, value)


def extract_granted_role_literals(script: str) -> set[str]:
    """The string-literal role ids granted by add_role/remove_role in ``script``."""
    return {match.group("role") for match in _GRANTED_ROLE_RE.finditer(script)}


def rendered_handler(
    handler: HandlerTemplate,
    manifest: ExtensionManifest,
    config: dict,
    script_template: str,
) -> RenderedHandler:
    """Render one handler's script, settings, and channel scope (pure)."""
    script = render_script(script_template, manifest, config)
    settings = render_settings(handler.settings, manifest, config)
    channel_ids = [config[name] for name in handler.channel_scope]
    return RenderedHandler(
        key=handler.key,
        name=handler.name.strip(),
        trigger_type=handler.trigger_type,
        description=handler.description,
        script=script,
        settings=settings,
        channel_ids=channel_ids,
    )


def render_bundle(
    manifest: ExtensionManifest, config: dict, scripts: dict[str, str]
) -> list[RenderedHandler]:
    """Validate the config, render every handler, and run every render-time rail.

    ``scripts`` maps handler key -> raw template text (loaded by the registry).
    Raises :class:`RenderError` before any handler is returned, so the install
    service can call it first and know a failure left nothing behind.
    """
    cleaned = validate_config_values(manifest, config)
    rendered: list[RenderedHandler] = []
    for handler in manifest.handlers:
        template = scripts.get(handler.key)
        if template is None:
            raise RenderError(f"handler {handler.key!r} has no loaded script")
        item = rendered_handler(handler, manifest, cleaned, template)
        _check_rendered(item)
        rendered.append(item)
    return rendered


def _check_rendered(item: RenderedHandler) -> None:
    # No placeholder may survive substitution anywhere.
    if _LEFTOVER_MARKER in item.script:
        raise RenderError(
            f"handler {item.key!r}: a malformed placeholder survived rendering"
        )
    if _LEFTOVER_MARKER in json.dumps(item.settings):
        raise RenderError(
            f"handler {item.key!r}: a malformed placeholder survived in settings"
        )

    reason = lint_script(item.script)
    if reason is not None:
        raise RenderError(f"handler {item.key!r} failed lint: {reason}")

    granted = extract_granted_role_literals(item.script)
    if granted:
        allowlist = set(item.settings.get("allowed_role_ids") or [])
        missing = granted - allowlist
        if missing:
            raise RenderError(
                f"handler {item.key!r} grants role(s) {sorted(missing)} not present "
                "in settings.allowed_role_ids"
            )

    if item.trigger_type in _TIME_TRIGGERS:
        _check_schedule(item)


def _check_schedule(item: RenderedHandler) -> None:
    try:
        validate_time_trigger_settings(
            item.trigger_type,
            item.settings,
            uses_agent=_AGENT_TOKEN in item.script,
        )
        first_fire_at(item.trigger_type, item.settings, datetime.now(UTC))
    except ScheduleError as exc:
        raise RenderError(f"handler {item.key!r} schedule invalid: {exc}") from exc
