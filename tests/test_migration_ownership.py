"""Guard tests for alembic migration-tree table ownership.

The project has one alembic tree (``alembic/main``) plus Skrift core's own
migrations from the installed ``skrift`` package (the ``alembic/legacy`` tree
was deleted in the phase-05 decommission). Partition rules:

- ``alembic/main`` owns every model table in ``smarter_dev/web/models.py``.
- Skrift core owns its own table names (e.g. ``api_keys``); the main tree may
  never claim one.

These tests parse the ``MAIN_TABLES`` literal out of env.py with ``ast``
because importing an alembic env module executes migrations.
"""

from __future__ import annotations

import ast
from pathlib import Path

import skrift.db.models  # noqa: F401  -- registers all Skrift models with SkriftBase.metadata
from skrift.db.base import Base as SkriftBase

import smarter_dev.web.models  # noqa: F401  -- registers all models with Base.metadata
from smarter_dev.shared.database import Base

REPO_ROOT = Path(__file__).resolve().parent.parent
MAIN_ENV_PATH = REPO_ROOT / "alembic" / "main" / "env.py"


def load_ownership_frozenset(env_path: Path, variable_name: str) -> frozenset[str]:
    """Extract a module-level ``NAME: frozenset[str] = frozenset({...})`` literal."""
    module_ast = ast.parse(env_path.read_text())
    for node in module_ast.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            assigned_names = [node.target.id]
            value = node.value
        elif isinstance(node, ast.Assign):
            assigned_names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            value = node.value
        else:
            continue
        if variable_name not in assigned_names:
            continue
        is_frozenset_call = (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
        )
        if not is_frozenset_call:
            raise AssertionError(f"{variable_name} in {env_path} is not a frozenset(...) literal")
        if not value.args:
            return frozenset()
        return frozenset(ast.literal_eval(value.args[0]))
    raise AssertionError(f"{variable_name} not found in {env_path}")


def skrift_owned_table_names() -> frozenset[str]:
    """Table names owned by Skrift core migrations (the installed package)."""
    return frozenset(SkriftBase.metadata.tables)


def project_model_table_names() -> frozenset[str]:
    """Table names of models defined in the smarter_dev package.

    Derived from mapped classes rather than ``Base.metadata`` directly because
    test modules (e.g. tests/web/test_models/test_base_model.py) register
    throwaway models on the shared metadata during full-suite runs.
    """
    return frozenset(
        mapper.persist_selectable.name
        for mapper in Base.registry.mappers
        if mapper.class_.__module__.startswith("smarter_dev.")
    )


def test_every_model_table_is_owned() -> None:
    """Every table in the shared Base.metadata is covered by an owning tree.

    Fails when a new model is added to smarter_dev/web/models.py without
    adding it to MAIN_TABLES — the frozenset can never silently go stale.
    """
    main_tables = load_ownership_frozenset(MAIN_ENV_PATH, "MAIN_TABLES")
    unowned = project_model_table_names() - main_tables - skrift_owned_table_names()
    assert unowned == frozenset(), (
        f"model tables owned by no alembic tree: {sorted(unowned)}; "
        "add them to MAIN_TABLES in alembic/main/env.py"
    )


def test_main_tables_has_no_phantom_entries() -> None:
    """Every MAIN_TABLES entry corresponds to a real model table."""
    main_tables = load_ownership_frozenset(MAIN_ENV_PATH, "MAIN_TABLES")
    phantom_entries = main_tables - project_model_table_names()
    assert phantom_entries == frozenset(), (
        f"MAIN_TABLES entries with no model: {sorted(phantom_entries)}"
    )


def test_main_tables_does_not_claim_skrift_owned_names() -> None:
    """The main tree must never own a table name Skrift core migrations own.

    In particular ``api_keys`` must stay out of MAIN_TABLES: ``skrift.api_keys``
    is Skrift core's own table (the legacy model of the same name is deleted).
    """
    main_tables = load_ownership_frozenset(MAIN_ENV_PATH, "MAIN_TABLES")
    doubly_owned = main_tables & skrift_owned_table_names()
    assert doubly_owned == frozenset(), (
        f"tables owned by both alembic/main and Skrift core: {sorted(doubly_owned)}"
    )
