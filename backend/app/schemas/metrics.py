"""Validation of the `metrics.json` contract (MLOPS.md §3).

`metrics.json` is the interface between agent-generated code and everything downstream —
criteria evaluation, MLflow logging, the report. It is the most important schema in the
platform, so it has exactly one definition: `metrics_contract.json`, a Draft 2020-12 JSON
Schema checked in beside this module. There is deliberately no parallel Pydantic mirror of
it; two definitions of one contract drift, and the drift is silent.

Validation happens in two layers:

* **Schema** — shape, types, enums, path patterns. Structural.
* **Semantics** (§3.4) — four checks the schema cannot express, each phrased as a message
  the Debugger can act on directly. The NaN check is the important one: a diverged model
  that exits 0 reporting `accuracy: nan` compares False against every threshold and would
  otherwise read as an ordinary quality miss rather than the numerical blow-up it is.
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).with_name("metrics_contract.json")

# Bumped alongside `schema_version` in the contract itself.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})


@lru_cache(maxsize=1)
def load_contract() -> dict[str, Any]:
    """The JSON Schema document. Cached — it is read once per process."""
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def validate_schema(payload: Any) -> list[str]:
    """Schema errors as human-readable strings, most structural first.

    `jsonschema` is an optional import here purely so the engine remains importable in a
    stripped environment; a missing library degrades to "no schema errors found" rather
    than crashing a run that otherwise succeeded, and the semantic checks below still run.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - exercised only in stripped environments
        return []

    validator = jsonschema.Draft202012Validator(load_contract())
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    return [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in errors
    ]


def observed_metrics(payload: dict[str, Any] | None) -> dict[str, float]:
    """The scalar metric mapping inside a metrics.json document.

    Callers hold the whole document — it carries the dataset, params and runtime blocks
    too — but criteria are evaluated against `metrics` alone. Going through this helper is
    what stops a caller from checking a threshold against the top level and silently
    finding every metric absent.
    """
    return dict((payload or {}).get("metrics") or {})


def check_finite(payload: dict[str, Any]) -> list[str]:
    """Every metric value must be finite (MLOPS.md §3.4)."""
    problems: list[str] = []
    for name, value in (payload.get("metrics") or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(float(value)):
            problems.append(
                f"metric '{name}' is {value}. NaN or Inf indicates a numerical failure — "
                "the metric was not actually computed."
            )
    return problems


def check_declared_files_exist(
    payload: dict[str, Any], artifacts_dir: Path
) -> list[str]:
    """Every declared artifact and plot must exist under /artifacts (MLOPS.md §3.4)."""
    declared = [a.get("path") for a in payload.get("artifacts") or [] if a.get("path")]
    declared += list(payload.get("plots") or [])
    return [
        f"metrics.json declares artifact '{rel}' but it was not written."
        for rel in declared
        if not (artifacts_dir / rel).exists()
    ]


def check_required_metrics(payload: dict[str, Any], criteria: list[Any]) -> list[str]:
    """Every metric a success criterion evaluates must be present (MLOPS.md §3.4)."""
    produced = set((payload.get("metrics") or {}).keys())
    return [
        f"metrics.json is missing required metric '{c.metric}', which success criterion "
        f"{c.id} evaluates."
        for c in criteria
        if c.metric not in produced
    ]


def check_dataset_binding(payload: dict[str, Any], binding: Any | None) -> list[str]:
    """The reported dataset must be the one the plan bound (MLOPS.md §3.4).

    Catches the failure where the script loads a plausible-looking wrong file: the metrics
    are real, they just describe different data.
    """
    if binding is None:
        return []
    reported = payload.get("dataset") or {}
    problems: list[str] = []
    if reported.get("id") != binding.dataset_id:
        problems.append(
            f"metrics.json reports dataset '{reported.get('id')}' but the plan bound "
            f"'{binding.dataset_id}'. The wrong data was loaded."
        )
    # A placeholder sha in the manifest (seeding writes the real one) must not fail a run
    # that is otherwise correct, so an empty binding hash skips the comparison.
    if (
        binding.sha256
        and reported.get("sha256")
        and reported["sha256"] != binding.sha256
    ):
        problems.append(
            f"metrics.json reports dataset sha256 '{reported['sha256']}' but the plan "
            f"bound '{binding.sha256}'. The wrong data was loaded."
        )
    return problems


def parse_metrics_file(
    path: Path, artifacts_dir: Path
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read, parse and validate `metrics.json`.

    Returns `(payload, errors)`. `payload` is None whenever the document is unusable —
    missing, unparseable, an unsupported version, or schema-invalid — which is what
    `sandbox_exec` turns into a `CONTRACT_VIOLATION`. A payload that is structurally valid
    but semantically wrong is returned *with* its errors, so the Debugger can see the
    numbers it is being told to distrust.
    """
    if not path.is_file():
        return None, ["/artifacts/metrics.json was not written."]

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"/artifacts/metrics.json is not readable JSON: {exc}"]

    if not isinstance(payload, dict):
        return None, [
            "/artifacts/metrics.json must contain a JSON object at the top level."
        ]

    version = payload.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        return None, [
            f"metrics.json declares schema_version {version!r}; this platform validates "
            f"{sorted(SUPPORTED_SCHEMA_VERSIONS)}."
        ]

    schema_errors = validate_schema(payload)
    if schema_errors:
        return None, schema_errors

    return payload, check_finite(payload) + check_declared_files_exist(
        payload, artifacts_dir
    )
