"""D-006 — LangGraph's checkpoint tables must be invisible to autogeneration.

Without the filter, the next `alembic revision --autogenerate` emits DROP TABLE for all
four and destroys every resumable run.
"""

import importlib.util
from pathlib import Path

import pytest

ENV_PY = Path(__file__).resolve().parents[1] / "alembic" / "env.py"


@pytest.fixture(scope="module")
def include_object():
    """Load `include_object` out of alembic/env.py without running its migrations."""
    source = ENV_PY.read_text()
    # env.py both needs a live Alembic context and runs migrations at import time, so
    # slice out just the filter definition and execute that.
    start = source.index("LANGGRAPH_TABLES")
    end = source.index("def run_migrations_offline")
    namespace: dict = {}
    exec(compile(source[start:end], str(ENV_PY), "exec"), namespace)  # noqa: S102
    return namespace["include_object"]


@pytest.mark.parametrize(
    "table",
    ["checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"],
)
def test_langgraph_tables_are_excluded(include_object, table):
    assert include_object(None, table, "table", True, None) is False


@pytest.mark.parametrize("table", ["tasks", "agent_logs", "artifacts"])
def test_application_tables_are_included(include_object, table):
    assert include_object(None, table, "table", True, None) is True


def test_non_table_objects_are_included(include_object):
    assert include_object(None, "checkpoints", "column", True, None) is True


def test_env_module_imports_cleanly():
    assert importlib.util.find_spec("alembic") is not None
