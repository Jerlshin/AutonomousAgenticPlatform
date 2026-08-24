"""Request/response schemas for `/benchmarks` (ARCHITECTURE.md §8.2, AGENTS.md §13).

The listing endpoints describe suites as they are written on disk; the results endpoint
answers from `benchmark_results`, which is the durable record. `BenchmarkKpis` mirrors
§13.1's table so the API and the Markdown scorecard report the same numbers from the same
rows rather than each computing their own.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BenchmarkCaseRead(BaseModel):
    """One case as declared in the suite file."""

    id: str
    prompt: str
    task_kind: str = ""
    expect: dict[str, Any] = Field(default_factory=dict)
    tests: str = Field(
        default="", description="What judgement this case is testing, if it is a trap."
    )
    trap: bool = Field(
        default=False,
        description="Judgement cases are scored separately and never averaged in.",
    )


class BenchmarkSuiteRead(BaseModel):
    name: str
    description: str = ""
    version: str
    cases: list[BenchmarkCaseRead] = Field(default_factory=list)


class BenchmarkSuiteListResponse(BaseModel):
    total: int
    suites: list[BenchmarkSuiteRead]


class BenchmarkRunRequest(BaseModel):
    """Optional narrowing of a suite execution to specific cases."""

    cases: list[str] | None = Field(
        default=None,
        description="Case ids to run. Omit to run the whole suite.",
    )


class BenchmarkRunAccepted(BaseModel):
    """202 — the suite is running in the background; poll `/results` for scores."""

    suite: str
    version: str
    cases: list[str]
    message: str


class BenchmarkResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suite: str
    case_id: str
    task_id: uuid.UUID | None = None
    run_id: str | None = None
    outcome: str | None = None
    passed: bool
    metrics: dict[str, Any] = Field(default_factory=dict)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    duration_seconds: int | None = None
    created_at: datetime


class BenchmarkKpis(BaseModel):
    """The platform KPIs of AGENTS.md §13.1, over the rows returned."""

    cases_scored: int
    expectations_met: int
    task_success_rate: float = Field(description="SUCCEEDED / total. Target ≥ 0.70.")
    judgement_score: str = Field(description="Trap cases passed. Target ≥ 2/3.")
    mean_debug_iterations: float = Field(
        description="Over successful runs. Target ≤ 1.5."
    )
    first_pass_rate: float = Field(
        description="Succeeded with 0 debug iterations. Target ≥ 0.40."
    )
    replan_rate: float = Field(description="Runs needing ≥ 1 replan. Target ≤ 0.25.")


class BenchmarkResultsResponse(BaseModel):
    suite: str
    total: int
    kpis: BenchmarkKpis
    results: list[BenchmarkResultRead]
