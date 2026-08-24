"""Shared fixtures for the backend test suite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PLAN_JSON = {
    "task_kind": "tabular-classification",
    "primary_metric": "accuracy",
    "revision": 1,
    "steps": [
        {
            "id": "s1",
            "index": 0,
            "title": "Retrieve sklearn pipeline APIs",
            "description": "Find exact signatures for Pipeline and GridSearchCV.",
            "kind": "research",
            "depends_on": [],
            "acceptance": ["API signatures captured verbatim"],
        },
        {
            "id": "s2",
            "index": 1,
            "title": "Train a classifier on breast_cancer",
            "description": "Stratified 80/20 split, seed 42, LogisticRegression pipeline.",
            "kind": "train",
            "depends_on": ["s1"],
            "dataset": {
                "dataset_id": "sklearn.breast_cancer",
                "path": "/datasets/tabular/breast_cancer.parquet",
                "sha256": "a" * 64,
                "task_kind": "tabular-classification",
                "n_samples": 569,
                "target_column": "target",
            },
            "acceptance": ["metrics.json contains accuracy and f1_macro"],
        },
        {
            "id": "s3",
            "index": 2,
            "title": "Report results",
            "description": "Summarise metrics against criteria.",
            "kind": "report",
            "depends_on": ["s2"],
        },
    ],
    "success_criteria": [
        {
            "id": "c1",
            "metric": "accuracy",
            "comparator": "gte",
            "threshold": 0.95,
            "required": True,
            "weight": 2.0,
            "rationale": "User-specified target.",
        },
        {
            "id": "c2",
            "metric": "f1_macro",
            "comparator": "gte",
            "threshold": 0.94,
            "required": True,
            "weight": 1.5,
            "rationale": "Guards against accuracy inflated by class imbalance.",
        },
    ],
    "assumptions": ["80/20 stratified split with seed 42."],
}

VALID_PROGRAM = """\
import json
import os
import random

import numpy as np


def main():
    random.seed(int(os.environ["PLUTON_SEED"]))
    np.random.seed(int(os.environ["PLUTON_SEED"]))
    os.makedirs("/artifacts", exist_ok=True)
    print("training complete", flush=True)
    with open("/artifacts/metrics.json", "w") as handle:
        json.dump({"metrics": {"accuracy": 0.97}}, handle)


if __name__ == "__main__":
    main()
"""


@pytest.fixture
def plan_reply() -> str:
    """A planner response: a fenced JSON plan, the way a local model returns one."""
    return f"Here is the plan:\n```json\n{json.dumps(PLAN_JSON)}\n```"


@pytest.fixture
def coder_reply() -> str:
    """A coder response: a fenced program plus its JSON sidecar."""
    sidecar = {
        "rationale": "Baseline logistic regression.",
        "requirements": [],
        "addresses_error": None,
    }
    return f"```python\n{VALID_PROGRAM}```\n```json\n{json.dumps(sidecar)}\n```"


DIAGNOSIS_JSON = {
    "error_fingerprint": "KeyError:target",
    "root_cause": (
        "The parquet file names the label column `diagnosis`; the program reads `target`."
    ),
    "evidence": ["KeyError: 'target'", "y = df['target']"],
    "fix_strategy": "Read the label from the column the dataset actually has.",
    "targeted_changes": [
        "Replace `df['target']` on line 12 with `df['diagnosis']`.",
        "Leave the split and the estimator unchanged.",
    ],
    "prior_art": [],
    "confidence": 0.86,
    "requires_replan": False,
    "requires_research": False,
}

REPORT_MARKDOWN = """\
## 1. Objective
Train a classifier on the breast cancer dataset and reach at least 95% held-out accuracy.

## 2. Result
The run met both required criteria on the held-out split.

## 3. Approach
A stratified 80/20 split with a logistic regression pipeline, seeded for reproducibility.

## 4. What went wrong and how it was fixed
The first attempt read a column the file does not have; the second read the right one.

## 7. Limitations and next steps
Only one split was measured, so the interval around these numbers is unknown.
"""


def diagnosis_reply(**overrides: object) -> str:
    """A debugger response: the Diagnosis JSON a local model returns, fenced."""
    payload = {**DIAGNOSIS_JSON, **overrides}
    return f"```json\n{json.dumps(payload)}\n```"


@pytest.fixture
def debugger_reply() -> str:
    return diagnosis_reply()


@pytest.fixture
def reporter_reply() -> str:
    """A reporter response: the five narrative sections, as markdown prose."""
    return REPORT_MARKDOWN


@pytest.fixture
def runs_root(tmp_path: Path) -> Path:
    root = tmp_path / "runs"
    root.mkdir()
    return root
