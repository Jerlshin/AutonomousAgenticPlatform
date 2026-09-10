"""`compute_kpis` agrees with the contract fixture the dashboard asserts against (§13).

docs/FRONTEND.md §8.10 needs KPI *deltas* between a suite's latest execution and the one
before it, and `GET /benchmarks/{suite}/results` scores only the newest row per case. So
`frontend/src/lib/benchmarks.ts::computeKpis` mirrors `compute_kpis` in TypeScript.

Two implementations of one formula drift silently — the dashboard would show one success
rate on a tile and another in the delta beneath it. `frontend/tests/fixtures/benchmark-kpis.json`
is a third, independent statement of the formula, and both languages assert against it:
this test covers the Python side, `frontend/tests/contract/benchmarkKpis.test.ts` the
TypeScript. Changing the formula in either language reddens that language's suite at once.

The fixture is hand-authored rather than generated on purpose. A fixture emitted by
`compute_kpis` could only ever prove that `compute_kpis` agrees with itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.api.v1.benchmarks import compute_kpis

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "frontend" / "tests" / "fixtures" / "benchmark-kpis.json"


@dataclass
class Row:
    """The attributes `compute_kpis` reads off a `BenchmarkResult` row.

    Duck-typed rather than constructed as an ORM object so this test needs no database:
    the function reads `case_id`, `outcome`, `passed` and `metrics` and nothing else.
    """

    case_id: str
    passed: bool
    outcome: str | None
    metrics: dict[str, Any] = field(default_factory=dict)


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    assert FIXTURE.is_file(), (
        f"{FIXTURE.relative_to(REPO_ROOT)} is missing. It is the shared KPI contract "
        "between this suite and the dashboard's; it is hand-authored and checked in."
    )
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rows(contract: dict[str, Any]) -> list[Row]:
    return [
        Row(
            case_id=row["case_id"],
            passed=row["passed"],
            outcome=row["outcome"],
            metrics=row.get("metrics") or {},
        )
        for row in contract["results"]
    ]


def test_counts_match_the_contract(contract: dict[str, Any], rows: list[Row]) -> None:
    kpis = compute_kpis(rows)  # type: ignore[arg-type]
    expected = contract["kpis"]
    assert kpis.cases_scored == expected["cases_scored"]
    assert kpis.expectations_met == expected["expectations_met"]


def test_judgement_score_matches_the_contract(
    contract: dict[str, Any], rows: list[Row]
) -> None:
    """A string ratio, not a float — the trap denominator has to stay visible."""
    kpis = compute_kpis(rows)  # type: ignore[arg-type]
    assert kpis.judgement_score == contract["kpis"]["judgement_score"]


def test_rates_match_the_contract(contract: dict[str, Any], rows: list[Row]) -> None:
    kpis = compute_kpis(rows)  # type: ignore[arg-type]
    expected = contract["kpis"]
    assert kpis.task_success_rate == pytest.approx(expected["task_success_rate"])
    assert kpis.first_pass_rate == pytest.approx(expected["first_pass_rate"])
    assert kpis.replan_rate == pytest.approx(expected["replan_rate"])
    assert kpis.mean_debug_iterations == pytest.approx(
        expected["mean_debug_iterations"]
    )


def test_a_row_without_metrics_stays_in_the_denominator(rows: list[Row]) -> None:
    """`ingest-smoke` carries no metrics at all.

    It must still be counted: dropping a row whose counters are absent would inflate every
    rate, which is the failure mode of a permissive counter and the reason the fixture
    includes one.
    """
    assert any(row.metrics == {} for row in rows), "the fixture lost its metric-less row"
    assert compute_kpis(rows).cases_scored == len(rows)  # type: ignore[arg-type]


def test_empty_input_does_not_divide_by_zero() -> None:
    kpis = compute_kpis([])
    assert kpis.cases_scored == 0
    assert kpis.judgement_score == "n/a"
    assert kpis.task_success_rate == 0.0
