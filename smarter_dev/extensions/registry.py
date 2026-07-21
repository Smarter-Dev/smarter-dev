"""Catalog discovery + a cached, self-validating registry.

The catalog is exactly what lives under
``smarter_dev/extensions/catalog/<slug>/``: each subpackage exports a
module-level ``MANIFEST`` and ships its ``*.monty`` script templates as sibling
files. :func:`load_registry` imports every subpackage, loads its scripts, and
proves each manifest against its own ``example_config`` by running the full
render pipeline — so a catalog change that breaks a template fails at load
(and, via the admin controller's import-time :func:`get_registry` call, at app
startup) rather than at install time.

No entry points, no dynamic paths: the registry can only see repo code.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from pathlib import Path

from smarter_dev.extensions import catalog
from smarter_dev.extensions.rendering import RenderError, render_bundle
from smarter_dev.extensions.schema import ExtensionManifest


class ExtensionRegistryError(Exception):
    """A malformed or unloadable catalog manifest — raised at load, never swallowed."""


@dataclass(frozen=True)
class LoadedExtension:
    """A manifest plus its handlers' raw script templates (key -> template text)."""

    manifest: ExtensionManifest
    scripts: dict[str, str]


class ExtensionRegistry:
    """An immutable view of the loaded catalog, keyed by slug."""

    def __init__(self, extensions: dict[str, LoadedExtension]) -> None:
        self._extensions = extensions

    def all(self) -> list[LoadedExtension]:
        """Every loaded extension, sorted by slug (the catalog page order)."""
        return [self._extensions[slug] for slug in sorted(self._extensions)]

    def get(self, slug: str) -> LoadedExtension:
        try:
            return self._extensions[slug]
        except KeyError as exc:
            raise ExtensionRegistryError(f"unknown extension {slug!r}") from exc


def _load_one(module_name: str) -> LoadedExtension:
    module = importlib.import_module(module_name)
    manifest = getattr(module, "MANIFEST", None)
    if not isinstance(manifest, ExtensionManifest):
        raise ExtensionRegistryError(
            f"catalog module {module_name!r} must define a MANIFEST of type "
            "ExtensionManifest"
        )
    package_dir = Path(module.__file__).parent
    scripts: dict[str, str] = {}
    for handler in manifest.handlers:
        script_path = package_dir / handler.script_file
        try:
            scripts[handler.key] = script_path.read_text()
        except OSError as exc:
            raise ExtensionRegistryError(
                f"extension {manifest.slug!r}: cannot read script file "
                f"{handler.script_file!r} for handler {handler.key!r}: {exc}"
            ) from exc
    return LoadedExtension(manifest=manifest, scripts=scripts)


def load_registry() -> ExtensionRegistry:
    """Discover, load, and fully validate every catalog extension."""
    extensions: dict[str, LoadedExtension] = {}
    for module_info in pkgutil.iter_modules(catalog.__path__):
        if not module_info.ispkg:
            continue
        module_name = f"{catalog.__name__}.{module_info.name}"
        loaded = _load_one(module_name)
        slug = loaded.manifest.slug
        if slug in extensions:
            raise ExtensionRegistryError(
                f"duplicate extension slug {slug!r} (module {module_name!r})"
            )
        # Prove the shipped example renders + lints clean (the CI/startup gate).
        try:
            render_bundle(loaded.manifest, loaded.manifest.example_config, loaded.scripts)
        except RenderError as exc:
            raise ExtensionRegistryError(
                f"extension {slug!r} example_config does not render cleanly: {exc}"
            ) from exc
        extensions[slug] = loaded
    return ExtensionRegistry(extensions)


_REGISTRY: ExtensionRegistry | None = None


def get_registry() -> ExtensionRegistry:
    """Return the cached registry singleton, loading it on first use."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = load_registry()
    return _REGISTRY
