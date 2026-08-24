"""End-to-end execution of the graph with a mock LLM.

`init → planner → coder → sandbox_exec → debugger → coder → … → reporter → finalizer`,
driven by scripted model replies and a scripted sandbox. No Ollama, no Docker, no Postgres
— the control flow is what is under test, and it must be deterministic (design principle
P1).

The properties every scenario asserts are the ones the whole design rests on:

* the run always reaches `finalizer` through `reporter`, and always leaves a deliverable,
  including on every failure path (`AGENTS.md` §6.4, corollary);
* the outcome is computed from `metrics.json` by arithmetic, never from model prose;
* the correctness loop is bounded — by the debug budget, the sandbox budget, and the
  stagnation rule — so a run that cannot fix itself stops saying so rather than spinning.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.engine.graph import build_graph, run_config
from app.engine.state import Budgets, RunOutcome, StepStatus
from tests.conftest import (
    PLAN_JSON,
    REPORT_MARKDOWN,
    VALID_PROGRAM,
    diagnosis_reply,
)
from tests.fakes import CLEAN_METRICS, FakeChatModel, FakeSandboxDriver, run

RUN_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
PROMPT = "Build a classifier on breast_cancer reaching at least 95% test accuracy."

KEY_ERROR_STDERR = (
    "Traceback (most recent call last):\n"
    '  File "/workspace/main.py", line 12, in main\n'
    "    y = df['target']\n"
    "KeyError: 'target'\n"
)


def variant(marker: str) -> str:
    """`VALID_PROGRAM` with a distinguishing comment, so revisions are not byte-identical.

    The Coder refuses to resubmit code it already ran (`_rejection_reason`), which is
    correct behaviour and would otherwise cost every multi-revision test an extra
    re-prompt round it is not trying to exercise.
    """
    sidecar = {"rationale": marker, "requirements": [], "addresses_error": None}
    program = f"# {marker}\n{VALID_PROGRAM}"
    return f"```python\n{program}```\n```json\n{json.dumps(sidecar)}\n```"


def invoke(
    *,
    runs_root: Path,
    plan_replies: list[str],
    coder_replies: list[str],
    debugger_replies: list[str] | None = None,
    reporter_replies: list[str] | None = None,
    driver: FakeSandboxDriver | None = None,
    initial: dict | None = None,
) -> tuple[dict, dict]:
    """Run the graph to completion and return `(final_state, doubles)`."""
    planner = FakeChatModel(plan_replies)
    coder = FakeChatModel(coder_replies)
    debugger = FakeChatModel(debugger_replies or [diagnosis_reply()])
    reporter = FakeChatModel(reporter_replies or [REPORT_MARKDOWN])
    sandbox = driver or FakeSandboxDriver(runs_root, metrics=CLEAN_METRICS)

    graph = build_graph(InMemorySaver())
    config = run_config(
        RUN_ID,
        llm_clients={
            "planner": planner,
            "coder": coder,
            "debugger": debugger,
            "reporter": reporter,
        },
        sandbox_driver=sandbox,
    )
    state = run(
        graph.ainvoke({"run_id": RUN_ID, "prompt": PROMPT, **(initial or {})}, config)
    )
    return state, {
        "planner": planner,
        "coder": coder,
        "debugger": debugger,
        "reporter": reporter,
        "sandbox": sandbox,
        "graph": graph,
    }


class TestHappyPath:
    @pytest.fixture
    def result(self, runs_root, plan_reply, coder_reply):
        return invoke(
            runs_root=runs_root, plan_replies=[plan_reply], coder_replies=[coder_reply]
        )

    def test_the_run_succeeds(self, result):
        state, _ = result
        assert state["outcome"] is RunOutcome.SUCCEEDED

    def test_every_node_ran_exactly_once(self, result):
        """`node_visits` is the potential function in the termination proof (§6.4)."""
        state, _ = result
        # init, planner, coder, sandbox_exec, reporter, finalizer. No debugger: a clean
        # execution never enters loop 1.
        assert state["usage"].node_visits == 6
        assert state["usage"].sandbox_executions == 1
        assert state["usage"].llm_calls == 3  # planner, coder, reporter
        assert state.get("debug_iterations") == 0

    def test_the_plan_carries_a_measurable_criteria_contract(self, result):
        state, _ = result
        plan = state["plan"]
        assert plan.task_kind == "tabular-classification"
        assert plan.primary_metric == "accuracy"
        assert {c.metric for c in plan.success_criteria} == {"accuracy", "f1_macro"}
        assert state["plan_history"] == [plan]

    def test_the_train_step_ran_and_the_research_step_was_skipped_not_faked(
        self, result
    ):
        """No Researcher exists yet; the record says skipped rather than implying success."""
        state, _ = result
        assert state["step_status"]["s1"] is StepStatus.SKIPPED
        assert state["step_status"]["s2"] is StepStatus.SUCCEEDED

    def test_the_sandbox_ran_the_train_profile_with_the_generated_code(self, result):
        _state, doubles = result
        call = doubles["sandbox"].calls[0]
        assert call["profile"] == "train"
        assert "PLUTON_SEED" in call["code"]

    def test_execution_was_classified_clean_from_the_exit_state(self, result):
        state, _ = result
        outcome = state["last_outcome"]
        assert outcome.classification == "CLEAN"
        assert outcome.exit_code == 0
        assert outcome.metrics["metrics"]["accuracy"] == 0.9737
        assert state.get("last_error") is None

    def test_a_bundle_and_manifest_are_written(self, result, runs_root):
        state, _ = result
        final_dir = runs_root / RUN_ID / "final"
        bundle = final_dir / "bundle.zip"

        assert bundle.is_file()
        assert (final_dir / "deliverables.json").is_file()
        assert (final_dir / "SUMMARY.md").is_file()

        names = zipfile.ZipFile(bundle).namelist()
        assert "code/main.py" in names
        assert "logs/stdout.log" in names
        assert "artifacts/metrics.json" in names

        manifest = json.loads((final_dir / "deliverables.json").read_text())
        assert manifest["outcome"] == "SUCCEEDED"
        assert manifest["metrics"]["metrics"]["accuracy"] == 0.9737

        kinds = {d.artifact_type for d in state["deliverables"]}
        assert {"metrics", "bundle", "report"} <= kinds

    def test_the_summary_states_the_result_and_the_criteria(self, result, runs_root):
        summary = (runs_root / RUN_ID / "final" / "SUMMARY.md").read_text()
        assert "SUCCEEDED" in summary
        assert "accuracy" in summary
        assert "0.9737" in summary
        # The budget the summary reports must match the run's own accounting.
        assert "Node visits: 6" in summary

    def test_the_final_state_is_checkpointed(self, result):
        state, doubles = result
        snapshot = doubles["graph"].get_state(run_config(RUN_ID))
        assert snapshot.values["outcome"] is RunOutcome.SUCCEEDED
        assert snapshot.values["run_id"] == state["run_id"]


class TestQualityMiss:
    def test_clean_execution_below_a_required_threshold_is_partial_not_failed(
        self, runs_root, plan_reply, coder_reply
    ):
        """A real, reproducible result that is not good enough is not a crash."""
        metrics = {**CLEAN_METRICS, "metrics": {"accuracy": 0.88, "f1_macro": 0.87}}
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            driver=FakeSandboxDriver(runs_root, metrics=metrics),
        )
        assert state["last_outcome"].classification == "CLEAN"
        assert state["outcome"] is RunOutcome.PARTIAL

    def test_a_missing_required_metric_is_a_contract_violation_not_a_quality_miss(
        self, runs_root, plan_reply, coder_reply
    ):
        """The plan evaluates f1_macro; a run that never computed it has not been measured."""
        metrics = {**CLEAN_METRICS, "metrics": {"accuracy": 0.99}}
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            driver=FakeSandboxDriver(runs_root, metrics=metrics),
        )
        assert state["last_outcome"].classification == "CONTRACT_VIOLATION"
        assert "f1_macro" in state["last_error"].message
        assert state["outcome"] is RunOutcome.FAILED


class TestExecutionFailures:
    def test_exit_zero_without_metrics_is_a_contract_violation(
        self, runs_root, plan_reply, coder_reply
    ):
        """§10.9 — a train run that exits 0 with no metrics has produced no deliverable."""
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            driver=FakeSandboxDriver(runs_root, metrics=None),
        )
        assert state["last_outcome"].classification == "CONTRACT_VIOLATION"
        assert state["last_outcome"].metrics is None
        assert state["outcome"] is RunOutcome.FAILED
        assert state["step_status"]["s2"] is StepStatus.FAILED

    def test_a_traceback_becomes_a_structured_error_record(
        self, runs_root, plan_reply, coder_reply
    ):
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            driver=FakeSandboxDriver(
                runs_root,
                script=[
                    {"exit_code": 1, "metrics": None, "stderr": KEY_ERROR_STDERR},
                    {"metrics": CLEAN_METRICS},
                ],
            ),
        )

        error = state["errors"][0]
        assert error.exception_type == "KeyError"
        assert error.fingerprint == "KeyError:target"
        assert error.line == 12
        assert error.offending_source is not None
        # The fix ran, so the record of the failure survives while `last_error` is stale
        # by design: `errors` accumulates precisely so the history is not overwritten.
        assert state["last_outcome"].classification == "CLEAN"

    def test_a_timeout_is_classified_from_the_runtime_not_from_stderr(
        self, runs_root, plan_reply, coder_reply
    ):
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            driver=FakeSandboxDriver(
                runs_root, exit_code=137, metrics=None, timed_out=True
            ),
        )
        assert state["last_outcome"].classification == "TIMEOUT"
        assert state["outcome"] is RunOutcome.FAILED

    def test_an_oom_kill_is_distinguished_from_an_ordinary_crash(
        self, runs_root, plan_reply, coder_reply
    ):
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            driver=FakeSandboxDriver(
                runs_root, exit_code=137, metrics=None, oom_killed=True
            ),
        )
        assert state["last_outcome"].classification == "OOM"

    def test_a_failed_run_still_produces_a_bundle(
        self, runs_root, plan_reply, coder_reply
    ):
        """The deliverable guarantee holds on every terminal path."""
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            driver=FakeSandboxDriver(
                runs_root, exit_code=1, metrics=None, stderr="boom\n"
            ),
        )
        summary = (runs_root / RUN_ID / "final" / "SUMMARY.md").read_text()
        assert (runs_root / RUN_ID / "final" / "bundle.zip").is_file()
        assert "FAILED" in summary
        assert "What went wrong" in summary
        assert state["outcome"] is RunOutcome.FAILED


class TestAgentFailureDiversions:
    def test_a_planner_that_never_produces_a_plan_diverts_to_the_finalizer(
        self, runs_root, coder_reply
    ):
        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=["I am afraid I cannot do that."],
            coder_replies=[coder_reply],
        )
        assert state.get("plan") is None
        assert state["outcome"] is RunOutcome.FAILED
        assert doubles["coder"].call_count == 0
        assert doubles["sandbox"].calls == []
        assert "planner_failure" in state["metadata"]
        # `metadata` is last-write-wins; a degraded node must not wipe the run's seed.
        assert state["metadata"]["seed"] == 42

    def test_a_coder_that_returns_no_code_diverts_to_the_finalizer(
        self, runs_root, plan_reply
    ):
        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=["I would rather not write that."],
        )
        assert state.get("current_revision") is None
        assert state["outcome"] is RunOutcome.FAILED
        assert doubles["sandbox"].calls == []

    def test_a_cancelled_run_terminates_with_a_deliverable(
        self, runs_root, plan_reply, coder_reply
    ):
        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            initial={"cancel_requested": True},
        )
        assert state["outcome"] is RunOutcome.CANCELLED
        assert doubles["sandbox"].calls == []
        assert (runs_root / RUN_ID / "final" / "SUMMARY.md").is_file()


class TestRepairLadders:
    def test_the_planner_is_re_prompted_once_with_its_semantic_errors(
        self, runs_root, plan_reply, coder_reply
    ):
        """A criterion outside the metric vocabulary is a specific, fixable complaint."""
        broken = json.loads(json.dumps(PLAN_JSON))
        broken["success_criteria"][0]["metric"] = "vibes"
        broken["primary_metric"] = "vibes"
        first = f"```json\n{json.dumps(broken)}\n```"

        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[first, plan_reply],
            coder_replies=[coder_reply],
        )

        assert doubles["planner"].call_count == 2
        assert state["plan"].primary_metric == "accuracy"
        repair_prompt = doubles["planner"].calls[1][-1].content
        assert "vibes" in repair_prompt

    def test_the_coder_is_re_prompted_when_static_validation_rejects_its_first_attempt(
        self, runs_root, plan_reply
    ):
        """A hallucinated download is caught before a container is ever launched."""
        rejected = (
            "```python\nimport requests\nresponse = requests.get('https://data')\n```"
        )
        accepted = f"```python\n{VALID_PROGRAM}```\n```json\n{{}}\n```"

        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[rejected, accepted],
        )

        assert doubles["coder"].call_count == 2
        assert "requests" not in state["current_revision"].content
        assert state["outcome"] is RunOutcome.SUCCEEDED
        assert "no network" in doubles["coder"].calls[1][-1].content

    def test_byte_identical_code_is_refused_before_it_is_re_run(
        self, runs_root, plan_reply
    ):
        """Re-running unchanged code cannot produce a different result."""
        from app.engine.nodes.coder import _rejection_reason
        from app.engine.state import CodeRevision
        from app.services.sandbox import sha256_text

        previous = CodeRevision(
            revision=1, content=VALID_PROGRAM, sha256=sha256_text(VALID_PROGRAM)
        )
        reason = _rejection_reason(VALID_PROGRAM, previous, "train")
        assert reason is not None
        assert "byte-identical" in reason


class TestBudgetGuards:
    def test_an_exhausted_node_budget_routes_straight_to_the_terminal_node(
        self, runs_root, plan_reply, coder_reply
    ):
        """Step 4 of the termination proof: when Φ reaches 0, the next hop is terminal."""
        from app.engine.state import Budgets

        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            initial={"budgets": Budgets(max_node_visits=2)},
        )
        assert doubles["coder"].call_count == 0
        assert state["outcome"] is RunOutcome.FAILED
        assert state["plan"] is not None  # planning happened; execution was cut off


class TestCorrectnessLoop:
    """Loop 1: `sandbox_exec → debugger → coder` (AGENTS.md §6.1).

    The scenario the whole phase exists for — a run that fails, diagnoses itself, fixes
    itself, and says what happened.
    """

    @pytest.fixture
    def result(self, runs_root, plan_reply, coder_reply):
        return invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply, variant("read the diagnosis column")],
            driver=FakeSandboxDriver(
                runs_root,
                script=[
                    {"exit_code": 1, "metrics": None, "stderr": KEY_ERROR_STDERR},
                    {"metrics": CLEAN_METRICS},
                ],
            ),
        )

    def test_a_broken_first_attempt_is_fixed_and_the_run_succeeds(self, result):
        state, _ = result
        assert state["outcome"] is RunOutcome.SUCCEEDED
        assert state["debug_iterations"] == 1
        assert len(state["code_revisions"]) == 2
        assert state["last_outcome"].classification == "CLEAN"

    def test_the_second_revision_differs_from_the_first(self, result):
        """Re-running identical bytes cannot produce a different result."""
        state, _ = result
        first, second = state["code_revisions"]
        assert first.sha256 != second.sha256
        assert second.addresses_error == "KeyError:target"

    def test_the_debugger_saw_the_traceback_and_the_environment_hint(self, result):
        _state, doubles = result
        system = doubles["debugger"].calls[0][0].content
        assert "KeyError: 'target'" in system
        assert "sandbox_stderr" in system  # fenced as untrusted, not as instruction
        assert "no network" in system  # the deterministic DATA hint
        assert "Debug iteration: 1 of 4" in system

    def test_the_coder_was_given_the_diagnosis_not_a_blank_slate(self, result):
        _state, doubles = result
        system = doubles["coder"].calls[1][0].content
        assert "You are fixing a specific failure — this is revision 2" in system
        assert "names the label column `diagnosis`" in system
        assert "Replace `df['target']`" in system
        assert "### Previous code" in system

    def test_the_diagnosis_is_recorded_against_the_real_fingerprint(self, result):
        state, _ = result
        diagnosis = state["last_diagnosis"]
        assert diagnosis.error_fingerprint == "KeyError:target"
        assert diagnosis.requires_replan is False
        assert state["diagnoses"] == [diagnosis]

    def test_the_report_narrates_the_failure_and_the_fix(self, result, runs_root):
        report = (runs_root / RUN_ID / "final" / "REPORT.md").read_text()
        assert "## 4. What went wrong and how it was fixed" in report
        assert "the second read the right one" in report
        assert "SUCCEEDED" in report


class TestDebugBudget:
    """A run that cannot fix itself must stop, and say so (§5.4 rules 1–2)."""

    @pytest.fixture
    def result(self, runs_root, plan_reply):
        # Distinct code each time, so nothing is refused as byte-identical; distinct
        # diagnoses, so the run is thrashing rather than repeating one directive.
        return invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[variant(f"attempt {n}") for n in range(1, 9)],
            debugger_replies=[
                diagnosis_reply(root_cause=f"cause {n}", requires_replan=False)
                for n in range(1, 9)
            ],
            driver=FakeSandboxDriver(
                runs_root, exit_code=1, metrics=None, stderr=KEY_ERROR_STDERR
            ),
            initial={"budgets": Budgets(max_debug_iterations=4, max_replans=0)},
        )

    def test_the_loop_stops_at_the_debug_budget(self, result):
        state, _ = result
        assert state["debug_iterations"] == 4
        assert state["usage"].sandbox_executions == 5  # four fixes, five attempts
        assert state["outcome"] is RunOutcome.FAILED

    def test_every_attempt_is_recorded_for_the_report(self, result):
        """§12.1 scenario 4 — the report contains section 4 with all four attempts."""
        state, _ = result
        assert len(state["errors"]) == 5
        assert len(state["diagnoses"]) == 4

    def test_the_report_still_exists_and_states_the_failure(self, result, runs_root):
        report = (runs_root / RUN_ID / "final" / "REPORT.md").read_text()
        assert "**Status:** FAILED" in report
        assert "## 4. What went wrong and how it was fixed" in report

    def test_the_sandbox_budget_also_ends_the_loop(self, runs_root, plan_reply):
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[variant(f"attempt {n}") for n in range(1, 9)],
            driver=FakeSandboxDriver(
                runs_root, exit_code=1, metrics=None, stderr=KEY_ERROR_STDERR
            ),
            initial={
                "budgets": Budgets(
                    max_debug_iterations=99, max_sandbox_executions=2, max_replans=0
                )
            },
        )
        assert state["usage"].sandbox_executions == 2
        assert state["outcome"] is RunOutcome.FAILED


class TestStagnationGuard:
    """§5.5 rule 4 — three identical fingerprints mean the approach is wrong."""

    def test_three_identical_fingerprints_escalate_to_a_replan(
        self, runs_root, plan_reply
    ):
        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[variant(f"attempt {n}") for n in range(1, 9)],
            driver=FakeSandboxDriver(
                runs_root, exit_code=1, metrics=None, stderr=KEY_ERROR_STDERR
            ),
            initial={"budgets": Budgets(max_debug_iterations=6, max_replans=1)},
        )

        assert state["replan_count"] == 1
        assert state["plan"].revision == 2
        assert doubles["planner"].call_count == 2
        assert len({e.fingerprint for e in state["errors"]}) == 1

    def test_the_replanner_is_shown_the_failures_it_has_to_avoid(
        self, runs_root, plan_reply
    ):
        _state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[variant(f"attempt {n}") for n in range(1, 9)],
            driver=FakeSandboxDriver(
                runs_root, exit_code=1, metrics=None, stderr=KEY_ERROR_STDERR
            ),
            initial={"budgets": Budgets(max_debug_iterations=6, max_replans=1)},
        )
        replan_prompt = doubles["planner"].calls[1][0].content
        assert "KeyError:target" in replan_prompt

    def test_the_debugger_is_warned_before_the_router_escalates(
        self, runs_root, plan_reply
    ):
        """Warn on the second repeat, escalate on the third — the model gets a chance."""
        _state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[variant(f"attempt {n}") for n in range(1, 9)],
            driver=FakeSandboxDriver(
                runs_root, exit_code=1, metrics=None, stderr=KEY_ERROR_STDERR
            ),
            initial={"budgets": Budgets(max_debug_iterations=6, max_replans=1)},
        )
        assert "WARNING" not in doubles["debugger"].calls[0][0].content
        second = doubles["debugger"].calls[1][0].content
        assert "failure number 2 in a row" in second
        assert "requires_replan" in second

    def test_a_diagnosis_demanding_a_replan_escalates_immediately(
        self, runs_root, plan_reply, coder_reply
    ):
        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply, variant("after replan")],
            debugger_replies=[diagnosis_reply(requires_replan=True)],
            driver=FakeSandboxDriver(
                runs_root,
                script=[
                    {"exit_code": 1, "metrics": None, "stderr": KEY_ERROR_STDERR},
                    {"metrics": CLEAN_METRICS},
                ],
            ),
        )
        assert doubles["planner"].call_count == 2
        assert state["replan_count"] == 1
        # One failure only — the escalation came from the diagnosis, not from stagnation.
        assert len(state["errors"]) == 1
        assert state["outcome"] is RunOutcome.SUCCEEDED


class TestValidationRejectionLoop:
    def test_rejected_code_reaches_the_debugger_without_launching_a_container(
        self, runs_root, plan_reply
    ):
        """§10.9 — VALIDATION_REJECTED is a debug cycle at zero container cost."""
        forbidden = "```python\nimport socket\nsocket.socket()\n```\n```json\n{}\n```"
        state, doubles = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[forbidden, forbidden, variant("no sockets")],
            driver=FakeSandboxDriver(runs_root, metrics=CLEAN_METRICS),
        )

        rejected = state["outcomes"][0]
        assert rejected.classification == "VALIDATION_REJECTED"
        assert rejected.exit_code is None
        assert state["errors"][0].kind.value == "validation_rejected"
        assert state["outcome"] is RunOutcome.SUCCEEDED
        # The Debugger was told what the gate refused, not handed an import traceback.
        assert "no network" in doubles["debugger"].calls[0][0].content


class TestReporterFallback:
    def test_a_reporter_whose_model_fails_still_writes_the_full_report(
        self, runs_root, plan_reply, coder_reply
    ):
        """`SYNTHESISE_FALLBACK` — this node cannot fail (§6.5, §7.8)."""

        class BrokenModel:
            async def ainvoke(self, _messages, **_kwargs):
                raise RuntimeError("ollama is unreachable")

        planner = FakeChatModel([plan_reply])
        coder = FakeChatModel([coder_reply])
        graph = build_graph(InMemorySaver())
        config = run_config(
            RUN_ID,
            llm_clients={
                "planner": planner,
                "coder": coder,
                "reporter": BrokenModel(),
            },
            sandbox_driver=FakeSandboxDriver(runs_root, metrics=CLEAN_METRICS),
        )
        state = run(graph.ainvoke({"run_id": RUN_ID, "prompt": PROMPT}, config))

        report = state["report_markdown"]
        assert report is not None
        for number in range(1, 9):
            assert f"## {number}." in report
        assert "0.9737" in report
        assert "reporter_failure" in state["metadata"]
        assert state["outcome"] is RunOutcome.SUCCEEDED

    def test_a_reporter_that_skips_sections_has_them_filled_from_the_template(
        self, runs_root, plan_reply, coder_reply
    ):
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            reporter_replies=[
                "## 1. Objective\nTrain a logistic-regression classifier on the "
                "Wisconsin breast cancer dataset and measure held-out accuracy.\n"
            ],
        )
        report = state["report_markdown"]
        assert "Wisconsin breast cancer dataset" in report
        assert "## 4. What went wrong and how it was fixed" in report
        assert "No execution failures occurred" in report

    def test_the_criteria_table_comes_from_state_not_from_the_model(
        self, runs_root, plan_reply, coder_reply
    ):
        """A model that invents numbers must not be able to publish them."""
        state, _ = invoke(
            runs_root=runs_root,
            plan_replies=[plan_reply],
            coder_replies=[coder_reply],
            reporter_replies=[
                "## 2. Result\nWe hit the target comfortably.\n\n"
                "| Criterion | Target | Achieved | Status |\n"
                "|---|---|---|---|\n"
                "| accuracy | >= 0.95 | 0.9999 | Pass |\n"
            ],
        )
        report = state["report_markdown"]
        assert "0.9999" not in report
        assert "0.9737" in report
        assert "We hit the target comfortably." in report
