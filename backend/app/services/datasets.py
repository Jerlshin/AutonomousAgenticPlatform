"""The read-only dataset registry (ARCHITECTURE.md §10.8).

With `--network none` the sandbox cannot fetch anything, so every dataset a plan can use
must already exist on the `pluton_datasets` volume and be listed in `manifest.json`. The
Planner reads this manifest and **must** bind each `train` step to a concrete entry — the
single most effective guard against the classic "agent writes `pd.read_csv('data.csv')`
for a file that does not exist" failure.

The registry is seeded by `make seed-datasets`. Until it has been, `load_manifest` returns
an empty registry rather than raising: a missing registry is a setup state, not a crash.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.engine.state import DatasetBinding

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"


def datasets_root() -> Path:
    return Path(settings.DATASETS_ROOT)


def load_manifest(root: Path | None = None) -> dict[str, Any]:
    """The manifest document, or an empty registry when it has not been seeded."""
    path = (root or datasets_root()) / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning(
            "No dataset manifest at %s — run `make seed-datasets`. Plans will be built "
            "without dataset bindings.",
            path,
        )
        return {"schema_version": "1.0", "datasets": []}
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Dataset manifest at %s is unreadable: %s", path, exc)
        return {"schema_version": "1.0", "datasets": []}

    if not isinstance(payload, dict) or not isinstance(payload.get("datasets"), list):
        logger.error("Dataset manifest at %s is not in the expected shape", path)
        return {"schema_version": "1.0", "datasets": []}
    return payload


def list_datasets(
    task_kind: str | None = None, root: Path | None = None
) -> list[dict[str, Any]]:
    """Manifest entries, optionally narrowed to one task kind."""
    entries = load_manifest(root).get("datasets", [])
    if task_kind is None:
        return list(entries)
    return [entry for entry in entries if entry.get("task_kind") == task_kind]


def find_dataset(dataset_id: str, root: Path | None = None) -> dict[str, Any] | None:
    return next(
        (
            entry
            for entry in load_manifest(root).get("datasets", [])
            if entry.get("id") == dataset_id
        ),
        None,
    )


def binding_from_entry(entry: dict[str, Any]) -> DatasetBinding:
    """Turn a manifest entry into the binding a plan step carries."""
    return DatasetBinding(
        dataset_id=entry["id"],
        path=entry["path"],
        sha256=entry.get("sha256", ""),
        task_kind=entry.get("task_kind", ""),
        n_samples=entry.get("n_samples"),
        target_column=entry.get("target"),
    )


def manifest_for_prompt(root: Path | None = None) -> str:
    """The manifest rendered for a prompt, trimmed to the fields a Planner needs.

    Sending the whole document wastes context on licences and prose descriptions the model
    does not plan with, and the Planner's truncation priority puts the manifest near the
    top of what gets dropped under pressure — so it should be small to begin with.
    """
    entries = load_manifest(root).get("datasets", [])
    if not entries:
        return (
            "NO DATASETS ARE REGISTERED. The dataset registry has not been seeded, so no "
            "`train` step can be bound to real data. Plan for what can be computed without "
            "a dataset, and record the missing registry in `assumptions`."
        )

    trimmed = [
        {
            key: entry[key]
            for key in (
                "id",
                "task_kind",
                "path",
                "sha256",
                "n_samples",
                "n_features",
                "n_classes",
                "target",
                "description",
            )
            if key in entry
        }
        for entry in entries
    ]
    return json.dumps(trimmed, indent=2)
