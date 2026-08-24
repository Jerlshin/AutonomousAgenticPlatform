"""The `/benchmarks` endpoints (ARCHITECTURE.md §8.2, AGENTS.md §13).

Three things are worth testing at this layer and nothing else is: that a bad suite or case
name is rejected *before* the 202 rather than becoming a background task that silently runs
nothing; that the background task is actually scheduled; and that the KPI table on
`/results` computes §13.1's numbers from rows rather than from whatever the last in-memory
report happened to hold.

The suite execution itself is stubbed here. A `TestClient` request runs its background
tasks synchronously on the way out, so an unstubbed `POST /run` would try to reach Docker,
Ollama and Postgres from a unit test.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.v1.benchmarks import compute_kpis
from app.core.db import get_db
from app.db.models.benchmark_result import BenchmarkResult
from app.main import app
from app.services import benchmarks


class FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> FakeResult:
        return self

    def all(self) -> list:
        return self._rows


class FakeSession:
    """Just enough `AsyncSession` for the one `select()` the results endpoint runs."""

    def __init__(self, rows: list) -> None:
        self.rows = rows
        self.statements: list = []

    async def execute(self, statement) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self.rows)


def row(
    case_id: str,
    *,
    suite: str = "core-10",
    passed: bool = True,
    outcome: str = "SUCCEEDED",
    created_at: datetime | None = None,
    **metrics,
) -> BenchmarkResult:
    return BenchmarkResult(
        id=uuid.uuid4(),
        suite=suite,
        case_id=case_id,
        run_id=str(uuid.uuid4()),
        outcome=outcome,
        passed=passed,
        metrics={"debug_iterations": 0, "replans": 0, **metrics},
        checks=[{"name": "outcome", "passed": passed, "detail": ""}],
        duration_seconds=120,
        created_at=created_at or datetime.now(UTC),
    )


@pytest.fixture
def client():
    """A client that does not run the app's lifespan.

    Startup pings Postgres. It tolerates the ping failing, but a unit test that reaches for
    a database it does not need is a test that is slower and noisier than it has to be, so
    the `TestClient` is used without its context manager — requests are served, the lifespan
    is not.
    """
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def rows(request):
    """Install a fake DB session returning `request.param` rows."""
    session = FakeSession(getattr(request, "param", []))
    app.dependency_overrides[get_db] = lambda: session
    return session


class TestListing:
    def test_the_shipped_suites_are_listed_with_their_cases(self, client):
        payload = client.get("/api/v1/benchmarks").json()
        core = next(s for s in payload["suites"] if s["name"] == "core-10")
        assert payload["total"] >= 1
        assert len(core["cases"]) == 10
        assert core["version"] == "1.0.0"

    def test_a_trap_case_is_marked_as_one_so_the_ui_can_score_it_separately(
        self, client
    ):
        payload = client.get("/api/v1/benchmarks").json()
        core = next(s for s in payload["suites"] if s["name"] == "core-10")
        traps = [c for c in core["cases"] if c["trap"]]
        assert [c["id"] for c in traps] == [
            "imbalance-trap",
            "leakage-trap",
            "impossible-target",
        ]
        assert all(c["tests"] for c in traps)


class TestRunning:
    @pytest.fixture(autouse=True)
    def stub_execution(self, monkeypatch):
        started: list[tuple[str, list[str] | None]] = []

        async def fake_run(suite, **kwargs):
            started.append((suite, kwargs.get("case_ids")))
            return benchmarks.SuiteReport(
                suite=suite, version="1.0.0", started_at=datetime.now(UTC)
            ), None

        monkeypatch.setattr(benchmarks, "run_suite_and_write", fake_run)
        return started

    def test_a_suite_run_is_accepted_and_scheduled(self, client, stub_execution):
        response = client.post("/api/v1/benchmarks/core-10/run")
        assert response.status_code == 202
        body = response.json()
        assert len(body["cases"]) == 10
        assert "poll" in body["message"]
        assert stub_execution == [("core-10", None)]

    def test_a_run_can_be_narrowed_to_named_cases(self, client, stub_execution):
        response = client.post(
            "/api/v1/benchmarks/core-10/run", json={"cases": ["bc-logreg"]}
        )
        assert response.status_code == 202
        assert response.json()["cases"] == ["bc-logreg"]
        assert stub_execution == [("core-10", ["bc-logreg"])]

    def test_an_unknown_suite_is_404_and_starts_nothing(self, client, stub_execution):
        assert client.post("/api/v1/benchmarks/nope/run").status_code == 404
        assert stub_execution == []

    def test_an_unknown_case_is_rejected_before_the_202(self, client, stub_execution):
        """Otherwise the caller gets a 202 for a background task that runs nothing."""
        response = client.post(
            "/api/v1/benchmarks/core-10/run", json={"cases": ["bc-logreg", "typo"]}
        )
        assert response.status_code == 404
        assert "typo" in response.json()["detail"]
        assert stub_execution == []


class TestResults:
    @pytest.mark.parametrize("rows", [[]], indirect=True)
    def test_a_suite_with_no_recorded_results_is_404(self, client, rows):
        assert client.get("/api/v1/benchmarks/core-10/results").status_code == 404

    @pytest.mark.parametrize(
        "rows",
        [
            [
                row("bc-logreg"),
                row("wine-multiclass", debug_iterations=2),
                row("impossible-target", outcome="PARTIAL"),
                row("leakage-trap", passed=False, outcome="FAILED", replans=1),
            ]
        ],
        indirect=True,
    )
    def test_the_kpi_table_is_computed_from_the_rows(self, client, rows):
        payload = client.get("/api/v1/benchmarks/core-10/results").json()
        kpis = payload["kpis"]
        assert payload["total"] == 4
        assert kpis["cases_scored"] == 4
        assert kpis["expectations_met"] == 3
        assert kpis["task_success_rate"] == 0.5
        assert kpis["judgement_score"] == "1/2"  # the trap and the PARTIAL case
        assert kpis["mean_debug_iterations"] == 1.0  # over the two that succeeded
        assert kpis["first_pass_rate"] == 0.25
        assert kpis["replan_rate"] == 0.25

    @pytest.mark.parametrize(
        "rows",
        [
            [
                row("bc-logreg", passed=True, created_at=datetime.now(UTC)),
                row(
                    "bc-logreg",
                    passed=False,
                    outcome="FAILED",
                    created_at=datetime.now(UTC) - timedelta(days=1),
                ),
            ]
        ],
        indirect=True,
    )
    def test_only_the_newest_run_of_a_case_is_scored_by_default(self, client, rows):
        """Yesterday's failure must not drag down today's board, but stays in history."""
        payload = client.get("/api/v1/benchmarks/core-10/results").json()
        assert payload["total"] == 2  # both rows are returned
        assert payload["kpis"]["cases_scored"] == 1  # one is scored
        assert payload["kpis"]["task_success_rate"] == 1.0

    @pytest.mark.parametrize(
        "rows",
        [[row("bc-logreg"), row("bc-logreg", passed=False, outcome="FAILED")]],
        indirect=True,
    )
    def test_history_can_be_scored_in_full(self, client, rows):
        payload = client.get(
            "/api/v1/benchmarks/core-10/results", params={"latest_only": False}
        ).json()
        assert payload["kpis"]["cases_scored"] == 2


class TestKpiArithmetic:
    def test_an_empty_set_reports_zeros_rather_than_dividing_by_zero(self):
        kpis = compute_kpis([])
        assert kpis.cases_scored == 0
        assert kpis.task_success_rate == 0.0
        assert kpis.judgement_score == "n/a"

    def test_the_judgement_score_covers_the_three_section_13_traps(self):
        """Two are named `-trap`; `impossible-target` is caught by expecting PARTIAL."""
        kpis = compute_kpis(
            [
                row("imbalance-trap"),
                row("leakage-trap", passed=False, outcome="FAILED"),
                row("impossible-target", outcome="PARTIAL"),
                row("bc-logreg"),
            ]
        )
        assert kpis.judgement_score == "2/3"

    def test_the_mean_debug_iterations_covers_successful_runs_only(self):
        """A crashed run's debug count measures the crash, not the self-correction depth."""
        kpis = compute_kpis(
            [
                row("a", debug_iterations=1),
                row("b", debug_iterations=3),
                row("c", passed=False, outcome="FAILED", debug_iterations=99),
            ]
        )
        assert kpis.mean_debug_iterations == 2.0
