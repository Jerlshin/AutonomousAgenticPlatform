"""Plan validation beyond the schema, and the dataset registry it validates against.

AGENTS.md §7.1 lists the checks a schema cannot express. They exist because a plan that is
structurally valid and semantically wrong — a criterion on a metric nothing will compute, a
train step bound to a dataset that does not exist — fails four nodes later as a
`CONTRACT_VIOLATION`, having spent a container launch to discover it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import settings
from app.engine.nodes.planner import (
    _failure_history,
    _initial_step_status,
    validate_plan,
)
from app.engine.state import (
    DatasetBinding,
    ErrorKind,
    ErrorRecord,
    Plan,
    PlanStep,
    StepKind,
    StepStatus,
    SuccessCriterion,
)
from app.services.datasets import (
    binding_from_entry,
    find_dataset,
    list_datasets,
    load_manifest,
    manifest_for_prompt,
)

MANIFEST = {
    "schema_version": "1.0",
    "datasets": [
        {
            "id": "sklearn.breast_cancer",
            "task_kind": "tabular-classification",
            "path": "/datasets/tabular/breast_cancer.parquet",
            "sha256": "a" * 64,
            "n_samples": 569,
            "n_features": 30,
            "n_classes": 2,
            "target": "target",
            "license": "CC-BY-4.0",
        },
        {
            "id": "sklearn.california",
            "task_kind": "tabular-regression",
            "path": "/datasets/tabular/california_housing.parquet",
            "sha256": "b" * 64,
            "n_samples": 20640,
        },
    ],
}


@pytest.fixture
def seeded_registry(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "datasets"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps(MANIFEST), encoding="utf-8")
    monkeypatch.setattr(settings, "DATASETS_ROOT", str(root))
    return root


def make_plan(
    *,
    steps: list[PlanStep] | None = None,
    criteria: list[SuccessCriterion] | None = None,
    primary: str = "accuracy",
) -> Plan:
    return Plan(
        steps=steps
        or [
            _step("s1", 0, StepKind.RESEARCH),
            _step("s2", 1, StepKind.TRAIN, dataset=_binding()),
            _step("s3", 2, StepKind.REPORT),
        ],
        success_criteria=criteria
        or [
            SuccessCriterion(
                id="c1", metric="accuracy", comparator="gte", threshold=0.95
            )
        ],
        task_kind="tabular-classification",
        primary_metric=primary,
    )


class TestDatasetRegistry:
    def test_an_unseeded_registry_is_a_setup_state_not_a_crash(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings, "DATASETS_ROOT", str(tmp_path / "absent"))
        assert load_manifest()["datasets"] == []
        assert "NO DATASETS ARE REGISTERED" in manifest_for_prompt()

    def test_a_corrupt_manifest_degrades_rather_than_raising(
        self, tmp_path, monkeypatch
    ):
        root = tmp_path / "datasets"
        root.mkdir()
        (root / "manifest.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(settings, "DATASETS_ROOT", str(root))
        assert load_manifest()["datasets"] == []

    def test_entries_are_listed_and_filtered_by_task_kind(self, seeded_registry):
        assert len(list_datasets()) == 2
        assert [d["id"] for d in list_datasets("tabular-regression")] == [
            "sklearn.california"
        ]

    def test_lookup_by_id(self, seeded_registry):
        assert find_dataset("sklearn.breast_cancer")["n_samples"] == 569
        assert find_dataset("nope") is None

    def test_the_prompt_view_drops_fields_a_planner_does_not_plan_with(
        self, seeded_registry
    ):
        rendered = manifest_for_prompt()
        assert "sklearn.breast_cancer" in rendered
        assert "license" not in rendered

    def test_a_manifest_entry_becomes_a_step_binding(self, seeded_registry):
        binding = binding_from_entry(find_dataset("sklearn.breast_cancer"))
        assert binding.dataset_id == "sklearn.breast_cancer"
        assert binding.target_column == "target"
        assert binding.sha256 == "a" * 64


class TestPlanValidation:
    def test_a_well_formed_plan_has_no_problems(self, seeded_registry):
        assert validate_plan(make_plan()) == []

    def test_a_criterion_outside_the_vocabulary_is_rejected(self, seeded_registry):
        plan = make_plan(
            criteria=[
                SuccessCriterion(
                    id="c1", metric="vibes", comparator="gte", threshold=0.9
                )
            ],
            primary="vibes",
        )
        assert any(
            "not\nin the standard vocabulary".replace("\n", " ") in p
            for p in validate_plan(plan)
        )

    def test_a_primary_metric_no_criterion_measures_is_rejected(self, seeded_registry):
        problems = validate_plan(make_plan(primary="roc_auc"))
        assert any("primary_metric" in p for p in problems)

    def test_a_plan_with_no_required_criterion_is_rejected(self, seeded_registry):
        plan = make_plan(
            criteria=[
                SuccessCriterion(
                    id="c1",
                    metric="accuracy",
                    comparator="gte",
                    threshold=0.95,
                    required=False,
                )
            ]
        )
        assert any("required" in p for p in validate_plan(plan))

    def test_a_train_step_without_a_dataset_binding_is_rejected(self, seeded_registry):
        plan = make_plan(
            steps=[
                _step("s1", 0, StepKind.RESEARCH),
                _step("s2", 1, StepKind.TRAIN),
                _step("s3", 2, StepKind.REPORT),
            ]
        )
        assert any("no `dataset` binding" in p for p in validate_plan(plan))

    def test_a_mismatched_dataset_hash_is_rejected(self, seeded_registry):
        """The manifest is the authority; a hash the model invented means the wrong data."""
        plan = make_plan(
            steps=[
                _step("s1", 0, StepKind.RESEARCH),
                _step("s2", 1, StepKind.TRAIN, dataset=_binding(sha256="c" * 64)),
                _step("s3", 2, StepKind.REPORT),
            ]
        )
        assert any("sha256" in p for p in validate_plan(plan))

    def test_an_unseeded_registry_does_not_reject_every_plan(
        self, tmp_path, monkeypatch
    ):
        """Before `make seed-datasets`, bindings cannot be arbitrated — but work continues."""
        monkeypatch.setattr(settings, "DATASETS_ROOT", str(tmp_path / "absent"))
        assert validate_plan(make_plan()) == []

    def test_a_plan_outside_the_step_bounds_is_rejected(self, seeded_registry):
        plan = make_plan(steps=[_step("s1", 0, StepKind.TRAIN, dataset=_binding())])
        assert any("between 3 and 6" in p for p in validate_plan(plan))


class TestStepStatusSeeding:
    def test_steps_before_the_first_executable_one_are_marked_skipped(self):
        plan = make_plan()
        status = _initial_step_status(plan, plan.steps[1])
        assert status == {
            "s1": StepStatus.SKIPPED,
            "s2": StepStatus.PENDING,
            "s3": StepStatus.PENDING,
        }

    def test_nothing_is_skipped_when_there_is_nothing_executable(self):
        plan = make_plan()
        assert set(_initial_step_status(plan, None).values()) == {StepStatus.PENDING}


class TestFailureHistory:
    def test_the_first_plan_has_no_history(self):
        assert "first plan" in _failure_history({})

    def test_prior_failures_are_fenced_as_untrusted_evidence(self):
        history = _failure_history(
            {
                "plan_history": [make_plan()],
                "errors": [
                    ErrorRecord(
                        kind=ErrorKind.DATA,
                        fingerprint="KeyError:target",
                        message="'target'",
                        revision=1,
                    )
                ],
            }
        )
        assert "<untrusted" in history
        assert "KeyError:target" in history


def _step(step_id, index, kind, dataset=None) -> PlanStep:
    return PlanStep(
        id=step_id,
        index=index,
        title=f"step {step_id}",
        description="…",
        kind=kind,
        depends_on=[] if index == 0 else [f"s{index}"],
        dataset=dataset,
    )


def _binding(sha256: str = "a" * 64) -> DatasetBinding:
    return DatasetBinding(
        dataset_id="sklearn.breast_cancer",
        path="/datasets/tabular/breast_cancer.parquet",
        sha256=sha256,
        task_kind="tabular-classification",
        n_samples=569,
        target_column="target",
    )
