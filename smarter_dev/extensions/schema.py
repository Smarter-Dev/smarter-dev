"""Pydantic manifest models for the extension catalog.

A catalog entry declares one :class:`ExtensionManifest` — its config schema
(:class:`ConfigField`) and one or more handler templates
(:class:`HandlerTemplate`). The models are frozen and self-validating: a
malformed manifest raises ``ValueError`` at construction, which the registry
turns into a startup failure (so a broken manifest can never ship). The
trigger vocabulary and the schedule-key vocabulary are imported from their
single sources of truth so the manifest can never drift from what the
``admin_handlers`` rails enforce.
"""

from __future__ import annotations

import re

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import field_validator
from pydantic import model_validator

from smarter_dev.web.models import ADMIN_HANDLER_TRIGGER_TYPES

# The declarable config field types. ``channel_id``/``role_id`` are Discord
# snowflakes (validated + emitted as quoted literals — the injection guard);
# the rest are plain scalars.
CONFIG_FIELD_TYPES = ("channel_id", "role_id", "string", "int", "bool")

# Time-trigger settings vocabulary, from ``handler_schedule.py`` (NOT cron). Each
# maps to the python type its value must resolve to after rendering.
_SCHEDULE_TIMING_KEYS = {
    "schedule": {"interval_seconds": "int", "daily_time": "string"},
    "timer": {"delay_seconds": "int", "fire_at": "string"},
}
_SCHEDULE_OPTIONAL_KEYS = {"schedule": {"start_at": "string"}, "timer": {}}
_TIME_TRIGGERS = tuple(_SCHEDULE_TIMING_KEYS)

_CONFIG_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_HANDLER_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")

# ``{{cfg.<field>}}`` — ``cfg.`` is mandatory so a bare ``{{x}}`` (or a Monty
# set/dict literal) never collides with the placeholder syntax.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*cfg\.([a-z][a-z0-9_]*)\s*\}\}")


def _exact_placeholder(text: str) -> str | None:
    """If ``text`` is EXACTLY one placeholder, the referenced field name; else None."""
    match = _PLACEHOLDER_RE.fullmatch(text.strip())
    return match.group(1) if match is not None else None


class ConfigField(BaseModel):
    """One admin-supplied config value, and how the install form should render it."""

    model_config = ConfigDict(frozen=True)

    name: str
    type: str
    label: str
    help: str = ""
    required: bool = True
    default: str | int | bool | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _CONFIG_NAME_RE.fullmatch(value):
            raise ValueError(
                f"config field name {value!r} must match ^[a-z][a-z0-9_]{{0,39}}$"
            )
        return value

    @field_validator("type")
    @classmethod
    def _valid_type(cls, value: str) -> str:
        if value not in CONFIG_FIELD_TYPES:
            raise ValueError(
                f"config field type {value!r} is not one of {CONFIG_FIELD_TYPES}"
            )
        return value

    @model_validator(mode="after")
    def _default_matches_type(self) -> ConfigField:
        if self.default is None:
            return self
        # bool is an int subclass — check it first so a bool never satisfies int.
        if self.type in ("channel_id", "role_id", "string"):
            if not isinstance(self.default, str):
                raise ValueError(f"config field {self.name!r} default must be a string")
        elif self.type == "int":
            if isinstance(self.default, bool) or not isinstance(self.default, int):
                raise ValueError(f"config field {self.name!r} default must be an int")
        elif self.type == "bool":
            if not isinstance(self.default, bool):
                raise ValueError(f"config field {self.name!r} default must be a bool")
        return self


class HandlerTemplate(BaseModel):
    """One admin-handler row to materialise, with ``{{cfg.*}}`` placeholders."""

    model_config = ConfigDict(frozen=True)

    key: str
    name: str
    trigger_type: str
    description: str
    script_file: str
    settings: dict = {}
    channel_scope: list[str] = []

    @field_validator("key")
    @classmethod
    def _valid_key(cls, value: str) -> str:
        if not _HANDLER_KEY_RE.fullmatch(value):
            raise ValueError(
                f"handler key {value!r} must match ^[a-z][a-z0-9_-]{{0,39}}$"
            )
        return value

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("handler name is required")
        if len(stripped) > 64:
            raise ValueError(f"handler name {value!r} is too long (max 64)")
        return value

    @field_validator("trigger_type")
    @classmethod
    def _valid_trigger(cls, value: str) -> str:
        if value not in ADMIN_HANDLER_TRIGGER_TYPES:
            raise ValueError(f"unknown trigger_type {value!r}")
        return value


class ExtensionManifest(BaseModel):
    """A catalog extension: config schema plus one or more handler templates."""

    model_config = ConfigDict(frozen=True)

    slug: str
    title: str
    summary: str
    version: int
    config: list[ConfigField]
    handlers: list[HandlerTemplate]
    example_config: dict

    @field_validator("slug")
    @classmethod
    def _valid_slug(cls, value: str) -> str:
        if not _SLUG_RE.fullmatch(value):
            raise ValueError(
                f"extension slug {value!r} must match ^[a-z][a-z0-9-]{{0,63}}$"
            )
        return value

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: int) -> int:
        if value < 1:
            raise ValueError("extension version must be >= 1")
        return value

    @field_validator("handlers")
    @classmethod
    def _at_least_one_handler(
        cls, value: list[HandlerTemplate]
    ) -> list[HandlerTemplate]:
        if not value:
            raise ValueError("an extension must declare at least one handler")
        return value

    @model_validator(mode="after")
    def _cross_checks(self) -> ExtensionManifest:
        field_types = {field.name: field.type for field in self.config}
        if len(field_types) != len(self.config):
            raise ValueError("duplicate config field names")

        keys = [handler.key for handler in self.handlers]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate handler keys")
        names = [handler.name.strip() for handler in self.handlers]
        if len(set(names)) != len(names):
            raise ValueError("duplicate handler names")

        for handler in self.handlers:
            self._check_handler(handler, field_types)
        return self

    @staticmethod
    def _check_handler(handler: HandlerTemplate, field_types: dict[str, str]) -> None:
        # include_bot_messages only means anything on a message trigger — the
        # exact rail create_admin_handler enforces (_reject_bot_optin_on_non_message).
        if (
            handler.settings.get("include_bot_messages")
            and handler.trigger_type != "message"
        ):
            raise ValueError(
                f"handler {handler.key!r}: include_bot_messages is only valid on "
                "message-trigger handlers"
            )

        for scoped in handler.channel_scope:
            if field_types.get(scoped) != "channel_id":
                raise ValueError(
                    f"handler {handler.key!r}: channel_scope entry {scoped!r} "
                    "must name a declared channel_id config field"
                )

        if handler.trigger_type in _TIME_TRIGGERS:
            ExtensionManifest._check_timing(handler, field_types)

    @staticmethod
    def _check_timing(handler: HandlerTemplate, field_types: dict[str, str]) -> None:
        allowed = _SCHEDULE_TIMING_KEYS[handler.trigger_type]
        present = [key for key in allowed if key in handler.settings]
        if len(present) != 1:
            raise ValueError(
                f"handler {handler.key!r}: a {handler.trigger_type} handler's "
                f"settings must contain exactly one of {tuple(allowed)}"
            )
        ExtensionManifest._check_timing_value(
            handler, present[0], allowed[present[0]], field_types
        )
        for key, expected in _SCHEDULE_OPTIONAL_KEYS[handler.trigger_type].items():
            if key in handler.settings:
                ExtensionManifest._check_timing_value(
                    handler, key, expected, field_types
                )

    @staticmethod
    def _check_timing_value(
        handler: HandlerTemplate,
        key: str,
        expected: str,
        field_types: dict[str, str],
    ) -> None:
        value = handler.settings[key]
        if isinstance(value, str):
            referenced = _exact_placeholder(value)
            if referenced is not None:
                actual = field_types.get(referenced)
                if actual is None:
                    raise ValueError(
                        f"handler {handler.key!r}: {key} references undeclared "
                        f"config field {referenced!r}"
                    )
                if actual != expected:
                    raise ValueError(
                        f"handler {handler.key!r}: {key} needs a {expected} config "
                        f"field but {referenced!r} is {actual}"
                    )
                return
            if expected != "string":
                raise ValueError(
                    f"handler {handler.key!r}: {key} must be an int (a literal or a "
                    "matching config placeholder)"
                )
            return
        # A bare literal value: bool would satisfy int via subclassing, so reject it.
        if expected == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"handler {handler.key!r}: {key} must be an int")
        else:
            raise ValueError(
                f"handler {handler.key!r}: {key} must be a string (HH:MM or ISO)"
            )
