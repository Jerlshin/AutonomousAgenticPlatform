"""The Researcher node in isolation (AGENTS.md §7.2).

Two properties are worth testing here that the graph tests cannot isolate cheaply: that
retrieval is genuinely deterministic (the node calls the store itself, between two model
calls, never leaving "whether to search" to the model), and that a signature the model
claims but the corpus never actually contained is dropped rather than trusted — the whole
reason this node exists.
"""

from __future__ import annotations

from app.engine.nodes.researcher import degraded_context_pack, researcher_node
from app.engine.state import (
    DatasetBinding,
    Diagnosis,
    Plan,
    PlanStep,
    StepKind,
    SuccessCriterion,
)
from tests.conftest import researcher_extract_reply, researcher_query_reply
from tests.fakes import DEFAULT_CHUNK, FakeChatModel, FakeVectorStore, run


def plan(**overrides) -> Plan:
    base = dict(
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
    base.update(overrides)
    return Plan(**base)


def base_state(**overrides) -> dict:
    state = {
        "run_id": "r1",
        "task_kind": "tabular-classification",
        "plan": plan(),
        "current_step_id": "s1",
    }
    state.update(overrides)
    return state


def research(
    state: dict,
    *,
    replies: list[str] | None = None,
    store: FakeVectorStore | None = None,
) -> tuple[dict, FakeChatModel, FakeVectorStore]:
    llm = FakeChatModel(
        replies or [researcher_query_reply(), researcher_extract_reply()]
    )
    vector_store = store or FakeVectorStore()
    config = {
        "configurable": {
            "llm_clients": {"researcher": llm},
            "vector_store": vector_store,
        }
    }
    update = run(researcher_node(state, config))
    return update, llm, vector_store


class TestQueryPlanning:
    def test_the_step_and_task_kind_reach_the_query_planning_prompt(self):
        _update, llm, _store = research(base_state())
        system = llm.calls[0][0].content
        assert "Train a classifier" in system
        assert "tabular-classification" in system

    def test_gaps_from_a_previous_round_are_carried_into_the_next_query(self):
        from app.engine.state import ContextPack

        previous = ContextPack(
            sufficiency="insufficient", gaps=["GridSearchCV cv parameter"]
        )
        _update, llm, _store = research(base_state(context_history=[previous]))
        system = llm.calls[0][0].content
        assert "GridSearchCV cv parameter" in system

    def test_a_debug_triggered_round_asks_about_the_diagnosis_not_the_step(self):
        diagnosis = Diagnosis(
            error_fingerprint="TypeError:unexpected-keyword",
            root_cause="Unsure whether GridSearchCV accepts `scoring` as a string.",
            fix_strategy="Confirm the accepted values for `scoring`.",
            targeted_changes=[],
            confidence=0.3,
            requires_research=True,
        )
        _update, llm, _store = research(base_state(last_diagnosis=diagnosis))
        system = llm.calls[0][0].content
        assert "GridSearchCV accepts" in system

    def test_a_blank_query_plan_falls_back_to_the_topic_rather_than_searching_nothing(
        self,
    ):
        """`_QueryPlan.queries` only requires a non-empty list, not non-blank entries —
        a model returning `["   "]` still validates, and `_plan_queries` has to notice
        the blank itself before it becomes a search for nothing."""
        blank_queries = researcher_query_reply(queries=["   "])
        _update, _llm, store = research(
            base_state(), replies=[blank_queries, researcher_extract_reply()]
        )
        assert store.searches  # something was searched, not nothing


class TestRetrieval:
    def test_the_query_plan_is_actually_searched_against_the_store(self):
        _update, _llm, store = research(base_state())
        assert (
            "rd_corpus",
            "LogisticRegression pipeline StandardScaler",
        ) in store.searches

    def test_implement_and_train_steps_also_search_code_exemplars(self):
        _update, _llm, store = research(base_state())
        assert any(kind == "code_exemplars" for kind, _query in store.searches)

    def test_a_research_kind_step_does_not_search_code_exemplars(self):
        research_step = PlanStep(
            id="s1",
            index=0,
            title="Find the API",
            description="…",
            kind=StepKind.RESEARCH,
        )
        state = base_state(plan=plan(steps=[research_step]))
        _update, _llm, store = research(state)
        assert not any(kind == "code_exemplars" for kind, _query in store.searches)

    def test_the_context_pack_carries_the_retrieved_chunks(self):
        update, _llm, _store = research(base_state())
        pack = update["context_pack"]
        assert pack.chunks[0].point_id == DEFAULT_CHUNK["point_id"]
        assert pack.chunks[0].text == DEFAULT_CHUNK["text"]

    def test_a_failing_search_degrades_to_zero_chunks_not_a_crash(self):
        class BrokenStore(FakeVectorStore):
            async def search_rd_corpus(self, *_args, **_kwargs):
                raise ConnectionError("qdrant is down")

        update, _llm, _store = research(base_state(), store=BrokenStore())
        assert update["context_pack"].chunks == []
        assert update["context_pack"].sufficiency == "insufficient"


class TestExtractionVerification:
    def test_a_signature_actually_in_the_retrieved_text_is_kept(self):
        reply = researcher_extract_reply(
            api_signatures=["Pipeline, StandardScaler and LogisticRegression basics."]
        )
        update, _llm, _store = research(
            base_state(), replies=[researcher_query_reply(), reply]
        )
        assert update["context_pack"].api_signatures == [
            "Pipeline, StandardScaler and LogisticRegression basics."
        ]

    def test_a_signature_not_in_the_retrieved_text_is_dropped(self):
        """The absolute rule (§7.2): a claimed signature the corpus never contained must
        never reach the Coder, however plausible it looks."""
        reply = researcher_extract_reply(
            api_signatures=["GridSearchCV(estimator, param_grid, cv=5, scoring='f1')"],
            sufficiency="sufficient",
        )
        update, _llm, _store = research(
            base_state(), replies=[researcher_query_reply(), reply]
        )
        pack = update["context_pack"]
        assert pack.api_signatures == []
        assert any("GridSearchCV" in gap for gap in pack.gaps)
        # A dropped signature is decisive evidence the round was not actually complete.
        assert pack.sufficiency == "partial"

    def test_a_citation_pointing_at_an_unretrieved_point_id_is_dropped(self):
        reply = researcher_extract_reply(
            key_facts=["Pipelines chain a scaler and an estimator."],
            citations={"0": ["some-point-that-was-never-retrieved"]},
        )
        update, _llm, _store = research(
            base_state(), replies=[researcher_query_reply(), reply]
        )
        assert update["context_pack"].citations == {}

    def test_a_valid_citation_survives(self):
        reply = researcher_extract_reply(
            key_facts=["Pipelines chain a scaler and an estimator."],
            citations={"0": [DEFAULT_CHUNK["point_id"]]},
        )
        update, _llm, _store = research(
            base_state(), replies=[researcher_query_reply(), reply]
        )
        assert update["context_pack"].citations == {"0": [DEFAULT_CHUNK["point_id"]]}

    def test_zero_chunks_can_never_be_reported_sufficient(self):
        """No model, however confident, gets to claim sufficiency over no evidence."""
        empty_store = FakeVectorStore(rd_corpus_hits=[])
        reply = researcher_extract_reply(sufficiency="sufficient")
        update, _llm, _store = research(
            base_state(), replies=[researcher_query_reply(), reply], store=empty_store
        )
        assert update["context_pack"].sufficiency == "insufficient"


class TestDegradation:
    def test_an_unreachable_model_yields_an_honestly_insufficient_pack(self):
        update = degraded_context_pack({}, RuntimeError("ollama is down"))
        pack = update["context_pack"]
        assert pack.sufficiency == "insufficient"
        assert pack.chunks == []
        assert pack.api_signatures == []

    def test_the_degraded_pack_is_appended_to_context_history(self):
        update = degraded_context_pack({})
        assert update["context_history"] is update["context_pack"]

    def test_a_model_that_never_produces_valid_json_still_produces_a_pack(self):
        update, _llm, _store = research(
            base_state(), replies=["I cannot help with that."]
        )
        assert update["context_pack"].sufficiency == "insufficient"


__all__: list[str] = []
