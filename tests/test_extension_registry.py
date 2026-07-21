"""Tests for the extension registry loader and the manifest self-validation.

Two layers: (1) the shipped catalog must load and every example_config must
render + lint clean (the CI/startup fail-fast gate); (2) malformed manifests
and malformed catalog packages must fail loudly with the offending name in the
message. Malformed catalogs are fed to load_registry via synthetic modules so
nothing bad is written to the real catalog directory.
"""

from __future__ import annotations

import types

import pytest

from smarter_dev.extensions import registry as registry_module
from smarter_dev.extensions.registry import (
    ExtensionRegistryError,
    get_registry,
    load_registry,
)
from smarter_dev.extensions.rendering import render_bundle
from smarter_dev.extensions.schema import (
    ConfigField,
    ExtensionManifest,
    HandlerTemplate,
)


# -- shipped catalog (the real fail-fast gate) ---------------------------------


def test_load_registry_loads_shipped_catalog():
    reg = load_registry()
    slugs = [ext.manifest.slug for ext in reg.all()]
    assert "dm-forum-relay" in slugs


def test_get_registry_is_cached_singleton():
    assert get_registry() is get_registry()


def test_all_shipped_examples_render_and_lint_clean():
    reg = load_registry()
    for ext in reg.all():
        bundle = render_bundle(
            ext.manifest, ext.manifest.example_config, ext.scripts
        )
        assert len(bundle) == len(ext.manifest.handlers)


def test_registry_get_unknown_slug_raises():
    reg = load_registry()
    with pytest.raises(ExtensionRegistryError, match="unknown extension"):
        reg.get("does-not-exist")


# -- synthetic-catalog helpers -------------------------------------------------


def _valid_manifest(**over):
    kwargs = dict(
        slug="test-ext",
        title="T",
        summary="S",
        version=1,
        config=[ConfigField(name="chan", type="channel_id", label="Chan")],
        handlers=[
            HandlerTemplate(
                key="h1",
                name="test-h1",
                trigger_type="message",
                description="d",
                script_file="h1.monty",
                channel_scope=["chan"],
            )
        ],
        example_config={"chan": "123456789012345678"},
    )
    kwargs.update(over)
    return ExtensionManifest(**kwargs)


def _install_fake_catalog(monkeypatch, tmp_path, modules: dict[str, object]):
    """Point load_registry at ``modules`` (name -> module) under ``tmp_path``."""
    infos = [
        types.SimpleNamespace(name=name, ispkg=True) for name in modules
    ]

    def _iter(_path):
        return iter(infos)

    def _import(full_name):
        return modules[full_name.rsplit(".", 1)[1]]

    monkeypatch.setattr(registry_module.pkgutil, "iter_modules", _iter)
    monkeypatch.setattr(registry_module.importlib, "import_module", _import)


def _fake_module(tmp_path, name, manifest, *, scripts=None, write_scripts=True):
    package_dir = tmp_path / name
    package_dir.mkdir(exist_ok=True)
    module = types.ModuleType(f"fake_{name}")
    if manifest is not None:
        module.MANIFEST = manifest
    module.__file__ = str(package_dir / "__init__.py")
    if manifest is not None and write_scripts:
        for handler in manifest.handlers:
            text = (scripts or {}).get(handler.key, "send_message('hi')\n")
            (package_dir / handler.script_file).write_text(text)
    return module


# -- catalog-level registry errors ---------------------------------------------


def test_missing_manifest_attribute_raises(monkeypatch, tmp_path):
    module = _fake_module(tmp_path, "broken", None)
    _install_fake_catalog(monkeypatch, tmp_path, {"broken": module})
    with pytest.raises(ExtensionRegistryError, match="broken"):
        load_registry()


def test_manifest_wrong_type_raises(monkeypatch, tmp_path):
    module = _fake_module(tmp_path, "wrong", None)
    module.MANIFEST = {"not": "a manifest"}
    _install_fake_catalog(monkeypatch, tmp_path, {"wrong": module})
    with pytest.raises(ExtensionRegistryError, match="MANIFEST"):
        load_registry()


def test_duplicate_slug_raises(monkeypatch, tmp_path):
    a = _fake_module(tmp_path, "a", _valid_manifest(slug="dup"))
    b = _fake_module(tmp_path, "b", _valid_manifest(slug="dup"))
    _install_fake_catalog(monkeypatch, tmp_path, {"a": a, "b": b})
    with pytest.raises(ExtensionRegistryError, match="duplicate extension slug"):
        load_registry()


def test_missing_script_file_raises(monkeypatch, tmp_path):
    module = _fake_module(
        tmp_path, "noscript", _valid_manifest(), write_scripts=False
    )
    _install_fake_catalog(monkeypatch, tmp_path, {"noscript": module})
    with pytest.raises(ExtensionRegistryError, match="script file"):
        load_registry()


def test_example_config_that_does_not_render_raises(monkeypatch, tmp_path):
    # example_config supplies an id that fails the snowflake guard, so the
    # startup render of the example fails.
    manifest = _valid_manifest(example_config={"chan": "not-a-snowflake"})
    module = _fake_module(tmp_path, "badexample", manifest)
    _install_fake_catalog(monkeypatch, tmp_path, {"badexample": module})
    with pytest.raises(ExtensionRegistryError, match="does not render cleanly"):
        load_registry()


# -- manifest self-validation --------------------------------------------------


def test_bad_slug_rejected():
    with pytest.raises(ValueError, match="slug"):
        _valid_manifest(slug="Not A Slug")


def test_duplicate_config_field_names_rejected():
    with pytest.raises(ValueError, match="duplicate config field names"):
        _valid_manifest(
            config=[
                ConfigField(name="chan", type="channel_id", label="A"),
                ConfigField(name="chan", type="string", label="B"),
            ]
        )


def test_duplicate_handler_keys_rejected():
    with pytest.raises(ValueError, match="duplicate handler keys"):
        _valid_manifest(
            handlers=[
                HandlerTemplate(
                    key="h1",
                    name="n1",
                    trigger_type="message",
                    description="d",
                    script_file="a.monty",
                ),
                HandlerTemplate(
                    key="h1",
                    name="n2",
                    trigger_type="message",
                    description="d",
                    script_file="b.monty",
                ),
            ]
        )


def test_duplicate_handler_names_rejected():
    with pytest.raises(ValueError, match="duplicate handler names"):
        _valid_manifest(
            handlers=[
                HandlerTemplate(
                    key="h1",
                    name="same",
                    trigger_type="message",
                    description="d",
                    script_file="a.monty",
                ),
                HandlerTemplate(
                    key="h2",
                    name="same",
                    trigger_type="message",
                    description="d",
                    script_file="b.monty",
                ),
            ]
        )


def test_unknown_trigger_type_rejected():
    with pytest.raises(ValueError, match="trigger_type"):
        HandlerTemplate(
            key="h1",
            name="n1",
            trigger_type="not_a_trigger",
            description="d",
            script_file="a.monty",
        )


def test_include_bot_messages_on_non_message_rejected():
    with pytest.raises(ValueError, match="include_bot_messages"):
        _valid_manifest(
            handlers=[
                HandlerTemplate(
                    key="dm",
                    name="dm1",
                    trigger_type="dm_message",
                    description="d",
                    script_file="a.monty",
                    settings={"include_bot_messages": True},
                )
            ]
        )


def test_channel_scope_naming_non_channel_field_rejected():
    with pytest.raises(ValueError, match="channel_scope"):
        _valid_manifest(
            config=[ConfigField(name="note", type="string", label="N")],
            handlers=[
                HandlerTemplate(
                    key="h1",
                    name="n1",
                    trigger_type="message",
                    description="d",
                    script_file="a.monty",
                    channel_scope=["note"],
                )
            ],
            example_config={"note": "hello"},
        )


def test_schedule_handler_without_timing_key_rejected():
    with pytest.raises(ValueError, match="exactly one"):
        _valid_manifest(
            handlers=[
                HandlerTemplate(
                    key="sch",
                    name="sch1",
                    trigger_type="schedule",
                    description="d",
                    script_file="a.monty",
                    settings={},
                )
            ]
        )
