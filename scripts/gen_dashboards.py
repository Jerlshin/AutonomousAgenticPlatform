"""Generate the five Grafana dashboards specified in `docs/ARCHITECTURE.md` §12.2.

`make gen-dashboards` writes them; `make check-dashboards` (and CI) fails if the
checked-in JSON has drifted. Same contract as `.env.example` and the generated event
types, for the same reason: a Grafana dashboard is 500 lines of JSON per board, and
hand-editing five of them is how panels end up referencing metrics that no longer exist.

The helpers below are deliberately thin — they encode the panel scaffolding Grafana needs
(`fieldConfig`, `gridPos`, datasource refs) so the dashboard definitions at the bottom of
the file can be read as what they are: a list of questions and the PromQL that answers
each one. Panel *descriptions* are part of the deliverable, not decoration: a panel that
needs a paragraph to interpret should carry that paragraph, because the person reading it
at 2 a.m. did not write it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import pathlib
import sys

DS = {"type": "prometheus", "uid": "pluton-prometheus"}

# Resolved against the repository root rather than the CWD, so the script works from
# anywhere — CI runs it from the root, a developer usually does not.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = REPO_ROOT / "infrastructure" / "observability" / "grafana" / "dashboards"

_next_id = {"n": 0}


def _pid() -> int:
    _next_id["n"] += 1
    return _next_id["n"]


def target(
    expr: str, legend: str = "", *, instant: bool = False, fmt: str = "time_series"
):
    t = {
        "datasource": DS,
        "editorMode": "code",
        "expr": expr,
        "legendFormat": legend or "__auto",
        "range": not instant,
        "instant": instant,
        "refId": chr(65 + target.count % 26),
        "format": fmt,
    }
    target.count += 1
    return t


target.count = 0


def _grid(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def ts(
    title,
    targets,
    *,
    x,
    y,
    w=12,
    h=8,
    unit="short",
    desc="",
    stack=False,
    fill=8,
    min_=None,
    legend="list",
    decimals=None,
):
    """A timeseries panel."""
    custom = {
        "drawStyle": "line",
        "lineInterpolation": "smooth",
        "lineWidth": 2,
        "fillOpacity": fill,
        "gradientMode": "opacity",
        "showPoints": "never",
        "spanNulls": True,
        "axisSoftMin": 0 if min_ is None else min_,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
    }
    return {
        "id": _pid(),
        "type": "timeseries",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "custom": custom,
                "color": {"mode": "palette-classic"},
            },
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": legend,
                "placement": "bottom",
                "showLegend": True,
                "calcs": ["lastNotNull", "max"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "targets": targets,
    }


def heatmap(title, expr, *, x, y, w=12, h=8, unit="s", desc="", legend_unit=None):
    """A bucket heatmap: the honest rendering of a Prometheus histogram."""
    return {
        "id": _pid(),
        "type": "heatmap",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "targets": [target(expr, "{{le}}", fmt="heatmap")],
        "options": {
            # `calculate: false` — the buckets already exist. Letting Grafana re-bucket
            # them would smear the histogram it was handed.
            "calculate": False,
            "cellGap": 1,
            "color": {
                "mode": "scheme",
                "scheme": "Turbo",
                "steps": 64,
                "exponent": 0.5,
                "fill": "dark-orange",
                "reverse": False,
            },
            "yAxis": {"unit": legend_unit or unit, "axisPlacement": "left"},
            "legend": {"show": True},
            "tooltip": {"mode": "single", "yHistogram": True, "showColorScale": True},
            "exemplars": {"color": "rgba(255,0,255,0.7)"},
            "filterValues": {"le": 1e-9},
            "rowsFrame": {"layout": "auto"},
        },
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "hideFrom": {"tooltip": False, "viz": False, "legend": False}
                }
            },
            "overrides": [],
        },
    }


def stat(
    title,
    targets,
    *,
    x,
    y,
    w=6,
    h=4,
    unit="short",
    desc="",
    decimals=None,
    thresholds=None,
    graph="none",
    text_size=32,
):
    steps = thresholds or [{"color": "text", "value": None}]
    return {
        "id": _pid(),
        "type": "stat",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "mappings": [],
                "thresholds": {"mode": "absolute", "steps": steps},
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "value",
            "graphMode": graph,
            "justifyMode": "auto",
            "textMode": "auto",
            "text": {"valueSize": text_size},
        },
        "targets": targets,
    }


def gauge(
    title,
    targets,
    *,
    x,
    y,
    w=6,
    h=6,
    unit="percentunit",
    desc="",
    thresholds=None,
    minv=0,
    maxv=1,
):
    steps = thresholds or [
        {"color": "red", "value": None},
        {"color": "orange", "value": 0.6},
        {"color": "green", "value": 0.8},
    ]
    return {
        "id": _pid(),
        "type": "gauge",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "min": minv,
                "max": maxv,
                "decimals": 2,
                "thresholds": {"mode": "absolute", "steps": steps},
                "color": {"mode": "thresholds"},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "showThresholdLabels": False,
            "showThresholdMarkers": True,
        },
        "targets": targets,
    }


def bars(title, targets, *, x, y, w=12, h=8, unit="short", desc=""):
    return {
        "id": _pid(),
        "type": "barchart",
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": _grid(x, y, w, h),
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {
                    "lineWidth": 1,
                    "fillOpacity": 80,
                    "gradientMode": "hue",
                    "axisPlacement": "auto",
                    "thresholdsStyle": {"mode": "off"},
                },
                "color": {"mode": "palette-classic"},
            },
            "overrides": [],
        },
        "options": {
            "orientation": "horizontal",
            "showValue": "auto",
            "stacking": "none",
            "xTickLabelRotation": 0,
            "legend": {
                "displayMode": "list",
                "placement": "bottom",
                "showLegend": False,
            },
            "tooltip": {"mode": "single", "sort": "none"},
        },
        "targets": targets,
    }


def row(title, *, y):
    return {
        "id": _pid(),
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": _grid(0, y, 24, 1),
        "panels": [],
    }


def text(title, content, *, x, y, w=24, h=4):
    return {
        "id": _pid(),
        "type": "text",
        "title": title,
        "gridPos": _grid(x, y, w, h),
        "options": {"mode": "markdown", "content": content},
    }


def dashboard(uid, title, description, panels, *, refresh="30s", window="now-6h"):
    return {
        "uid": uid,
        "title": title,
        "description": description,
        "tags": ["pluton"],
        "timezone": "browser",
        "editable": True,
        "schemaVersion": 39,
        "version": 1,
        "refresh": refresh,
        "graphTooltip": 1,
        "time": {"from": window, "to": "now"},
        "timepicker": {},
        "annotations": {"list": []},
        "templating": {"list": []},
        "links": [],
        "panels": panels,
    }


def render(dash) -> tuple[pathlib.Path, str]:
    """The file this dashboard belongs in and the exact bytes it should contain."""
    # refIds are panel-local in Grafana; the shared counter above only keeps them distinct
    # within a panel. Renumbering here makes regeneration produce a stable diff.
    for panel in dash["panels"]:
        for i, t in enumerate(panel.get("targets", [])):
            t["refId"] = chr(65 + i)
    path = OUT / f"{dash['uid'].removeprefix('pluton-')}.json"
    return path, json.dumps(dash, indent=2) + "\n"


# ======================================================================================
#  1. Run Pipeline
# ======================================================================================

RATE = "$__rate_interval"

run_pipeline = dashboard(
    "pluton-run-pipeline",
    "Run Pipeline",
    "Terminal outcomes, node latency and the self-correction depth that AGENTS.md §13 "
    "treats as the platform's headline KPI.",
    [
        row("Outcome", y=0),
        gauge(
            "Success-rate SLO",
            [
                target(
                    'sum(increase(pluton_runs_total{status="SUCCEEDED"}[$__range]))'
                    " / clamp_min(sum(increase(pluton_runs_total[$__range])), 1)",
                    "success rate",
                    instant=True,
                )
            ],
            x=0,
            y=1,
            w=6,
            h=7,
            desc="Fraction of runs in the selected window that met every required "
            "criterion. PARTIAL runs — a real result that missed a threshold — count "
            "against this deliberately; §17's target is 70% on the core-10 suite.",
        ),
        stat(
            "Active runs",
            [target("sum(pluton_active_runs)", "active")],
            x=6,
            y=1,
            w=4,
            h=4,
            graph="area",
            desc="Runs currently held by a worker. Ceilinged by WORKER_MAX_JOBS × workers.",
        ),
        stat(
            "Queue depth",
            [target("pluton_queue_depth", "pending")],
            x=10,
            y=1,
            w=4,
            h=4,
            graph="area",
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 3},
                {"color": "red", "value": 6},
            ],
            desc="Pending arq jobs, sampled from the queue sorted set every 5 s.",
        ),
        stat(
            "Mean debug iterations",
            [
                target(
                    "sum(rate(pluton_debug_iterations_sum[$__range]))"
                    " / clamp_min(sum(rate(pluton_debug_iterations_count[$__range])), 0.0001)",
                    "mean",
                )
            ],
            x=14,
            y=1,
            w=5,
            h=4,
            decimals=2,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 2},
                {"color": "red", "value": 3.5},
            ],
            desc="How many self-correction cycles the average run needed. Approaching "
            "MAX_DEBUG_ITERATIONS means the loop is exhausting rather than converging.",
        ),
        stat(
            "Replans / h",
            [target("sum(increase(pluton_replans_total[1h]))", "replans")],
            x=19,
            y=1,
            w=5,
            h=4,
            desc="Evaluator- and Debugger-triggered returns to the Planner.",
        ),
        ts(
            "Runs by outcome",
            [
                target(
                    f"sum by (status) (increase(pluton_runs_total[{RATE}]))",
                    "{{status}}",
                )
            ],
            x=6,
            y=5,
            w=18,
            h=10,
            stack=True,
            fill=60,
            unit="short",
            desc="Stacked terminal outcomes. FAILED is a crash or an infrastructural "
            "failure; PARTIAL is a reproducible result that missed a required "
            "threshold — conflating the two hides which problem you have.",
        ),
        row("Latency", y=15),
        heatmap(
            "Node latency distribution",
            f"sum by (le) (increase(pluton_node_duration_seconds_bucket[{RATE}]))",
            x=0,
            y=16,
            w=12,
            h=9,
            desc="Every node, all outcomes. The bimodality to look for is a fast "
            "deterministic band (init, finalizer, routers) and a slow model band.",
        ),
        ts(
            "Node latency p95, by node",
            [
                target(
                    "histogram_quantile(0.95, sum by (le, node) "
                    f"(rate(pluton_node_duration_seconds_bucket[{RATE}])))",
                    "{{node}}",
                )
            ],
            x=12,
            y=16,
            w=12,
            h=9,
            unit="s",
            desc="p95 per node. sandbox_exec tracks container time, not model time.",
        ),
        ts(
            "Run duration percentiles",
            [
                target(
                    "histogram_quantile(0.5, sum by (le) "
                    f"(rate(pluton_run_duration_seconds_bucket[{RATE}])))",
                    "p50",
                ),
                target(
                    "histogram_quantile(0.95, sum by (le) "
                    f"(rate(pluton_run_duration_seconds_bucket[{RATE}])))",
                    "p95",
                ),
            ],
            x=0,
            y=25,
            w=12,
            h=8,
            unit="s",
            desc="End-to-end wall clock. RUN_WALLCLOCK_SECONDS (1800 s by default) is the "
            "hard ceiling — a p95 approaching it means runs are being killed.",
        ),
        ts(
            "Node visits (cycle-detection signal)",
            [
                target(
                    f"sum by (node) (rate(pluton_node_visits_total[{RATE}]))",
                    "{{node}}",
                )
            ],
            x=12,
            y=25,
            w=12,
            h=8,
            unit="ops",
            desc="Every node increments this exactly once per entry — it is the potential "
            "function in §6.4's termination proof. A node whose rate is far above the "
            "others is being cycled through.",
        ),
        row("Self-correction", y=33),
        heatmap(
            "Debug-iteration distribution",
            "sum by (le) (increase(pluton_debug_iterations_bucket[$__range]))",
            x=0,
            y=34,
            w=12,
            h=9,
            unit="short",
            legend_unit="short",
            desc="How many runs needed 0, 1, 2 … fixes. A distribution piling up at "
            "MAX_DEBUG_ITERATIONS is the stagnation case §6.2 guards against.",
        ),
        ts(
            "Criteria satisfaction ratio",
            [
                target(
                    "histogram_quantile(0.5, sum by (le) "
                    "(rate(pluton_criteria_satisfaction_ratio_bucket[$__range])))",
                    "p50",
                ),
                target(
                    "sum(rate(pluton_criteria_satisfaction_ratio_sum[$__range]))"
                    " / clamp_min(sum(rate(pluton_criteria_satisfaction_ratio_count[$__range])), 0.0001)",
                    "mean",
                ),
            ],
            x=12,
            y=34,
            w=12,
            h=9,
            unit="percentunit",
            desc="Fraction of *required* criteria met at the end of a run. 1.0 is SUCCEEDED; "
            "anything between 0 and 1 is the PARTIAL band.",
        ),
        ts(
            "Replans by reason",
            [
                target(
                    f"sum by (reason) (increase(pluton_replans_total[{RATE}]))",
                    "{{reason}}",
                )
            ],
            x=0,
            y=43,
            w=24,
            h=7,
            stack=True,
            fill=60,
            desc="`evaluator` — the rubric judged the approach wrong. `diagnosis` — the "
            "Debugger asked for one. `escalation` — the debug budget ran out.",
        ),
    ],
)

# ======================================================================================
#  2. LLM Performance
# ======================================================================================

llm = dashboard(
    "pluton-llm-performance",
    "LLM Performance",
    "Throughput, latency and schema adherence per model and role. On macOS the tokens/s "
    "panel is the GPU-utilisation proxy — see ARCHITECTURE.md §12.1.",
    [
        row("Throughput", y=0),
        ts(
            "Output tokens/s by model (p50)",
            [
                target(
                    "histogram_quantile(0.5, sum by (le, model) "
                    f"(rate(pluton_llm_tokens_per_second_bucket[{RATE}])))",
                    "{{model}} p50",
                ),
                target(
                    "histogram_quantile(0.05, sum by (le, model) "
                    f"(rate(pluton_llm_tokens_per_second_bucket[{RATE}])))",
                    "{{model}} p05",
                ),
            ],
            x=0,
            y=1,
            w=14,
            h=9,
            unit="short",
            desc="**This is the GPU panel on macOS.** Apple exposes no Prometheus-compatible "
            "GPU counters, so there is no utilisation series to plot (§12.1). A "
            "sustained drop here means the model spilled out of GPU memory to CPU — the "
            "p05 line is where that shows up first.",
        ),
        stat(
            "Tokens/s now (p50, all models)",
            [
                target(
                    "histogram_quantile(0.5, sum by (le) "
                    f"(rate(pluton_llm_tokens_per_second_bucket[{RATE}])))",
                    "tok/s",
                )
            ],
            x=14,
            y=1,
            w=5,
            h=4,
            decimals=1,
            thresholds=[
                {"color": "red", "value": None},
                {"color": "orange", "value": 10},
                {"color": "green", "value": 20},
            ],
            desc="Below ~5 tok/s the platform is unusable in practice; the "
            "PlutonLlmThroughputCollapsed rule fires there.",
        ),
        stat(
            "Error rate",
            [
                target(
                    'sum(rate(pluton_llm_requests_total{outcome="error"}[$__range]))'
                    " / clamp_min(sum(rate(pluton_llm_requests_total[$__range])), 0.0001)",
                    "errors",
                )
            ],
            x=19,
            y=1,
            w=5,
            h=4,
            unit="percentunit",
            decimals=3,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 0.01},
                {"color": "red", "value": 0.05},
            ],
            desc="Model calls that raised — a dropped Ollama connection, a timeout. Distinct "
            "from a call that returned unusable JSON, which is the repair ladder below.",
        ),
        ts(
            "Token rate by direction",
            [
                target(
                    f"sum by (direction) (rate(pluton_llm_tokens_total[{RATE}]))",
                    "{{direction}}",
                )
            ],
            x=14,
            y=5,
            w=10,
            h=5,
            unit="short",
            desc="`in` is prompt evaluation, `out` is generation. A large and growing `in` "
            "with flat `out` means prompts are bloating — the truncation ladders in "
            "AGENTS.md §7 exist for this.",
        ),
        row("Latency", y=10),
        ts(
            "Time to first token",
            [
                target(
                    "histogram_quantile(0.5, sum by (le) "
                    f"(rate(pluton_llm_ttft_seconds_bucket[{RATE}])))",
                    "p50",
                ),
                target(
                    "histogram_quantile(0.95, sum by (le) "
                    f"(rate(pluton_llm_ttft_seconds_bucket[{RATE}])))",
                    "p95",
                ),
                target(
                    "histogram_quantile(0.99, sum by (le) "
                    f"(rate(pluton_llm_ttft_seconds_bucket[{RATE}])))",
                    "p99",
                ),
            ],
            x=0,
            y=11,
            w=12,
            h=8,
            unit="s",
            desc="Only the streaming path can observe this, so these series cover calls made "
            "while a run is being watched. A TTFT in the tens of seconds is a model "
            "load, not a slow prompt — check OLLAMA_KEEP_ALIVE.",
        ),
        ts(
            "TTFT p95 by role",
            [
                target(
                    "histogram_quantile(0.95, sum by (le, role) "
                    f"(rate(pluton_llm_ttft_seconds_bucket[{RATE}])))",
                    "{{role}}",
                )
            ],
            x=12,
            y=11,
            w=12,
            h=8,
            unit="s",
            desc="Roles route to different models (§11.1), so a divergence here is usually "
            "a model-size difference rather than a prompt problem.",
        ),
        row("Demand and schema adherence", y=19),
        ts(
            "Requests by role",
            [
                target(
                    f"sum by (role) (rate(pluton_llm_requests_total[{RATE}]))",
                    "{{role}}",
                )
            ],
            x=0,
            y=20,
            w=12,
            h=8,
            unit="reqps",
            stack=True,
            fill=50,
            desc="Which agents are spending the model budget. The Coder and Debugger "
            "dominating is the expected shape of a debug-heavy run.",
        ),
        heatmap(
            "Structured-output repair depth",
            f"sum by (le) (increase(pluton_structured_output_attempts_bucket[{RATE}]))",
            x=12,
            y=20,
            w=12,
            h=8,
            unit="short",
            legend_unit="short",
            desc="Attempts needed for one schema-valid response. Mass at 1 means "
            "constrained decoding is working; mass at 2+ means the model needs the "
            "ValidationError shown back to it (§11.2 stage 3).",
        ),
        ts(
            "Repair-ladder outcome",
            [
                target(
                    f"sum by (stage) (rate(pluton_structured_output_attempts_count[{RATE}]))",
                    "{{stage}}",
                )
            ],
            x=0,
            y=28,
            w=12,
            h=7,
            unit="ops",
            stack=True,
            fill=60,
            desc="`constrained` validated first try, `repair` needed a round trip, "
            "`exhausted` never validated and the node applied its failure policy. A "
            "rising `exhausted` share is the evidence §11.2 asks for before "
            "implementing field-wise extraction.",
        ),
        ts(
            "Cache hits by kind",
            [
                target(
                    f"sum by (kind) (rate(pluton_llm_cache_hits_total[{RATE}]))",
                    "{{kind}}",
                )
            ],
            x=12,
            y=28,
            w=12,
            h=7,
            unit="ops",
            desc="**Expected to be empty in this build.** §12.1 defines this counter and the "
            "recording helper exists, but no LLM or embedding cache is wired up yet, so "
            "there is nothing to hit. It is a hit counter only — a ratio would need a "
            "miss counter the specification does not define.",
        ),
    ],
)

# ======================================================================================
#  3. Sandbox Health
# ======================================================================================

sandbox = dashboard(
    "pluton-sandbox-health",
    "Sandbox Health",
    "Execution outcomes and resource pressure for the container boundary described in "
    "ARCHITECTURE.md §10. A regression here is the platform's highest-severity failure.",
    [
        row("Outcomes", y=0),
        gauge(
            "Clean-execution rate",
            [
                target(
                    'sum(increase(pluton_sandbox_executions_total{classification="CLEAN"}[$__range]))'
                    " / clamp_min(sum(increase(pluton_sandbox_executions_total[$__range])), 1)",
                    "clean",
                    instant=True,
                )
            ],
            x=0,
            y=1,
            w=5,
            h=7,
            desc="Executions that exited 0. The rest are the debug loop's input — a low "
            "value is not necessarily bad, but a *falling* one means the Coder is "
            "regressing.",
        ),
        stat(
            "Timeouts / h",
            [target("sum(increase(pluton_sandbox_timeouts_total[1h]))", "timeouts")],
            x=5,
            y=1,
            w=5,
            h=4,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 2},
                {"color": "red", "value": 5},
            ],
            desc="Killed at the wall clock: SANDBOX_EXEC_TIMEOUT_S / "
            "SANDBOX_TRAIN_TIMEOUT_S. Reported as exit 137.",
        ),
        stat(
            "OOM kills / h",
            [target("sum(increase(pluton_sandbox_oom_total[1h]))", "ooms")],
            x=10,
            y=1,
            w=5,
            h=4,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 1},
                {"color": "red", "value": 3},
            ],
            desc="Killed by the cgroup limit. `--memory-swap` equals `--memory` (§10.4), so "
            "an allocation loop is killed rather than swapping the host to death.",
        ),
        stat(
            "Rejections / h",
            [
                target(
                    "sum(increase(pluton_sandbox_validation_rejections_total[1h]))",
                    "rejections",
                )
            ],
            x=15,
            y=1,
            w=4,
            h=4,
            desc="Static-gate rejections, which cost milliseconds instead of a container "
            "launch (§10.7).",
        ),
        stat(
            "Executions / h",
            [
                target(
                    "sum(increase(pluton_sandbox_executions_total[1h]))", "executions"
                )
            ],
            x=19,
            y=1,
            w=5,
            h=4,
            desc="Total, all classifications. MAX_SANDBOX_EXECUTIONS caps this per run.",
        ),
        ts(
            "Executions by classification",
            [
                target(
                    f"sum by (classification) (increase(pluton_sandbox_executions_total[{RATE}]))",
                    "{{classification}}",
                )
            ],
            x=5,
            y=5,
            w=19,
            h=10,
            stack=True,
            fill=60,
            desc="The §10.9 vocabulary. TIMEOUT and OOM take precedence over the exit code, "
            "and OOM takes precedence over TIMEOUT — a container killed for memory that "
            "also ran long is a memory story.",
        ),
        row("Resource pressure", y=15),
        ts(
            "Execution duration",
            [
                target(
                    "histogram_quantile(0.5, sum by (le, profile) "
                    f"(rate(pluton_sandbox_duration_seconds_bucket[{RATE}])))",
                    "{{profile}} p50",
                ),
                target(
                    "histogram_quantile(0.95, sum by (le, profile) "
                    f"(rate(pluton_sandbox_duration_seconds_bucket[{RATE}])))",
                    "{{profile}} p95",
                ),
            ],
            x=0,
            y=16,
            w=12,
            h=8,
            unit="s",
            desc="Includes container create and teardown, which is a second or two of the "
            "floor on the `exec` profile.",
        ),
        ts(
            "Peak container RSS",
            [
                target(
                    "histogram_quantile(0.95, sum by (le, profile) "
                    f"(rate(pluton_sandbox_max_rss_bytes_bucket[{RATE}])))",
                    "{{profile}} p95",
                ),
                target(
                    "histogram_quantile(0.5, sum by (le, profile) "
                    f"(rate(pluton_sandbox_max_rss_bytes_bucket[{RATE}])))",
                    "{{profile}} p50",
                ),
            ],
            x=12,
            y=16,
            w=12,
            h=8,
            unit="bytes",
            desc="Sampled every 2 s while the container runs, so a spike shorter than the "
            "sampling interval can be missed — an OOM with a low peak here is that case, "
            "not a contradiction. Compare against SANDBOX_EXEC_MEMORY / "
            "SANDBOX_TRAIN_MEMORY.",
        ),
        row("The static gate", y=24),
        bars(
            "Validation rejections by rule",
            [
                target(
                    "sum by (reason) (increase(pluton_sandbox_validation_rejections_total[$__range]))",
                    "{{reason}}",
                    instant=True,
                )
            ],
            x=0,
            y=25,
            w=12,
            h=9,
            desc="Which §10.7 rule fired. `network_import` and `readonly_write` dominating "
            "is the expected shape: those are the two things a model most often "
            "assumes it can do. A large `not_allowlisted` share means the sandbox "
            "image is missing a library the Coder keeps reaching for.",
        ),
        ts(
            "Rejection rate by rule",
            [
                target(
                    f"sum by (reason) (rate(pluton_sandbox_validation_rejections_total[{RATE}]))",
                    "{{reason}}",
                )
            ],
            x=12,
            y=25,
            w=12,
            h=9,
            unit="ops",
            stack=True,
            fill=50,
            desc="The same data over time. A step change after a prompt edit is the signal "
            "that the edit made code generation worse.",
        ),
    ],
)

# ======================================================================================
#  4. Retrieval Quality
# ======================================================================================

retrieval = dashboard(
    "pluton-retrieval-quality",
    "Retrieval Quality",
    "Hybrid search latency and hit quality per collection (ARCHITECTURE.md §7.3), plus the "
    "episodic-memory hit rate.",
    [
        row("Episodic memory", y=0),
        gauge(
            "Run-memory hit rate",
            [
                target(
                    'sum(increase(pluton_run_memory_hits_total{outcome="hit"}[$__range]))'
                    " / clamp_min(sum(increase(pluton_run_memory_hits_total[$__range])), 1)",
                    "hit rate",
                    instant=True,
                )
            ],
            x=0,
            y=1,
            w=6,
            h=7,
            thresholds=[
                {"color": "text", "value": None},
                {"color": "green", "value": 0.2},
            ],
            desc="Fraction of Debugger lookups that found a prior successful fix for the "
            "same error fingerprint. Necessarily near zero on a fresh install — the "
            "collection only fills as runs succeed — so read the *trend*, not the "
            "value. The 0.82 cosine floor (§7.3.3) is what keeps a hit meaningful.",
        ),
        ts(
            "Run-memory lookups",
            [
                target(
                    f"sum by (outcome) (rate(pluton_run_memory_hits_total[{RATE}]))",
                    "{{outcome}}",
                )
            ],
            x=6,
            y=1,
            w=18,
            h=7,
            unit="ops",
            stack=True,
            fill=50,
            desc="Hits and misses over time. Only SUCCEEDED runs write to this collection, so "
            "a platform that is failing everything cannot learn from itself.",
        ),
        row("Latency", y=8),
        ts(
            "Search latency by collection",
            [
                target(
                    "histogram_quantile(0.95, sum by (le, collection) "
                    f"(rate(pluton_retrieval_latency_seconds_bucket[{RATE}])))",
                    "{{collection}} p95",
                ),
                target(
                    "histogram_quantile(0.5, sum by (le, collection) "
                    f"(rate(pluton_retrieval_latency_seconds_bucket[{RATE}])))",
                    "{{collection}} p50",
                ),
            ],
            x=0,
            y=9,
            w=12,
            h=8,
            unit="s",
            desc="Timed from before the query embedding, not from the Qdrant call — on this "
            "hardware the embedding round trip is usually the larger half, and a panel "
            "that excluded it would stay flat while retrieval got slower.",
        ),
        stat(
            "Searches / min",
            [
                target(
                    "sum(rate(pluton_retrieval_latency_seconds_count[$__rate_interval])) * 60",
                    "searches",
                )
            ],
            x=12,
            y=9,
            w=6,
            h=4,
            decimals=1,
            desc="All collections. The Researcher issues several per round.",
        ),
        stat(
            "Empty-result rate",
            [
                target(
                    'sum(increase(pluton_retrieval_top_score_bucket{le="0.0"}[$__range]))'
                    " / clamp_min(sum(increase(pluton_retrieval_hits_count[$__range])), 1)",
                    "empty",
                )
            ],
            x=18,
            y=9,
            w=6,
            h=4,
            unit="percentunit",
            decimals=3,
            thresholds=[
                {"color": "green", "value": None},
                {"color": "orange", "value": 0.2},
                {"color": "red", "value": 0.5},
            ],
            desc="Queries whose best hit scored 0 — nothing cleared the score floor. A high "
            "value means the corpus does not cover what the agents are asking about, "
            "which is an ingestion problem rather than a retrieval one.",
        ),
        ts(
            "Mean hits per query",
            [
                target(
                    "sum by (collection) (rate(pluton_retrieval_hits_sum[$__rate_interval]))"
                    " / clamp_min(sum by (collection) (rate(pluton_retrieval_hits_count[$__rate_interval])), 0.0001)",
                    "{{collection}}",
                )
            ],
            x=12,
            y=13,
            w=12,
            h=4,
            unit="short",
            decimals=2,
            desc="Against the §7.3.4 limits: 8 for rd_corpus, 4 for code_exemplars, 3 for "
            "run_memory. Sitting at the limit means the floor is not binding; sitting "
            "well below it means it is.",
        ),
        row("Hit quality", y=17),
        heatmap(
            "Top-score distribution",
            f"sum by (le) (increase(pluton_retrieval_top_score_bucket[{RATE}]))",
            x=0,
            y=18,
            w=12,
            h=9,
            unit="short",
            legend_unit="short",
            desc="Score of the best-ranked hit per query. rd_corpus and code_exemplars "
            "are RRF-fused, so their scores are rank-based and small by nature; "
            "run_memory is a raw cosine score against a 0.82 floor. The two are on "
            "one axis here — read them as separate populations.",
        ),
        heatmap(
            "Hits-per-query distribution",
            f"sum by (le) (increase(pluton_retrieval_hits_bucket[{RATE}]))",
            x=12,
            y=18,
            w=12,
            h=9,
            unit="short",
            legend_unit="short",
            desc="Mass in the zero bucket is the retrieval failure worth chasing.",
        ),
    ],
)

# ======================================================================================
#  5. System
# ======================================================================================

CONTAINERS = 'name=~"autonomous_.*|pluton.*"'

system = dashboard(
    "pluton-system",
    "System",
    "Host and per-container resource use from node-exporter and cAdvisor, plus the "
    "datastores' own counters (ARCHITECTURE.md §12.1, §12.2).",
    [
        row("Host", y=0),
        ts(
            "Host CPU utilisation",
            [
                target(
                    '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[$__rate_interval])) * 100)',
                    "busy %",
                )
            ],
            x=0,
            y=1,
            w=8,
            h=7,
            unit="percent",
            desc="On macOS this is the Docker Desktop LinuxKit VM, not the Mac — the VM's CPU "
            "allocation is the ceiling, and the host's own load is invisible from inside "
            "a container.",
        ),
        ts(
            "Host memory",
            [
                target(
                    "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",
                    "used",
                ),
                target("node_memory_MemTotal_bytes", "total"),
            ],
            x=8,
            y=1,
            w=8,
            h=7,
            unit="bytes",
            desc="Same caveat as CPU: this is the VM's view.",
        ),
        ts(
            "Filesystem available",
            [
                target(
                    'node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}',
                    "{{mountpoint}}",
                )
            ],
            x=16,
            y=1,
            w=8,
            h=7,
            unit="bytes",
            desc="Where the `pluton_runs` volume lives, as node-exporter can see it. On "
            "Docker Desktop the named volume is inside the VM's disk image, so the "
            "series to watch is the root mount rather than a per-volume one. "
            "`make storage-report` gives the per-volume breakdown.",
        ),
        row("Containers (cAdvisor)", y=8),
        ts(
            "Container CPU",
            [
                target(
                    f"sum by (name) (rate(container_cpu_usage_seconds_total{{{CONTAINERS}}}[{RATE}]))",
                    "{{name}}",
                )
            ],
            x=0,
            y=9,
            w=12,
            h=8,
            unit="percentunit",
            desc="Cores, as a fraction. This is the panel that answers 'the worker is "
            "pinned' and 'Qdrant is busy'.",
        ),
        ts(
            "Container memory (working set)",
            [
                target(
                    f"sum by (name) (container_memory_working_set_bytes{{{CONTAINERS}}})",
                    "{{name}}",
                )
            ],
            x=12,
            y=9,
            w=12,
            h=8,
            unit="bytes",
            desc="Working set rather than RSS: it is what the cgroup limit is enforced "
            "against, so it is the number an OOM kill will be decided on.",
        ),
        ts(
            "Container network in",
            [
                target(
                    f"sum by (name) (rate(container_network_receive_bytes_total{{{CONTAINERS}}}[{RATE}]))",
                    "{{name}}",
                )
            ],
            x=0,
            y=17,
            w=12,
            h=7,
            unit="Bps",
            desc="A sandbox container should never appear here: `--network none` (§10.4). If "
            "one does, that is a §13.1 T2 finding, not a capacity observation.",
        ),
        ts(
            "Container network out",
            [
                target(
                    f"sum by (name) (rate(container_network_transmit_bytes_total{{{CONTAINERS}}}[{RATE}]))",
                    "{{name}}",
                )
            ],
            x=12,
            y=17,
            w=12,
            h=7,
            unit="Bps",
        ),
        row("Datastores", y=24),
        ts(
            "Postgres connections",
            [
                target(
                    "sum by (datname) (pg_stat_database_numbackends)", "{{datname}}"
                ),
                target("pg_settings_max_connections", "max"),
            ],
            x=0,
            y=25,
            w=8,
            h=8,
            unit="short",
            desc="The API pool, the worker pool and the LangGraph checkpointer each hold "
            "connections. Approaching max is what a run failing at a node boundary with "
            "a connection error looks like from the outside.",
        ),
        ts(
            "Redis memory",
            [
                target("redis_memory_used_bytes", "used"),
                target("redis_memory_max_bytes", "max"),
            ],
            x=8,
            y=25,
            w=8,
            h=8,
            unit="bytes",
            desc="Event streams are the bulk of this. `trim_event_streams` caps each run's "
            "stream; a monotonic rise means it is not keeping up.",
        ),
        ts(
            "Redis run-event stream lengths",
            [target("topk(10, redis_stream_length)", "{{stream}}")],
            x=16,
            y=25,
            w=8,
            h=8,
            unit="short",
            desc="Requires REDIS_EXPORTER_CHECK_STREAMS to name the key pattern, which the "
            "compose file sets to `run:*:events`. The ten longest streams: a run that "
            "streamed a very chatty sandbox shows up here first.",
        ),
        ts(
            "Qdrant collection vectors",
            [
                target("qdrant_collections_vector_total", "vectors"),
                target("qdrant_collections_total", "collections"),
            ],
            x=0,
            y=33,
            w=12,
            h=7,
            unit="short",
            desc="Corpus size, straight from Qdrant's own /metrics.",
        ),
        ts(
            "API request rate and latency",
            [
                target(f"sum(rate(pluton_http_requests_total[{RATE}]))", "requests/s"),
                target(
                    "histogram_quantile(0.95, sum by (le) "
                    f"(rate(pluton_http_request_duration_seconds_bucket[{RATE}])))",
                    "p95 (s)",
                ),
            ],
            x=12,
            y=33,
            w=12,
            h=7,
            unit="short",
            desc="`/metrics` and the shallow health probe are excluded from both series — a "
            "5-second liveness check would otherwise be most of the traffic.",
        ),
        row("GPU", y=40),
        text(
            "Reading this section",
            "GPU panels below populate only under the `linux-gpu` compose profile, which "
            "starts the DCGM exporter.\n\n"
            "**On macOS there is no GPU panel, by platform limitation rather than by "
            "omission.** Apple exposes no Prometheus-compatible GPU counters for Metal, so "
            "no exporter can be written. `pluton_llm_tokens_per_second` on the **LLM "
            "Performance** board is the practical proxy: a sustained drop means the model "
            "spilled out of GPU memory to CPU. See ARCHITECTURE.md §12.1.",
            x=0,
            y=41,
            w=24,
            h=4,
        ),
        ts(
            "GPU utilisation",
            [target("DCGM_FI_DEV_GPU_UTIL", "gpu {{gpu}}")],
            x=0,
            y=45,
            w=8,
            h=7,
            unit="percent",
        ),
        ts(
            "GPU framebuffer used",
            [target("DCGM_FI_DEV_FB_USED * 1024 * 1024", "gpu {{gpu}}")],
            x=8,
            y=45,
            w=8,
            h=7,
            unit="bytes",
            desc="DCGM reports MiB; scaled to bytes here so the axis matches the other "
            "memory panels.",
        ),
        ts(
            "GPU temperature and power",
            [
                target("DCGM_FI_DEV_GPU_TEMP", "temp °C gpu {{gpu}}"),
                target("DCGM_FI_DEV_POWER_USAGE", "power W gpu {{gpu}}"),
            ],
            x=16,
            y=45,
            w=8,
            h=7,
            unit="short",
        ),
    ],
)

DASHBOARDS = (run_pipeline, llm, sandbox, retrieval, system)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the checked-in dashboards differ from the generated ones",
    )
    args = parser.parse_args()

    drifted = False
    for dash in DASHBOARDS:
        path, generated = render(dash)
        if args.check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != generated:
                drifted = True
                print(
                    "\n".join(
                        difflib.unified_diff(
                            current.splitlines(),
                            generated.splitlines(),
                            fromfile=f"{path.name} (checked in)",
                            tofile=f"{path.name} (generated)",
                            lineterm="",
                            n=1,
                        )
                    )
                )
            continue
        OUT.mkdir(parents=True, exist_ok=True)
        path.write_text(generated, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO_ROOT)} ({len(dash['panels'])} panels)")

    if args.check:
        if drifted:
            print(
                "\nerror: provisioned dashboards are out of date. "
                "Run `make gen-dashboards`.",
                file=sys.stderr,
            )
            return 1
        print(
            f"{len(DASHBOARDS)} dashboards are in sync with scripts/gen_dashboards.py"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
