"""Prometheus instrumentation (ARCHITECTURE.md §12.1).

Two properties are worth testing here and the rest is bookkeeping.

**The metric surface matches the specification.** §12.1 is a table of metric names, types
and labels, and a dashboard panel referencing a metric that was renamed produces an empty
graph rather than an error — the failure is silent and shows up at the worst moment. So
the table is transcribed here and checked against what the registry actually exposes,
which also means the provisioned dashboards can be checked against the same list.

**Instrumentation cannot fail a request.** Every recording helper swallows its own errors.
A label that turns out to be `None`, a registry in a strange state, a metric renamed under
a call site — none of these may propagate into a run.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest

from app.core import metrics

# §12.1's tables, transcribed: metric name → the label set it must carry.
SPECIFIED_METRICS: dict[str, set[str]] = {
    # Platform
    "pluton_runs_total": {"status", "task_kind"},
    "pluton_run_duration_seconds": {"status", "task_kind"},
    "pluton_node_duration_seconds": {"node", "outcome"},
    "pluton_node_visits_total": {"node"},
    "pluton_debug_iterations": {"task_kind"},
    "pluton_replans_total": {"task_kind", "reason"},
    "pluton_criteria_satisfaction_ratio": {"task_kind"},
    "pluton_active_runs": {"worker_id"},
    "pluton_queue_depth": set(),
    "pluton_structured_output_attempts": {"role", "stage"},
    # LLM
    "pluton_llm_requests_total": {"model", "role", "outcome"},
    "pluton_llm_ttft_seconds": {"model", "role"},
    "pluton_llm_tokens_total": {"model", "role", "direction"},
    "pluton_llm_tokens_per_second": {"model"},
    "pluton_llm_cache_hits_total": {"kind"},
    # Sandbox
    "pluton_sandbox_executions_total": {"profile", "classification"},
    "pluton_sandbox_duration_seconds": {"profile"},
    "pluton_sandbox_max_rss_bytes": {"profile"},
    "pluton_sandbox_timeouts_total": {"profile"},
    "pluton_sandbox_oom_total": {"profile"},
    "pluton_sandbox_validation_rejections_total": {"reason"},
    # Retrieval
    "pluton_retrieval_latency_seconds": {"collection"},
    "pluton_retrieval_hits": {"collection"},
    "pluton_retrieval_top_score": {"collection"},
    "pluton_run_memory_hits_total": {"outcome"},
    # API and WebSocket
    "pluton_http_requests_total": {"method", "route", "status"},
    "pluton_http_request_duration_seconds": {"method", "route"},
    "pluton_ws_connections_active": set(),
    "pluton_ws_events_sent_total": {"type"},
    "pluton_ws_replay_gaps_total": set(),
}


def collectors() -> dict[str, Any]:
    """Every metric this module defines, keyed by its exposition name."""
    found = {}
    for name in SPECIFIED_METRICS:
        for attr in vars(metrics).values():
            base = getattr(attr, "_name", None)
            if base and name in (base, f"{base}_total"):
                found[name] = attr
    return found


# ------------------------------------------------------------------------------------
#  The specified surface
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(SPECIFIED_METRICS))
def test_every_specified_metric_is_defined(name: str) -> None:
    """§12.1's table, name by name."""
    assert name in collectors(), f"{name} is specified in §12.1 but not defined"


@pytest.mark.parametrize("name, labels", sorted(SPECIFIED_METRICS.items()))
def test_every_specified_metric_carries_its_labels(name: str, labels: set[str]) -> None:
    collector = collectors()[name]
    assert set(collector._labelnames) == labels


def test_the_scrape_body_parses_and_names_the_platform() -> None:
    payload, content_type = metrics.render()

    assert content_type.startswith("text/plain")
    text = payload.decode()
    assert "# HELP pluton_runs_total" in text
    assert "# TYPE pluton_runs_total counter" in text


def test_every_metric_carries_a_help_string() -> None:
    """An unlabelled metric is one nobody can interpret from the Prometheus UI."""
    text = metrics.render()[0].decode()
    documented = set(re.findall(r"^# HELP (pluton_\w+) (.+)$", text, re.MULTILINE))
    for name, help_text in documented:
        assert help_text.strip(), name


# ------------------------------------------------------------------------------------
#  Label vocabularies
# ------------------------------------------------------------------------------------


def test_task_kind_is_clamped_to_the_planner_taxonomy() -> None:
    """A model-produced label must not be able to grow the series count without bound."""
    assert metrics.task_kind_label("tabular-classification") == "tabular-classification"
    assert metrics.task_kind_label("tabular classification (binary)") == "other"
    assert metrics.task_kind_label(None) == "other"
    assert metrics.task_kind_label("") == "other"
    assert metrics.task_kind_label(12345) == "other"


def test_node_labels_are_clamped_to_the_graph() -> None:
    assert metrics.node_label("coder") == "coder"
    assert metrics.node_label("sandbox_exec") == "sandbox_exec"
    assert metrics.node_label("something_invented") == "other"
    assert metrics.node_label(None) == "other"


@pytest.mark.parametrize(
    "message, reason",
    [
        (
            "line 3: `import requests` — the sandbox has no network. Nothing…",
            "network_import",
        ),
        (
            "line 9: `import ctypes` is not permitted in the sandbox.",
            "forbidden_import",
        ),
        (
            "line 1: module `polars` is not installed in the sandbox and cannot…",
            "not_allowlisted",
        ),
        (
            "line 2: relative import — the sandbox runs a single file…",
            "relative_import",
        ),
        (
            "line 4: `eval()` on a non-literal argument is not permitted.",
            "dynamic_eval",
        ),
        ("line 5: `input()` — stdin is closed; the program runs unattended.", "stdin"),
        (
            "line 6: opening '/datasets/x' for writing — /datasets is mounted read-only.",
            "datasets_write",
        ),
        (
            "line 7: opening '/etc/x' for writing — the rootfs is read-only.",
            "readonly_write",
        ),
        ("main.py:12: SyntaxError: invalid syntax", "syntax_error"),
        (
            "the program never references /artifacts/metrics.json. Every training…",
            "no_metrics_contract",
        ),
        ("something nobody anticipated", "other"),
    ],
)
def test_validation_rejections_collapse_to_a_bounded_reason(
    message: str, reason: str
) -> None:
    """The validator's messages are prose for the Coder; the label is the rule that fired."""
    assert metrics.classify_rejection(message) == reason


def test_rejection_classification_never_returns_the_message() -> None:
    """The guard that keeps a line number or a module name out of a label value."""
    for message in ("line 999: `import ftplib` — the sandbox has no network.", "", "?"):
        assert metrics.classify_rejection(message) in {
            "network_import",
            "forbidden_import",
            "not_allowlisted",
            "relative_import",
            "os_attribute",
            "dynamic_import",
            "dynamic_eval",
            "stdin",
            "readonly_write",
            "datasets_write",
            "no_metrics_contract",
            "source_too_large",
            "syntax_error",
            "other",
        }


# ------------------------------------------------------------------------------------
#  Recording
# ------------------------------------------------------------------------------------


def sample(name: str, **labels: str) -> float:
    """One sample's current value, or 0.0 if the series does not exist yet."""
    from prometheus_client import REGISTRY

    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else value


def test_record_run_moves_both_the_counter_and_the_histogram() -> None:
    before = sample("pluton_runs_total", status="SUCCEEDED", task_kind="clustering")
    metrics.record_run("SUCCEEDED", "clustering", 12.5)

    assert (
        sample("pluton_runs_total", status="SUCCEEDED", task_kind="clustering")
        == before + 1
    )
    assert sample(
        "pluton_run_duration_seconds_sum", status="SUCCEEDED", task_kind="clustering"
    ) == pytest.approx(12.5)


def test_a_run_with_no_duration_still_counts() -> None:
    """A run that never reached `init` has no clock; it is still a terminal outcome."""
    before = sample("pluton_runs_total", status="FAILED", task_kind="analysis")
    metrics.record_run("FAILED", "analysis", None)

    assert (
        sample("pluton_runs_total", status="FAILED", task_kind="analysis") == before + 1
    )
    assert (
        sample(
            "pluton_run_duration_seconds_count", status="FAILED", task_kind="analysis"
        )
        == 0.0
    )


def test_observe_node_records_latency_and_the_visit_together() -> None:
    """One call, because a node visit and its duration are the same event (§6.4)."""
    before = sample("pluton_node_visits_total", node="debugger")
    metrics.observe_node("debugger", "ok", 0.75)

    assert sample("pluton_node_visits_total", node="debugger") == before + 1
    assert (
        sample("pluton_node_duration_seconds_count", node="debugger", outcome="ok") >= 1
    )


def test_negative_durations_are_clamped_rather_than_rejected() -> None:
    """A clock that went backwards must not poison a histogram or raise into a node."""
    metrics.observe_node("coder", "ok", -5.0)
    assert sample("pluton_node_duration_seconds_count", node="coder", outcome="ok") >= 1


def test_llm_metrics_skip_ttft_on_the_non_streaming_path() -> None:
    """Recording a full round trip as a time-to-first-token would corrupt the panel."""
    before = sample("pluton_llm_ttft_seconds_count", model="m1", role="coder")
    metrics.record_llm_call(
        model="m1",
        role="coder",
        outcome="ok",
        duration_s=2.0,
        tokens_in=10,
        tokens_out=20,
    )

    assert sample("pluton_llm_ttft_seconds_count", model="m1", role="coder") == before
    assert (
        sample("pluton_llm_tokens_total", model="m1", role="coder", direction="out")
        == 20
    )
    # 20 tokens in 2 s.
    assert sample("pluton_llm_tokens_per_second_sum", model="m1") == pytest.approx(10.0)


def test_llm_metrics_record_ttft_when_the_stream_provided_one() -> None:
    metrics.record_llm_call(
        model="m2",
        role="planner",
        outcome="ok",
        duration_s=3.0,
        tokens_out=30,
        ttft_s=0.4,
    )
    assert sample("pluton_llm_ttft_seconds_count", model="m2", role="planner") == 1


def test_a_failed_llm_call_is_counted_without_a_throughput_sample() -> None:
    metrics.record_llm_call(model="m3", role="coder", outcome="error", duration_s=1.0)

    assert (
        sample("pluton_llm_requests_total", model="m3", role="coder", outcome="error")
        == 1
    )
    assert sample("pluton_llm_tokens_per_second_count", model="m3") == 0.0


def test_sandbox_timeouts_and_ooms_get_their_own_counters() -> None:
    """An alert expression should not have to know that TIMEOUT is a classification value."""
    metrics.record_sandbox_execution(
        profile="train",
        classification="OOM",
        duration_s=9.0,
        max_rss_bytes=5 * 2**30,
        oom_killed=True,
    )
    assert sample("pluton_sandbox_oom_total", profile="train") == 1
    assert (
        sample("pluton_sandbox_executions_total", profile="train", classification="OOM")
        == 1
    )
    assert sample("pluton_sandbox_max_rss_bytes_count", profile="train") == 1


def test_a_query_that_returned_nothing_still_observes_a_top_score() -> None:
    """Skipping empty queries would make the score distribution describe only the good ones."""
    metrics.observe_retrieval("rd_corpus", duration_s=0.02, scores=[])

    assert sample("pluton_retrieval_top_score_count", collection="rd_corpus") == 1
    assert sample("pluton_retrieval_hits_sum", collection="rd_corpus") == 0.0


def test_run_memory_records_hit_or_miss() -> None:
    metrics.record_run_memory_lookup(2)
    metrics.record_run_memory_lookup(0)

    assert sample("pluton_run_memory_hits_total", outcome="hit") == 1
    assert sample("pluton_run_memory_hits_total", outcome="miss") == 1


def test_cache_lookups_count_hits_only() -> None:
    """§12.1 specifies a hit counter; a ratio would need a miss counter it does not define."""
    metrics.record_cache_lookup("embed", hit=True)
    metrics.record_cache_lookup("embed", hit=False)

    assert sample("pluton_llm_cache_hits_total", kind="embed") == 1


def test_the_active_runs_gauge_comes_back_down_on_an_exception() -> None:
    """A gauge that leaks on the failure path reads as permanent saturation."""
    before = sample("pluton_active_runs", worker_id="w-1")

    with pytest.raises(RuntimeError):
        with metrics.run_in_flight("w-1"):
            assert sample("pluton_active_runs", worker_id="w-1") == before + 1
            raise RuntimeError("the graph blew up")

    assert sample("pluton_active_runs", worker_id="w-1") == before


def test_the_ws_connection_gauge_comes_back_down_on_a_disconnect() -> None:
    before = sample("pluton_ws_connections_active")

    with pytest.raises(ConnectionResetError):
        with metrics.track_ws_connection():
            raise ConnectionResetError

    assert sample("pluton_ws_connections_active") == before


# ------------------------------------------------------------------------------------
#  Instrumentation is never load-bearing
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        lambda: metrics.record_run(None, object(), "not-a-number"),
        lambda: metrics.observe_node(None, None, None),
        lambda: metrics.observe_debug_iterations("x", "not-an-int"),
        lambda: metrics.observe_criteria_satisfaction("x", "not-a-float"),
        lambda: metrics.record_replan(None, None),
        lambda: metrics.record_llm_call(
            model=None, role=None, outcome=None, duration_s="x", tokens_out="y"
        ),
        lambda: metrics.record_sandbox_execution(profile=None, classification=None),
        lambda: metrics.record_validation_rejection(None),
        lambda: metrics.observe_retrieval(None, duration_s=None, scores=None),
        lambda: metrics.record_http_request(None, None, None, None),
        lambda: metrics.record_ws_event(None),
        lambda: metrics.set_queue_depth("nonsense"),
    ],
)
def test_a_broken_instrumentation_call_never_raises(call) -> None:  # noqa: ANN001
    """A monitoring defect must not be able to fail a run (see the module docstring)."""
    call()


def test_criteria_satisfaction_is_clamped_to_the_unit_interval() -> None:
    metrics.observe_criteria_satisfaction("analysis", 1.4)
    metrics.observe_criteria_satisfaction("analysis", -0.2)
    assert sample("pluton_criteria_satisfaction_ratio_count", task_kind="analysis") == 2


def test_redefining_a_metric_returns_the_registered_collector() -> None:
    """What makes a module reload (or `pytest --cov`) a no-op rather than an import error."""
    from prometheus_client import Counter

    again = metrics._metric(
        Counter, "pluton_runs_total", "Terminal run outcomes.", ("status", "task_kind")
    )
    assert again is metrics.RUNS_TOTAL


# ------------------------------------------------------------------------------------
#  Queue depth
# ------------------------------------------------------------------------------------


class _Queue:
    def __init__(self, depth: int | Exception) -> None:
        self.depth = depth

    async def zcard(self, _name: str) -> int:
        if isinstance(self.depth, Exception):
            raise self.depth
        return self.depth


def test_queue_depth_is_sampled_from_the_arq_sorted_set() -> None:
    from tests.fakes import run

    assert run(metrics.sample_queue_depth(_Queue(7))) == 7
    assert sample("pluton_queue_depth") == 7


def test_an_unreachable_redis_leaves_the_last_known_depth() -> None:
    """A Redis blip is not a worker failure, and zeroing the gauge would look like relief."""
    from tests.fakes import run

    run(metrics.sample_queue_depth(_Queue(4)))
    assert run(metrics.sample_queue_depth(_Queue(ConnectionError("down")))) is None
    assert sample("pluton_queue_depth") == 4


# ------------------------------------------------------------------------------------
#  Dashboards
# ------------------------------------------------------------------------------------

DASHBOARD_DIR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "infrastructure"
    / "observability"
    / "grafana"
    / "dashboards"
)


def dashboard_files() -> list[pathlib.Path]:
    return sorted(DASHBOARD_DIR.glob("*.json"))


def test_the_five_specified_dashboards_are_provisioned() -> None:
    """§12.2's table. A dashboard that is not on disk is not provisioned."""
    assert {path.stem for path in dashboard_files()} == {
        "run-pipeline",
        "llm-performance",
        "sandbox-health",
        "retrieval-quality",
        "system",
    }


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.stem)
def test_a_dashboard_is_valid_json_with_a_stable_uid(path: pathlib.Path) -> None:
    board = json.loads(path.read_text(encoding="utf-8"))

    assert board["uid"].startswith("pluton-")
    assert board["title"]
    assert board["panels"]
    ids = [panel["id"] for panel in board["panels"]]
    assert len(ids) == len(set(ids)), "duplicate panel ids break Grafana's panel links"


@pytest.mark.parametrize("path", dashboard_files(), ids=lambda p: p.stem)
def test_every_panel_query_names_a_metric_that_exists(path: pathlib.Path) -> None:
    """The silent failure this whole file exists to prevent.

    A panel referencing a renamed metric renders an empty graph — no error, no warning,
    just a flat line that reads as "nothing is happening". Only `pluton_` names are
    checked: the rest come from node-exporter, cAdvisor and the datastore exporters, which
    are not this repository's to guarantee.
    """
    board = json.loads(path.read_text(encoding="utf-8"))
    known = set(SPECIFIED_METRICS)

    for panel in board["panels"]:
        for target in panel.get("targets", []):
            for referenced in re.findall(r"pluton_[a-z0-9_]+", target["expr"]):
                # Histograms and counters expose derived series; map them back to the base.
                base = re.sub(r"_(bucket|sum|count|created)$", "", referenced)
                assert base in known or f"{base}_total" in known, (
                    f"{path.name} panel {panel['title']!r} references {referenced}, "
                    "which no metric in §12.1 exposes"
                )
