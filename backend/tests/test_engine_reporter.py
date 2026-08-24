"""Report construction (AGENTS.md §7.8).

Two guarantees are under test, and both are about what the platform refuses to delegate:

* **The document is always complete.** Eight sections, on every state — including one where
  planning never finished and nothing ever ran.
* **The numbers are the platform's.** A model that rounds, reorders or invents a metric
  cannot get it into the report, because the data sections are spliced in after generation.
"""

from __future__ import annotations

import uuid

from app.engine.reporting import (
    DATA_SECTIONS,
    SECTION_TITLES,
    assemble_report,
    criteria_table,
    debug_cycles,
    render_report,
    report_context,
    split_sections,
)
from app.engine.state import (
    Budgets,
    CodeRevision,
    Deliverable,
    Diagnosis,
    ErrorKind,
    ErrorRecord,
    Plan,
    PlanStep,
    RunOutcome,
    SandboxOutcome,
    StepKind,
    StepStatus,
    SuccessCriterion,
    Usage,
    ValidationReport,
)
from tests.fakes import CLEAN_METRICS

CRITERIA = [
    SuccessCriterion(
        id="c1", metric="accuracy", comparator="gte", threshold=0.95, weight=2.0
    ),
    SuccessCriterion(
        id="c2",
        metric="roc_auc",
        comparator="gte",
        threshold=0.99,
        required=False,
    ),
]


def plan() -> Plan:
    return Plan(
        steps=[
            PlanStep(
                id="s1",
                index=0,
                title="Retrieve pipeline APIs",
                description="Find exact signatures.",
                kind=StepKind.RESEARCH,
            ),
            PlanStep(
                id="s2",
                index=1,
                title="Train a classifier",
                description="Stratified 80/20 split, seed 42.",
                kind=StepKind.TRAIN,
                depends_on=["s1"],
            ),
        ],
        success_criteria=CRITERIA,
        task_kind="tabular-classification",
        primary_metric="accuracy",
        assumptions=["80/20 stratified split with seed 42."],
    )


def outcome(
    metrics: dict | None = None, classification: str = "CLEAN"
) -> SandboxOutcome:
    return SandboxOutcome(
        execution_id=uuid.uuid4(),
        profile="train",
        classification=classification,
        exit_code=0 if classification == "CLEAN" else 1,
        duration_ms=4200,
        metrics=metrics,
        validation=ValidationReport(passed=True, warnings=["no main guard"]),
        revision=1,
    )


def succeeded_state(**overrides) -> dict:
    state = {
        "run_id": "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0",
        "prompt": "Build a classifier on breast_cancer reaching at least 95% accuracy.",
        "plan": plan(),
        "current_step_id": "s2",
        "step_status": {"s1": StepStatus.SKIPPED, "s2": StepStatus.SUCCEEDED},
        "last_outcome": outcome(CLEAN_METRICS),
        "current_revision": CodeRevision(revision=1, content="x = 1", sha256="b" * 64),
        "code_revisions": [CodeRevision(revision=1, content="x = 1", sha256="b" * 64)],
        "deliverables": [
            Deliverable(
                name="metrics.json",
                artifact_type="metrics",
                path="/runs/x/artifacts/metrics.json",
                sha256="c" * 64,
                size_bytes=2048,
                mime_type="application/json",
            )
        ],
        "usage": Usage(
            tokens_in=900, tokens_out=400, llm_calls=3, sandbox_executions=1
        ),
        "budgets": Budgets(),
        "metadata": {"seed": 42},
    }
    state.update(overrides)
    return state


class TestContext:
    def test_a_clean_run_meeting_its_criteria_is_reported_as_succeeded(self):
        context = report_context(succeeded_state())
        assert context["status"] == RunOutcome.SUCCEEDED.value
        assert context["metrics"]["accuracy"] == 0.9737

    def test_criteria_rows_carry_the_observed_value_not_a_rounded_one(self):
        rows = {
            row["metric"]: row for row in report_context(succeeded_state())["criteria"]
        }
        assert rows["accuracy"]["achieved"] == "0.9737"
        assert rows["accuracy"]["status"] == "✅ Pass"

    def test_a_missing_metric_is_not_measured_and_a_stretch_goal_is_a_miss(self):
        """Absence is never success — that asymmetry is what stops silent wins."""
        rows = {
            row["metric"]: row for row in report_context(succeeded_state())["criteria"]
        }
        assert rows["roc_auc"]["achieved"] == "not measured"
        assert "Miss (stretch goal)" in rows["roc_auc"]["status"]

    def test_a_required_metric_below_threshold_is_partial_not_failed(self):
        metrics = {**CLEAN_METRICS, "metrics": {"accuracy": 0.88}}
        context = report_context(succeeded_state(last_outcome=outcome(metrics)))
        assert context["status"] == RunOutcome.PARTIAL.value
        assert "0.88" in context["headline"]

    def test_the_reproduction_block_names_the_seed_and_the_code_hash(self):
        repro = report_context(succeeded_state())["reproduction"]
        assert repro["seed"] == 42
        assert repro["code_sha256"] == "b" * 64
        assert repro["profile"] == "train"
        assert repro["image"]

    def test_limitations_name_the_step_that_was_planned_and_not_run(self):
        limitations = " ".join(report_context(succeeded_state())["limitations"])
        assert "Retrieve pipeline APIs" in limitations
        assert "no main guard" in limitations

    def test_the_context_is_total_on_a_run_that_never_planned_anything(self):
        """The Reporter runs on every terminal path, including the earliest ones."""
        context = report_context({"run_id": "r1", "prompt": "do a thing"})
        assert context["status"] == RunOutcome.FAILED.value
        assert context["criteria"] == []
        assert context["steps"] == []
        assert context["reproduction"]["dataset_id"] == "none"

    def test_a_cancelled_run_is_reported_as_cancelled(self):
        context = report_context(succeeded_state(cancel_requested=True))
        assert context["status"] == RunOutcome.CANCELLED.value


class TestDebugCycles:
    def test_each_failure_is_paired_with_its_diagnosis_and_the_revision_that_fixed_it(
        self,
    ):
        state = succeeded_state(
            errors=[_error("KeyError:target", 1)],
            diagnoses=[_diagnosis()],
            code_revisions=[
                CodeRevision(revision=1, content="a", sha256="1" * 64),
                CodeRevision(
                    revision=2,
                    content="b",
                    sha256="2" * 64,
                    rationale="read the right column",
                ),
            ],
        )
        (cycle,) = debug_cycles(state)
        assert cycle["fingerprint"] == "KeyError:target"
        assert cycle["root_cause"] == "wrong column"
        assert cycle["fix_revision"] == 2
        assert cycle["resolved"] is True

    def test_a_repeat_of_the_same_fingerprint_is_not_recorded_as_resolved(self):
        state = succeeded_state(
            errors=[_error("KeyError:target", 1), _error("KeyError:target", 2)],
            diagnoses=[_diagnosis(), _diagnosis()],
            last_outcome=outcome(None, "RUNTIME_ERROR"),
        )
        first, second = debug_cycles(state)
        assert first["resolved"] is False
        assert second["resolved"] is False

    def test_a_degraded_debugger_does_not_shift_the_pairing(self):
        """A missing diagnosis renders as 'none recorded', not as the next one."""
        state = succeeded_state(
            errors=[_error("A", 1), _error("B", 2)],
            diagnoses=[_diagnosis()],
        )
        first, second = debug_cycles(state)
        assert first["root_cause"] == "wrong column"
        assert second["root_cause"] == ""


class TestDeterministicTemplate:
    def test_every_required_section_is_present(self):
        report = render_report(report_context(succeeded_state()))
        for number, title in enumerate(SECTION_TITLES, start=1):
            assert f"## {number}. {title}" in report

    def test_the_template_states_the_real_numbers(self):
        report = render_report(report_context(succeeded_state()))
        assert "0.9737" in report
        assert "`accuracy`" in report
        assert "**Status:** SUCCEEDED" in report

    def test_a_run_with_no_failures_says_so_explicitly(self):
        """Section 4 is mandatory; silence and success must not look the same."""
        report = render_report(report_context(succeeded_state()))
        assert "No execution failures occurred" in report

    def test_a_run_that_never_executed_does_not_claim_a_clean_first_attempt(self):
        report = render_report(report_context({"run_id": "r1", "prompt": "do a thing"}))
        assert "no program was ever executed" in report

    def test_the_debugging_narrative_names_every_attempt(self):
        state = succeeded_state(
            errors=[_error("KeyError:target", 1), _error("ValueError:shape", 2)],
            diagnoses=[_diagnosis(), _diagnosis(root_cause="mismatched split")],
        )
        report = render_report(report_context(state))
        assert "Attempt 1" in report and "Attempt 2" in report
        assert "mismatched split" in report

    def test_the_footer_appears_exactly_once(self):
        report = render_report(report_context(succeeded_state()))
        assert report.count("3 model calls") == 1

    def test_rendering_survives_a_broken_template_engine(self, monkeypatch):
        """`SYNTHESISE_FALLBACK` has no exception clause — not even for Jinja2."""
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "jinja2":
                raise ImportError("no jinja2 here")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)
        report = render_report(report_context(succeeded_state()))
        for number in range(1, 9):
            assert f"## {number}." in report
        assert "0.9737" in report


class TestAssembly:
    def test_model_prose_is_kept_for_the_narrative_sections(self):
        context = report_context(succeeded_state())
        raw = (
            "## 1. Objective\nA long enough restatement of what the user actually asked "
            "for, in prose.\n\n"
            "## 3. Approach\nA stratified split and a regularised linear model, chosen "
            "for its calibration.\n"
        )
        report = assemble_report(raw, context)
        assert "in prose" in report
        assert "chosen for its calibration" in report

    def test_missing_sections_are_filled_from_the_template(self):
        context = report_context(succeeded_state())
        report = assemble_report("## 1. Objective\n" + "x" * 60, context)
        for number, title in enumerate(SECTION_TITLES, start=1):
            assert f"## {number}. {title}" in report
        assert "No execution failures occurred" in report

    def test_a_heading_with_nothing_under_it_counts_as_missing(self):
        context = report_context(succeeded_state())
        report = assemble_report(
            "## 4. What went wrong\n\n## 7. Limitations\n", context
        )
        assert "No execution failures occurred" in report

    def test_the_data_sections_are_never_the_models(self):
        context = report_context(succeeded_state())
        raw = "\n".join(
            f"## {n}. Section\nInvented content for section {n}, at some length."
            for n in DATA_SECTIONS
        )
        report = assemble_report(raw, context)
        assert "Invented content" not in report
        assert "make reproduce RUN_ID=" in report

    def test_a_model_invented_criteria_table_is_replaced_with_the_real_one(self):
        context = report_context(succeeded_state())
        raw = (
            "## 2. Result\nThe model performed superbly on every axis measured.\n\n"
            "| Criterion | Target | Achieved | Status |\n"
            "|---|---|---|---|\n"
            "| accuracy | >= 0.95 | 0.9999 | Pass |\n"
        )
        report = assemble_report(raw, context)
        assert "0.9999" not in report
        assert "0.9737" in report
        assert "performed superbly" in report

    def test_a_model_that_returns_nothing_useful_yields_the_whole_template(self):
        context = report_context(succeeded_state())
        assembled = split_sections(assemble_report("I cannot do that.", context))
        assert assembled == split_sections(render_report(context))

    def test_the_footer_is_not_duplicated_by_assembly(self):
        context = report_context(succeeded_state())
        assert (
            assemble_report("## 1. Objective\n" + "y" * 60, context).count(
                "3 model calls"
            )
            == 1
        )


class TestSectionSplitting:
    def test_sections_are_split_on_their_numbers(self):
        sections = split_sections("## 1. Objective\nfirst\n\n## 2. Result\nsecond\n")
        assert sections == {1: "first", 2: "second"}

    def test_the_numbering_style_a_model_drifts_into_still_parses(self):
        sections = split_sections("##2 Objective\nfirst\n## 3) Approach\nthird\n")
        assert sections[2] == "first"
        assert sections[3] == "third"

    def test_an_empty_document_has_no_sections(self):
        assert split_sections("") == {}


class TestCriteriaTable:
    def test_no_criteria_says_so_rather_than_rendering_an_empty_table(self):
        assert "No success criteria" in criteria_table([])

    def test_a_row_per_criterion(self):
        table = criteria_table(report_context(succeeded_state())["criteria"])
        assert table.count("\n") == 3  # header, separator, two rows


def _error(fingerprint: str, revision: int) -> ErrorRecord:
    return ErrorRecord(
        kind=ErrorKind.DATA,
        fingerprint=fingerprint,
        exception_type=fingerprint.split(":")[0],
        message="'target'",
        file="/workspace/main.py",
        line=12,
        revision=revision,
    )


def _diagnosis(**overrides) -> Diagnosis:
    base = {
        "error_fingerprint": "KeyError:target",
        "root_cause": "wrong column",
        "fix_strategy": "read the right one",
        "targeted_changes": ["rename it"],
        "confidence": 0.8,
    }
    return Diagnosis(**{**base, **overrides})


class TestLastResort:
    """The `SYNTHESISE_FALLBACK` guarantee has no exception clause (§6.5)."""

    def test_a_node_whose_fallback_also_raises_still_returns_a_state_update(self):
        """A fallback that raised would silently turn "cannot fail" into "no deliverable"."""
        from app.engine.nodes.base import FailurePolicy, RunPhase, node

        def broken_fallback(_state, _exc):
            raise RuntimeError("the fallback is broken too")

        @node(
            name="fragile",
            phase=RunPhase.REPORT,
            policy=FailurePolicy.SYNTHESISE_FALLBACK,
            fallback=broken_fallback,
        )
        async def fragile(_state, _config):
            raise RuntimeError("the body is broken")

        from tests.fakes import run

        update = run(fragile({}, {}))
        assert update["usage"].node_visits == 1
        assert "fragile_failure" in update["metadata"]

    def test_the_reporter_node_falls_back_when_its_model_is_unreachable(self):
        from app.engine.nodes.reporter import fallback_report

        update = fallback_report(succeeded_state(), RuntimeError("ollama is down"))
        for number in range(1, 9):
            assert f"## {number}." in update["report_markdown"]

    def test_a_report_for_a_state_with_no_prompt_is_still_titled(self):
        assert report_context({"run_id": "abc"})["title"] == "Run abc"

    def test_a_very_long_prompt_is_trimmed_into_a_title(self):
        context = report_context({"run_id": "abc", "prompt": "x" * 300})
        assert len(context["title"]) == 108
        assert context["title"].endswith("…")
