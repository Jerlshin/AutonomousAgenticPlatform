"""Benchmark suite listing, execution and history (ARCHITECTURE.md §8.2, AGENTS.md §13).

`core-10` is the primary regression gate for the whole platform, and running it means
running the whole graph ten times — 40–80 minutes of real containers, real models and real
MLflow runs. So `POST /benchmarks/{suite}/run` answers `202 Accepted` and executes in the
background: an endpoint that blocked for an hour would be indistinguishable from one that
had hung, and the scores are queryable from `/results` as each case lands.

The suite files are the source of truth for *what* is measured (`benchmarks/suites/*.yaml`);
`benchmark_results` is the source of truth for *what was measured*, which is why the KPI
table on `/results` is computed from rows rather than from a report object held in memory —
it stays answerable across restarts and across the process that ran the suite.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import require_token
from app.db.models.benchmark_result import BenchmarkResult
from app.schemas.benchmark import (
    BenchmarkCaseRead,
    BenchmarkKpis,
    BenchmarkResultRead,
    BenchmarkResultsResponse,
    BenchmarkRunAccepted,
    BenchmarkRunRequest,
    BenchmarkSuiteListResponse,
    BenchmarkSuiteRead,
)
from app.services import benchmarks

logger = logging.getLogger(__name__)

# §13.2: every non-health endpoint requires `Authorization: Bearer {PLATFORM_API_TOKEN}`.
# Declared on the router rather than per-endpoint so a route added later inherits it —
# authentication that has to be remembered on each handler is authentication that will
# eventually be forgotten on one.
router = APIRouter(dependencies=[Depends(require_token)])

# How a judgement case is recognised from a `benchmark_results` row, which carries the
# case id but not the suite file's `trap:` flag. Two of the three §13 traps are named for
# it; the third (`impossible-target`) is caught by expecting a non-SUCCEEDED outcome. See
# `compute_kpis`.
TRAP_SUFFIX = "-trap"


def _suite_read(suite: benchmarks.BenchmarkSuite) -> BenchmarkSuiteRead:
    return BenchmarkSuiteRead(
        name=suite.name,
        description=suite.description,
        version=suite.version,
        cases=[
            BenchmarkCaseRead(
                id=c.id,
                prompt=c.prompt,
                task_kind=c.task_kind,
                expect=c.expect,
                tests=c.tests,
                trap=c.trap,
            )
            for c in suite.cases
        ],
    )


@router.get(
    "", response_model=BenchmarkSuiteListResponse, summary="List benchmark suites"
)
async def list_benchmark_suites() -> Any:
    """Every suite in `benchmarks/suites/`, with its cases and their expectations."""
    suites = [_suite_read(s) for s in benchmarks.list_suites()]
    return BenchmarkSuiteListResponse(total=len(suites), suites=suites)


@router.post(
    "/{suite}/run",
    response_model=BenchmarkRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Execute a benchmark suite",
)
async def run_benchmark_suite(
    suite: str,
    background_tasks: BackgroundTasks,
    payload: BenchmarkRunRequest | None = None,
) -> Any:
    """Start a suite in the background and return the cases that will run.

    The suite is loaded and the requested case ids validated *before* the 202, so a typo in
    a case name is an error the caller sees rather than a background task that quietly runs
    nothing.
    """
    try:
        definition = benchmarks.load_suite(suite)
    except benchmarks.SuiteNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc

    requested = (payload.cases if payload else None) or None
    if requested is not None:
        unknown = [c for c in requested if definition.case(c) is None]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Suite '{suite}' has no cases {unknown}.",
            )

    cases = [c.id for c in definition.cases if requested is None or c.id in requested]
    background_tasks.add_task(_execute, definition.name, requested)
    return BenchmarkRunAccepted(
        suite=definition.name,
        version=definition.version,
        cases=cases,
        message=(
            f"Running {len(cases)} case(s). Each case executes the full graph; poll "
            f"GET /api/v1/benchmarks/{definition.name}/results for scores as they land."
        ),
    )


async def _execute(suite: str, case_ids: list[str] | None) -> None:
    """The background half of `POST /{suite}/run`.

    Absorbs its own failures: a `BackgroundTasks` callable that raises has nowhere to
    report to — the response was sent long ago — so the exception is logged here rather
    than disappearing into the ASGI server's handler.
    """
    try:
        report, path = await benchmarks.run_suite_and_write(suite, case_ids=case_ids)
        logger.info(
            "Benchmark suite %s finished: %d/%d expectations met, judgement %s, "
            "scorecard %s",
            suite,
            report.passed,
            report.total,
            report.judgement_score,
            path or "(not written)",
        )
    except Exception:
        logger.exception("Benchmark suite %s failed to run", suite)


@router.get(
    "/{suite}/results",
    response_model=BenchmarkResultsResponse,
    summary="Historical benchmark scores",
)
async def get_benchmark_results(
    suite: str,
    limit: int = Query(50, ge=1, le=500),
    latest_only: bool = Query(
        default=True,
        description="Score only the most recent run of each case, which is what the KPI "
        "targets are defined over.",
    ),
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Recorded results for a suite, newest first, with §13.1's KPIs computed over them."""
    result = await db.execute(
        select(BenchmarkResult)
        .where(BenchmarkResult.suite == suite)
        .order_by(BenchmarkResult.created_at.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No recorded results for benchmark suite '{suite}'.",
        )

    scored = _deduplicate(rows) if latest_only else rows
    return BenchmarkResultsResponse(
        suite=suite,
        total=len(rows),
        kpis=compute_kpis(scored),
        results=[BenchmarkResultRead.model_validate(row) for row in rows],
    )


def _deduplicate(rows: list[BenchmarkResult]) -> list[BenchmarkResult]:
    """The newest row per case. `rows` arrives newest-first, so the first wins."""
    seen: dict[str, BenchmarkResult] = {}
    for row in rows:
        seen.setdefault(row.case_id, row)
    return list(seen.values())


def compute_kpis(rows: list[BenchmarkResult]) -> BenchmarkKpis:
    """AGENTS.md §13.1, computed from `benchmark_results` rows.

    Trap cases are identified by the `-trap` suffix or by an expectation of a non-`SUCCEEDED`
    outcome — `impossible-target` is a judgement case whose whole point is that succeeding
    would be the failure — so the Judgement Score survives a case being renamed as long as
    the convention holds.
    """
    total = len(rows)
    if total == 0:
        return BenchmarkKpis(
            cases_scored=0,
            expectations_met=0,
            task_success_rate=0.0,
            judgement_score="n/a",
            mean_debug_iterations=0.0,
            first_pass_rate=0.0,
            replan_rate=0.0,
        )

    succeeded = [r for r in rows if r.outcome == "SUCCEEDED"]
    traps = [
        r for r in rows if r.case_id.endswith(TRAP_SUFFIX) or r.outcome == "PARTIAL"
    ]

    def counter(row: BenchmarkResult, key: str) -> float:
        value = (row.metrics or {}).get(key)
        return float(value) if isinstance(value, (int, float)) else 0.0

    return BenchmarkKpis(
        cases_scored=total,
        expectations_met=sum(1 for r in rows if r.passed),
        task_success_rate=len(succeeded) / total,
        judgement_score=(
            f"{sum(1 for r in traps if r.passed)}/{len(traps)}" if traps else "n/a"
        ),
        mean_debug_iterations=(
            sum(counter(r, "debug_iterations") for r in succeeded) / len(succeeded)
            if succeeded
            else 0.0
        ),
        first_pass_rate=(
            sum(1 for r in succeeded if not counter(r, "debug_iterations")) / total
        ),
        replan_rate=sum(1 for r in rows if counter(r, "replans")) / total,
    )
