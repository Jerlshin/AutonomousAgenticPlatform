"""The Debugger node in isolation (AGENTS.md §7.5).

Two things are worth testing here that the graph tests cannot isolate: what the prompt is
built out of, and what happens to model output the platform has better information about.
The Debugger's job is to be *right about the environment*, and every fact it is right about
is one that was injected rather than generated.
"""

from __future__ import annotations

import uuid

from app.engine.nodes.debugger import (
    UNEVIDENCED_CONFIDENCE_CEILING,
    debugger_node,
    minimal_diagnosis_update,
    repeat_warning_block,
)
from app.engine.state import (
    Budgets,
    DatasetBinding,
    ErrorKind,
    ErrorRecord,
    Plan,
    PlanStep,
    SandboxOutcome,
    StepKind,
    SuccessCriterion,
    ValidationReport,
)
from tests.conftest import diagnosis_reply
from tests.fakes import FakeChatModel, run

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/workspace/main.py", line 12, in main\n'
    "    y = df['target']\n"
    "KeyError: 'target'\n"
)


def error(**overrides) -> ErrorRecord:
    base = {
        "kind": ErrorKind.DATA,
        "fingerprint": "KeyError:target",
        "exception_type": "KeyError",
        "message": "'target'",
        "traceback": TRACEBACK,
        "file": "/workspace/main.py",
        "line": 12,
        "offending_source": "11   df = pd.read_parquet(path)\n12 > y = df['target']",
        "revision": 1,
    }
    return ErrorRecord(**{**base, **overrides})


def outcome(**overrides) -> SandboxOutcome:
    base = {
        "execution_id": uuid.uuid4(),
        "profile": "train",
        "classification": "RUNTIME_ERROR",
        "exit_code": 1,
        "duration_ms": 4200,
        "stdout_tail": "loading /datasets/tabular/breast_cancer.parquet\n",
        "stderr_tail": TRACEBACK,
        "validation": ValidationReport(passed=True),
        "revision": 1,
    }
    return SandboxOutcome(**{**base, **overrides})


def plan() -> Plan:
    return Plan(
        steps=[
            PlanStep(
                id="s1",
                index=0,
                title="Train a classifier",
                description="Stratified split, logistic regression.",
                kind=StepKind.TRAIN,
                dataset=DatasetBinding(
                    dataset_id="sklearn.breast_cancer",
                    path="/datasets/tabular/breast_cancer.parquet",
                    sha256="a" * 64,
                    task_kind="tabular-classification",
                    target_column="diagnosis",
                ),
            )
        ],
        success_criteria=[
            SuccessCriterion(
                id="c1", metric="accuracy", comparator="gte", threshold=0.95
            )
        ],
        task_kind="tabular-classification",
        primary_metric="accuracy",
    )


def diagnose(
    state: dict, replies: list[str] | None = None
) -> tuple[dict, FakeChatModel]:
    llm = FakeChatModel(replies or [diagnosis_reply()])
    config = {"configurable": {"llm_clients": {"debugger": llm}}}
    return run(debugger_node(state, config)), llm


def base_state(**overrides) -> dict:
    state = {
        "run_id": "r1",
        "task_kind": "tabular-classification",
        "plan": plan(),
        "current_step_id": "s1",
        "last_error": error(),
        "errors": [error()],
        "last_outcome": outcome(),
        "budgets": Budgets(),
        "debug_iterations": 0,
    }
    state.update(overrides)
    return state


class TestPromptConstruction:
    def test_the_traceback_and_stdout_are_fenced_as_untrusted(self):
        """Program output is evidence, never instruction (principle P7)."""
        _update, llm = diagnose(base_state())
        system = llm.calls[0][0].content
        assert '<untrusted source="sandbox_stderr" trust="untrusted">' in system
        assert '<untrusted source="sandbox_stdout"' in system
        assert "KeyError: 'target'" in system

    def test_the_failing_source_region_is_shown(self):
        _update, llm = diagnose(base_state())
        assert "12 > y = df['target']" in llm.calls[0][0].content

    def test_the_environment_hint_names_the_bound_dataset(self):
        """A DATA failure is nearly always a wrong path or a wrong column name."""
        _update, llm = diagnose(base_state())
        system = llm.calls[0][0].content
        assert "sklearn.breast_cancer" in system
        assert "`diagnosis`" in system

    def test_the_iteration_budget_is_stated(self):
        _update, llm = diagnose(
            base_state(debug_iterations=2, budgets=Budgets(max_debug_iterations=4))
        )
        assert "Debug iteration: 3 of 4" in llm.calls[0][0].content

    def test_the_plan_is_included_so_the_fix_is_judged_against_the_goal(self):
        _update, llm = diagnose(base_state())
        system = llm.calls[0][0].content
        assert "Train a classifier" in system
        assert "accuracy gte 0.95" in system

    def test_no_stdout_is_said_rather_than_left_blank(self):
        _update, llm = diagnose(base_state(last_outcome=outcome(stdout_tail="")))
        assert "produced no output on stdout" in llm.calls[0][0].content


class TestRepeatWarning:
    def test_a_first_failure_carries_no_warning(self):
        assert repeat_warning_block({"errors": [error()]}) == ""

    def test_a_second_identical_failure_warns_the_previous_fix_did_not_work(self):
        block = repeat_warning_block({"errors": [error(), error(revision=2)]})
        assert "failure number 2 in a row" in block
        assert "KeyError:target" in block
        assert "requires_replan" in block

    def test_a_different_second_failure_is_progress_not_a_repeat(self):
        errors = [error(), error(fingerprint="ValueError:shape", revision=2)]
        assert repeat_warning_block({"errors": errors}) == ""

    def test_the_warning_reaches_the_prompt(self):
        _update, llm = diagnose(
            base_state(errors=[error(), error(revision=2)], debug_iterations=1)
        )
        assert "WARNING" in llm.calls[0][0].content


class TestOutputReconciliation:
    def test_the_fingerprint_is_the_platforms_not_the_models(self):
        """The model reproducing a fingerprint from memory is a coin flip; state is not."""
        update, _llm = diagnose(
            base_state(),
            [diagnosis_reply(error_fingerprint="TotallyWrong:invented")],
        )
        assert update["last_diagnosis"].error_fingerprint == "KeyError:target"

    def test_confidence_without_quoted_evidence_is_capped(self):
        update, _llm = diagnose(
            base_state(), [diagnosis_reply(evidence=[], confidence=0.95)]
        )
        assert update["last_diagnosis"].confidence == UNEVIDENCED_CONFIDENCE_CEILING

    def test_evidence_backed_confidence_is_left_alone(self):
        update, _llm = diagnose(base_state(), [diagnosis_reply(confidence=0.86)])
        assert update["last_diagnosis"].confidence == 0.86

    def test_an_empty_directive_falls_back_to_the_fix_strategy(self):
        """A directive with no instruction leaves the Coder to regenerate blind."""
        update, _llm = diagnose(
            base_state(),
            [diagnosis_reply(targeted_changes=["", "   "])],
        )
        changes = update["last_diagnosis"].targeted_changes
        assert changes == ["Read the label from the column the dataset actually has."]

    def test_the_iteration_counter_advances(self):
        update, _llm = diagnose(base_state(debug_iterations=2))
        assert update["debug_iterations"] == 3
        assert update["diagnoses"] is update["last_diagnosis"]


class TestPriorArt:
    def test_run_memory_hits_are_injected_before_the_call(self):
        """Episodic lookup is not a tool the model may decline to use (§7.5)."""
        recorded: dict = {}

        def search(**kwargs):
            recorded.update(kwargs)
            return ["Rename `target` to `diagnosis` — fixed in run 91c2."]

        llm = FakeChatModel([diagnosis_reply()])
        config = {
            "configurable": {
                "llm_clients": {"debugger": llm},
                "run_memory_search": search,
            }
        }
        update = run(debugger_node(base_state(), config))

        assert recorded["fingerprint"] == "KeyError:target"
        assert recorded["task_kind"] == "tabular-classification"
        assert "fixed in run 91c2" in llm.calls[0][0].content
        assert update["last_diagnosis"].prior_art == [
            "Rename `target` to `diagnosis` — fixed in run 91c2."
        ]

    def test_a_failing_lookup_does_not_cost_the_diagnosis(self):
        """Retrieval is an optimisation; the traceback alone is still enough."""

        def search(**_kwargs):
            raise ConnectionError("qdrant is down")

        llm = FakeChatModel([diagnosis_reply()])
        config = {
            "configurable": {
                "llm_clients": {"debugger": llm},
                "run_memory_search": search,
            }
        }
        update = run(debugger_node(base_state(), config))
        assert update["last_diagnosis"].root_cause
        assert update["last_diagnosis"].prior_art == []


class TestDegradation:
    def test_a_model_that_never_produces_valid_json_still_advances_the_loop(self):
        """§6.5 `DEGRADE` — the Coder gets a retry and the counter still moves."""
        update, _llm = diagnose(base_state(), ["I cannot help with that."])
        diagnosis = update["last_diagnosis"]
        assert diagnosis.confidence == 0.1
        assert diagnosis.error_fingerprint == "KeyError:target"
        assert "'target'" in diagnosis.targeted_changes[0]
        assert "main.py line 12" in diagnosis.targeted_changes[0]
        assert update["debug_iterations"] == 1

    def test_the_minimal_diagnosis_works_with_no_error_at_all(self):
        update = minimal_diagnosis_update({}, RuntimeError("model unreachable"))
        assert update["last_diagnosis"].error_fingerprint == "unknown"
        assert update["debug_iterations"] == 1

    def test_being_reached_with_no_error_degrades_rather_than_crashing_the_run(self):
        update, _llm = diagnose(base_state(last_error=None, errors=[]))
        assert update["last_diagnosis"].confidence == 0.1
