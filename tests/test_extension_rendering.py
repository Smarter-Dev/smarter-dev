"""Unit tests for extension config validation + script/settings rendering.

Pure functions only — no DB, no session. Covers the typed-substitution rules,
the snowflake injection guard, and every render-time rail render_bundle re-runs
(placeholder resolution, leftover markers, lint, role-allowlist closure, and
schedule floors).
"""

from __future__ import annotations

import pytest

from smarter_dev.extensions.rendering import (
    RenderError,
    extract_granted_role_literals,
    render_bundle,
    render_script,
    render_settings,
    validate_config_values,
)
from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)


def _manifest(config, handlers, example_config=None):
    return ExtensionManifest(
        slug="test-ext",
        title="T",
        summary="S",
        version=1,
        config=config,
        handlers=handlers,
        example_config=example_config or {},
    )


def _message_handler(*, key="h1", name="test-h1", settings=None, channel_scope=None):
    return HandlerTemplate(
        key=key,
        name=name,
        trigger_type="message",
        description="d",
        script_file=f"{key}.monty",
        settings=settings or {},
        channel_scope=channel_scope or [],
    )


# -- validate_config_values ----------------------------------------------------


def test_snowflake_ids_accepted_and_returned():
    manifest = _manifest(
        [ConfigField(name="chan", type="channel_id", label="Chan")],
        [_message_handler()],
    )
    cleaned = validate_config_values(manifest, {"chan": "123456789012345678"})
    assert cleaned == {"chan": "123456789012345678"}


@pytest.mark.parametrize(
    "bad",
    ['123"; evil()', "not-digits", "12345", "1234567890123456789012345"],
)
def test_snowflake_regex_rejects_injection_and_bad_length(bad):
    manifest = _manifest(
        [ConfigField(name="chan", type="channel_id", label="Chan")],
        [_message_handler()],
    )
    with pytest.raises(RenderError):
        validate_config_values(manifest, {"chan": bad})


def test_missing_required_field_raises():
    manifest = _manifest(
        [ConfigField(name="chan", type="channel_id", label="Chan")],
        [_message_handler()],
    )
    with pytest.raises(RenderError, match="missing required"):
        validate_config_values(manifest, {})


def test_missing_optional_uses_default():
    manifest = _manifest(
        [
            ConfigField(
                name="footer",
                type="string",
                required=False,
                default="hi",
                label="F",
            )
        ],
        [_message_handler()],
    )
    assert validate_config_values(manifest, {}) == {"footer": "hi"}


def test_missing_optional_without_default_is_omitted():
    manifest = _manifest(
        [ConfigField(name="footer", type="string", required=False, label="F")],
        [_message_handler()],
    )
    assert validate_config_values(manifest, {}) == {}


def test_unknown_key_raises():
    manifest = _manifest(
        [ConfigField(name="chan", type="channel_id", label="Chan")],
        [_message_handler()],
    )
    with pytest.raises(RenderError, match="unknown config field"):
        validate_config_values(
            manifest, {"chan": "123456789012345678", "surprise": "x"}
        )


def test_int_field_coerces_numeric_string_and_rejects_bool():
    manifest = _manifest(
        [ConfigField(name="n", type="int", label="N")], [_message_handler()]
    )
    assert validate_config_values(manifest, {"n": "42"}) == {"n": 42}
    assert validate_config_values(manifest, {"n": 7}) == {"n": 7}
    with pytest.raises(RenderError):
        validate_config_values(manifest, {"n": True})
    with pytest.raises(RenderError):
        validate_config_values(manifest, {"n": "notanumber"})


def test_string_field_rejects_overlong_and_nul():
    manifest = _manifest(
        [ConfigField(name="s", type="string", label="S")], [_message_handler()]
    )
    with pytest.raises(RenderError):
        validate_config_values(manifest, {"s": "x" * 501})
    with pytest.raises(RenderError):
        validate_config_values(manifest, {"s": "a\x00b"})


# -- render_script typed substitution ------------------------------------------


def _all_types_manifest():
    return _manifest(
        [
            ConfigField(name="chan", type="channel_id", label="C"),
            ConfigField(name="role", type="role_id", label="R"),
            ConfigField(name="note", type="string", label="N"),
            ConfigField(name="count", type="int", label="I"),
            ConfigField(name="flag", type="bool", label="B"),
        ],
        [_message_handler()],
    )


def test_render_script_emits_typed_literals():
    manifest = _all_types_manifest()
    config = {
        "chan": "123456789012345678",
        "role": "234567890123456789",
        "note": 'he said "hi"\nline2',
        "count": 5,
        "flag": True,
    }
    template = (
        "C = {{cfg.chan}}\n"
        "R = {{cfg.role}}\n"
        "N = {{cfg.note}}\n"
        "I = {{cfg.count}}\n"
        "F = {{cfg.flag}}\n"
    )
    out = render_script(template, manifest, config)
    assert 'C = "123456789012345678"' in out
    assert 'R = "234567890123456789"' in out
    # json.dumps escapes the embedded quotes and newline.
    assert 'N = "he said \\"hi\\"\\nline2"' in out
    assert "I = 5" in out
    assert "F = True" in out


def test_render_script_bool_false_and_undeclared_field():
    manifest = _all_types_manifest()
    out = render_script("F = {{cfg.flag}}", manifest, {"flag": False})
    assert out == "F = False"
    with pytest.raises(RenderError, match="undeclared"):
        render_script("X = {{cfg.nope}}", manifest, {})


# -- render_settings recursion -------------------------------------------------


def test_render_settings_exact_placeholder_becomes_typed_value():
    manifest = _manifest(
        [
            ConfigField(name="every", type="int", label="E"),
            ConfigField(name="role", type="role_id", label="R"),
        ],
        [_message_handler()],
    )
    config = {"every": 90, "role": "234567890123456789"}
    template = {
        "interval_seconds": "{{cfg.every}}",
        "allowed_role_ids": ["{{cfg.role}}"],
        "nested": {"n": "{{cfg.every}}"},
    }
    out = render_settings(template, manifest, config)
    assert out["interval_seconds"] == 90 and isinstance(out["interval_seconds"], int)
    assert out["allowed_role_ids"] == ["234567890123456789"]
    assert out["nested"]["n"] == 90


def test_render_settings_embedded_placeholder_splices_str():
    manifest = _manifest(
        [ConfigField(name="role", type="role_id", label="R")], [_message_handler()]
    )
    out = render_settings(
        {"label": "role is {{cfg.role}}!"},
        manifest,
        {"role": "234567890123456789"},
    )
    assert out["label"] == "role is 234567890123456789!"


# -- extract_granted_role_literals ---------------------------------------------


def test_extract_granted_role_literals():
    script = (
        'await add_role(context["author_id"], "111111111111111111")\n'
        'await remove_role(context["author_id"], "222222222222222222")\n'
    )
    assert extract_granted_role_literals(script) == {
        "111111111111111111",
        "222222222222222222",
    }


# -- render_bundle rails -------------------------------------------------------


def test_render_bundle_happy_path():
    manifest = _manifest(
        [ConfigField(name="chan", type="channel_id", label="C")],
        [_message_handler(channel_scope=["chan"])],
    )
    scripts = {"h1": "TARGET = {{cfg.chan}}\nsend_message('hi', TARGET)\n"}
    bundle = render_bundle(manifest, {"chan": "123456789012345678"}, scripts)
    assert len(bundle) == 1
    item = bundle[0]
    assert item.channel_ids == ["123456789012345678"]
    assert 'TARGET = "123456789012345678"' in item.script


def test_render_bundle_leftover_marker_after_render():
    manifest = _manifest(
        [ConfigField(name="chan", type="channel_id", label="C")],
        [_message_handler()],
    )
    # `{{cfg.chan}` (single closing brace) is skipped by the placeholder regex,
    # so the marker survives and the sweep must catch it.
    scripts = {"h1": 'X = "{{cfg.chan}"\n'}
    with pytest.raises(RenderError, match="malformed placeholder"):
        render_bundle(manifest, {"chan": "123456789012345678"}, scripts)


def test_render_bundle_lint_failure():
    manifest = _manifest(
        [ConfigField(name="chan", type="channel_id", label="C")],
        [_message_handler()],
    )
    scripts = {"h1": 'result = eval("2")\n'}
    with pytest.raises(RenderError, match="lint"):
        render_bundle(manifest, {"chan": "123456789012345678"}, scripts)


def test_render_bundle_role_grant_not_in_allowlist():
    manifest = _manifest(
        [ConfigField(name="role", type="role_id", label="R")],
        [_message_handler(settings={"allowed_role_ids": []})],
    )
    scripts = {"h1": 'await add_role(context["author_id"], "333333333333333333")\n'}
    with pytest.raises(RenderError, match="allowed_role_ids"):
        render_bundle(manifest, {"role": "333333333333333333"}, scripts)


def test_render_bundle_role_grant_in_allowlist_passes():
    manifest = _manifest(
        [ConfigField(name="role", type="role_id", label="R")],
        [
            _message_handler(
                settings={"allowed_role_ids": ["{{cfg.role}}"]}
            )
        ],
    )
    scripts = {"h1": 'await add_role(context["author_id"], "333333333333333333")\n'}
    bundle = render_bundle(manifest, {"role": "333333333333333333"}, scripts)
    assert bundle[0].settings["allowed_role_ids"] == ["333333333333333333"]


def test_render_bundle_schedule_below_floor():
    manifest = _manifest(
        [ConfigField(name="every", type="int", label="E")],
        [
            HandlerTemplate(
                key="sch",
                name="test-sch",
                trigger_type="schedule",
                description="d",
                script_file="sch.monty",
                settings={"interval_seconds": "{{cfg.every}}"},
            )
        ],
    )
    scripts = {"sch": "pass\n"}
    with pytest.raises(RenderError, match="schedule invalid"):
        render_bundle(manifest, {"every": 5}, scripts)


def test_render_bundle_schedule_at_floor_passes():
    manifest = _manifest(
        [ConfigField(name="every", type="int", label="E")],
        [
            HandlerTemplate(
                key="sch",
                name="test-sch",
                trigger_type="schedule",
                description="d",
                script_file="sch.monty",
                settings={"interval_seconds": "{{cfg.every}}"},
            )
        ],
    )
    scripts = {"sch": "pass\n"}
    bundle = render_bundle(manifest, {"every": 60}, scripts)
    assert bundle[0].settings == {"interval_seconds": 60}
