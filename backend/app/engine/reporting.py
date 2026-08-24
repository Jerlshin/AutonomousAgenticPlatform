"""Report construction — the deterministic half of the Reporter (AGENTS.md §7.8).

The report is the deliverable; everything else in a run was scaffolding. So this module
exists to make one guarantee cheap to hold: **the document is always written, and every
number in it comes from state.**

Three pieces, in dependency order:

* `report_context` — a pure projection of `AgentState` into the facts a report states.
  No model, no I/O, total on every state including one where planning never finished.
* `render_report` — the eight sections rendered from that context by a Jinja2 template.
  This is `SYNTHESISE_FALLBACK`: mechanical prose, complete data, no judgement.
* `assemble_report` — the merge. Narrative sections come from the model when it produced
  them and from the template when it did not; the data sections are always the template's.

**Why the data sections are never the model's.** §7.8's hard rules ask the model not to
invent a number and not to round one. Asking is not a control. Sections 5, 6 and 8 and the
criteria table in section 2 are pure tabulation of state, so they are spliced in
mechanically and the model is left the job it is actually good at: explaining what
happened. A report whose prose is imperfect is a mild disappointment. A report whose
accuracy column disagrees with `metrics.json` is worse than no report at all.

The template is a module constant rather than a file on disk. This node's contract is that
it cannot fail, and a missing or unreadable template file is exactly the kind of packaging
accident that would turn "cannot fail" into "produced nothing".
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings
from app.engine.criteria import COMPARATOR_SYMBOLS, determine_outcome
from app.engine.state import (
    AgentState,
    CriterionResult,
    Deliverable,
    Diagnosis,
    ErrorRecord,
    Plan,
    RunOutcome,
    StepStatus,
)

logger = logging.getLogger(__name__)

# The eight sections §7.8 requires, in order. Section 4 is mandatory even when nothing
# went wrong — a run with a clean first attempt says so explicitly, because "no section 4"
# and "no failures" are indistinguishable to a reader otherwise.
SECTION_TITLES: tuple[str, ...] = (
    "Objective",
    "Result",
    "Approach",
    "What went wrong and how it was fixed",
    "Results in detail",
    "Reproducing this run",
    "Limitations and next steps",
    "Artifacts",
)

# Sections whose content is tabulated from state and never taken from the model.
DATA_SECTIONS: frozenset[int] = frozenset({5, 6, 8})

# `## 4. What went wrong…`, tolerating the numbering and wording the model drifts into.
_HEADING = re.compile(r"^##\s*(\d)\s*[.)]?\s*(.*)$", re.MULTILINE)
_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)

# Below this a "section" is a heading with nothing under it, which is the model having
# emitted the skeleton and stopped. Treated as missing so the template fills it.
MIN_SECTION_CHARS = 40


# ------------------------------------------------------------------------------------
#  Context
# ------------------------------------------------------------------------------------


def report_context(state: AgentState) -> dict[str, Any]:
    """Project `AgentState` into the facts a report states. Pure and total.

    Every branch tolerates absence, because the Reporter runs on paths where planning
    never produced a plan and the sandbox never ran. "not measured" is always a valid
    answer; a plausible-looking number never is.
    """
    outcome, criteria_results = determine_outcome(state)
    plan: Plan | None = state.get("plan")
    last = state.get("last_outcome")
    usage = state.get("usage")
    metrics = dict((last.metrics or {}).get("metrics") or {}) if last else {}

    return {
        "title": _title(state, plan),
        "status": outcome.value,
        "run_id": state.get("run_id", "unknown"),
        "task_id": state.get("task_id", ""),
        "duration": _duration(usage.elapsed_seconds if usage else 0.0),
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
        "prompt": state.get("prompt", ""),
        "task_kind": plan.task_kind if plan else (state.get("task_kind") or "unknown"),
        "primary_metric": plan.primary_metric if plan else "",
        "assumptions": list(plan.assumptions) if plan else [],
        "headline": _headline(outcome, criteria_results, plan, metrics),
        "criteria": [_criterion_row(result) for result in criteria_results],
        "steps": _steps(state, plan),
        "dataset": _dataset(plan, state),
        "debug_cycles": debug_cycles(state),
        "revision_count": len(state.get("code_revisions") or []),
        "metrics": metrics,
        "params": dict((last.metrics or {}).get("params") or {}) if last else {},
        "plots": list((last.metrics or {}).get("plots") or []) if last else [],
        "runtime": dict((last.metrics or {}).get("runtime") or {}) if last else {},
        "mlflow_url": _mlflow_url(state),
        "reproduction": _reproduction(state, plan, last),
        "limitations": _limitations(state, outcome, criteria_results, plan),
        "artifacts": [_artifact_row(d) for d in state.get("deliverables") or []],
        "execution": _execution(state),
        "usage": _usage_row(state),
    }


def debug_cycles(state: AgentState) -> list[dict[str, Any]]:
    """One entry per failure, paired with the diagnosis and the revision that answered it.

    Section 4 is the most instructive part of the document, and it is assembled here
    rather than described to the model so that a report can never quietly omit an attempt.
    `errors` and `diagnoses` advance in lockstep through the correctness loop, so they are
    zipped by position; a diagnosis missing because the Debugger degraded simply renders
    as "no diagnosis was recorded" rather than shifting every later pairing.
    """
    errors: list[ErrorRecord] = list(state.get("errors") or [])
    diagnoses: list[Diagnosis] = list(state.get("diagnoses") or [])
    revisions = list(state.get("code_revisions") or [])
    last = state.get("last_outcome")

    cycles: list[dict[str, Any]] = []
    for index, error in enumerate(errors):
        diagnosis = diagnoses[index] if index < len(diagnoses) else None
        # The revision written *after* this failure is the one that attempted the fix.
        fix = next(
            (r for r in revisions if r.revision == error.revision + 1),
            None,
        )
        resolved = _was_resolved(errors, index, last)
        cycles.append(
            {
                "n": index + 1,
                "kind": error.kind.value,
                "fingerprint": error.fingerprint,
                "exception_type": error.exception_type,
                "message": error.message,
                "file": error.file,
                "line": error.line,
                "revision": error.revision,
                "root_cause": diagnosis.root_cause if diagnosis else "",
                "fix_strategy": diagnosis.fix_strategy if diagnosis else "",
                "targeted_changes": list(diagnosis.targeted_changes)
                if diagnosis
                else [],
                "confidence": diagnosis.confidence if diagnosis else None,
                "fix_rationale": fix.rationale if fix else "",
                "fix_revision": fix.revision if fix else None,
                "resolved": resolved,
            }
        )
    return cycles


# ------------------------------------------------------------------------------------
#  Rendering
# ------------------------------------------------------------------------------------

REPORT_TEMPLATE = """\
# {{ title }}

**Status:** {{ status }} · **Run:** `{{ run_id }}` · **Duration:** {{ duration }} · **Date:** {{ date }}

## 1. Objective

{{ prompt }}

{% if assumptions %}The plan recorded these assumptions:

{% for assumption in assumptions %}- {{ assumption }}
{% endfor %}{% else %}The plan recorded no explicit assumptions.
{% endif %}
## 2. Result

{{ headline }}

{{ criteria_table }}

## 3. Approach

{% if steps %}Task kind `{{ task_kind }}`, judged on `{{ primary_metric }}`.

{% for step in steps %}{{ step.n }}. **{{ step.title }}** (`{{ step.kind }}`) — {{ step.status }}. {{ step.description }}
{% endfor %}
{% if dataset %}Data: `{{ dataset.id }}` at `{{ dataset.path }}`{% if dataset.target %}, target column `{{ dataset.target }}`{% endif %}.
{% endif %}{% else %}No plan was produced, so there is no approach to describe: the run ended before planning completed.
{% endif %}
## 4. What went wrong and how it was fixed

{% if debug_cycles %}{% for cycle in debug_cycles %}### Attempt {{ cycle.revision }} — {{ cycle.kind }}{% if cycle.exception_type %} ({{ cycle.exception_type }}){% endif %}

**What happened.** {{ cycle.message }}{% if cycle.file %} The failure was at `{{ cycle.file }}`{% if cycle.line %} line {{ cycle.line }}{% endif %}.{% endif %}

**Diagnosis.** {% if cycle.root_cause %}{{ cycle.root_cause }}{% else %}No diagnosis was recorded for this failure.{% endif %}

{% if cycle.targeted_changes %}**The fix.** {{ cycle.fix_strategy }}

{% for change in cycle.targeted_changes %}- {{ change }}
{% endfor %}{% endif %}{% if cycle.resolved %}
The next attempt ran cleanly.
{% else %}
This attempt did not resolve the failure.
{% endif %}
{% endfor %}{% else %}{% if revision_count %}No execution failures occurred. The first program written for this run executed cleanly.{% else %}No execution failures occurred, because no program was ever executed — the run ended before code reached the sandbox.{% endif %}
{% endif %}
## 5. Results in detail

{% if metrics %}| Metric | Value |
|---|---|
{% for name, value in metrics.items() %}| `{{ name }}` | {{ value }} |
{% endfor %}
{% else %}No metrics were produced — `/artifacts/metrics.json` was never written or did not validate.

{% endif %}{% if params %}Parameters: {% for name, value in params.items() %}`{{ name }}={{ value }}`{% if not loop.last %}, {% endif %}{% endfor %}.

{% endif %}{% if plots %}Plots (relative to the artifacts directory):

{% for plot in plots %}- `{{ plot }}`
{% endfor %}
{% endif %}{% if mlflow_url %}MLflow run: {{ mlflow_url }}

{% endif %}Execution: {{ execution }}

## 6. Reproducing this run

```
{{ reproduction.command }}
```

| | |
|---|---|
| Dataset | `{{ reproduction.dataset_id }}` |
| Dataset SHA-256 | `{{ reproduction.dataset_sha256 }}` |
| Seed | `{{ reproduction.seed }}` |
| Sandbox image | `{{ reproduction.image }}` |
| Sandbox profile | `{{ reproduction.profile }}` |
| Code SHA-256 | `{{ reproduction.code_sha256 }}` |

## 7. Limitations and next steps

{% for limitation in limitations %}- {{ limitation }}
{% endfor %}
## 8. Artifacts

{% if artifacts %}| File | Type | Size | SHA-256 |
|---|---|---|---|
{% for artifact in artifacts %}| `{{ artifact.name }}` | {{ artifact.type }} | {{ artifact.size }} | `{{ artifact.sha256 }}` |
{% endfor %}
{% else %}No artifacts were produced.
{% endif %}"""

# Appended after the sections rather than written into the template: `split_sections` cuts
# on `## N.` headings, so anything trailing section 8 becomes part of section 8's body and
# would be emitted twice by `assemble_report`.
REPORT_FOOTER = "---\n\n{usage}\n"


def render_report(context: dict[str, Any]) -> str:
    """The full eight-section report, rendered deterministically from `context`."""
    return _with_footer(render_sections(context), context)


def _with_footer(body: str, context: dict[str, Any]) -> str:
    """Attach the budget footer, identically for both renderers."""
    return body.rstrip() + "\n\n" + REPORT_FOOTER.format(usage=context["usage"])


def render_sections(context: dict[str, Any]) -> str:
    """The eight sections, without the footer, so `assemble_report` can split them.

    Jinja2 is imported here rather than at module scope so that importing the engine —
    which the whole test suite and every schema check does — never depends on it. If it is
    genuinely unavailable, `_render_plainly` produces the same sections without it; the
    report is degraded, not absent.
    """
    context = {**context, "criteria_table": criteria_table(context.get("criteria"))}
    try:
        from jinja2 import StrictUndefined, Template
    except ImportError:  # pragma: no cover - exercised only in stripped environments
        logger.warning("Jinja2 is not installed; rendering the report without it")
        return _render_plainly(context)

    try:
        template = Template(
            REPORT_TEMPLATE, undefined=StrictUndefined, keep_trailing_newline=True
        )
        return template.render(**context)
    except Exception as exc:  # noqa: BLE001 - the report is written whatever happens
        logger.error(
            "Report template rendering failed (%s); falling back to plain", exc
        )
        return _render_plainly(context)


def assemble_report(raw: str, context: dict[str, Any]) -> str:
    """Merge model prose with deterministic data into the required eight sections.

    Post-generation section checking, as §7.8 specifies, plus the splice: a narrative
    section the model wrote is kept, one it skipped is filled from the template, and the
    data sections are the template's either way.
    """
    # The sections without the footer: splitting the footer-bearing document would fold
    # it into section 8's body and emit it twice.
    template_sections = split_sections(render_sections(context))
    model_sections = split_sections(raw)

    lines = [
        f"# {context['title']}",
        "",
        f"**Status:** {context['status']} · **Run:** `{context['run_id']}` · "
        f"**Duration:** {context['duration']} · **Date:** {context['date']}",
        "",
    ]

    for number, title in enumerate(SECTION_TITLES, start=1):
        body = template_sections.get(number, "")
        if number not in DATA_SECTIONS:
            written = model_sections.get(number, "")
            if len(written.strip()) >= MIN_SECTION_CHARS:
                body = written
        if number == 2:
            # The criteria table is arithmetic over metrics.json, so the model's version
            # of it — rounded, reordered, or invented — is dropped for the real one.
            body = (
                _strip_tables(body).rstrip()
                + "\n\n"
                + criteria_table(context.get("criteria"))
            )
        lines += [f"## {number}. {title}", "", body.strip(), ""]

    return _with_footer("\n".join(lines), context)


def split_sections(markdown: str) -> dict[int, str]:
    """Split a report into `{section_number: body}` on its `## N.` headings."""
    sections: dict[int, str] = {}
    matches = list(_HEADING.finditer(markdown or ""))
    for index, match in enumerate(matches):
        number = int(match.group(1))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        sections[number] = markdown[match.end() : end].strip()
    return sections


def criteria_table(criteria: list[dict[str, Any]] | None) -> str:
    """The §2 criteria table, generated from state so it cannot disagree with it."""
    rows = list(criteria or [])
    if not rows:
        return (
            "No success criteria were evaluated — the run produced no metrics to check "
            "them against."
        )
    lines = ["| Criterion | Target | Achieved | Status |", "|---|---|---|---|"]
    lines += [
        f"| {row['metric']} | {row['target']} | {row['achieved']} | {row['status']} |"
        for row in rows
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------------------------
#  Context helpers
# ------------------------------------------------------------------------------------


def _title(state: AgentState, plan: Plan | None) -> str:
    """A title a reader can scan a directory by: the ask, trimmed to one line."""
    prompt = (state.get("prompt") or "").strip().splitlines()
    headline = prompt[0] if prompt else ""
    if not headline:
        return f"Run {state.get('run_id', 'unknown')}"
    if len(headline) > 110:
        headline = headline[:107].rstrip() + "…"
    return headline


def _headline(
    outcome: RunOutcome,
    criteria: list[CriterionResult],
    plan: Plan | None,
    metrics: dict[str, Any],
) -> str:
    """One paragraph a non-specialist can read, stating the result without spin."""
    primary = plan.primary_metric if plan else ""
    observed = metrics.get(primary)
    achieved = (
        f"The headline metric `{primary}` reached {observed}."
        if isinstance(observed, (int, float)) and not isinstance(observed, bool)
        else ""
    )
    missed = [c for c in criteria if c.required and not c.passed]

    if outcome is RunOutcome.SUCCEEDED:
        return " ".join(
            filter(
                None,
                ["The run succeeded and met every required criterion.", achieved],
            )
        )
    if outcome is RunOutcome.PARTIAL:
        gaps = ", ".join(
            f"`{c.metric}` {c.observed if c.observed is not None else 'not measured'} "
            f"against a target of {COMPARATOR_SYMBOLS.get(c.comparator, c.comparator)} "
            f"{c.threshold:g}"
            for c in missed
        )
        return " ".join(
            filter(
                None,
                [
                    "The run produced a real, reproducible result that did not meet every "
                    "required criterion.",
                    f"Unmet: {gaps}." if gaps else "",
                    achieved,
                ],
            )
        )
    if outcome is RunOutcome.CANCELLED:
        return "The run was cancelled before it could produce a result."
    return (
        "The run failed: it did not produce a validated result. Section 4 records every "
        "attempt and what went wrong with each."
    )


def _criterion_row(result: CriterionResult) -> dict[str, Any]:
    symbol = COMPARATOR_SYMBOLS.get(result.comparator, result.comparator)
    if result.observed is None:
        achieved = "not measured"
    else:
        achieved = f"{result.observed:.4f}"
    if result.passed:
        status = "✅ Pass"
    elif result.required:
        status = "❌ Fail"
    else:
        status = "⚠️ Miss (stretch goal)"
    if result.note:
        status = f"{status} — {result.note}"
    return {
        "metric": result.metric,
        "target": f"{symbol} {result.threshold:g}",
        "achieved": achieved,
        "status": status,
        "required": result.required,
    }


def _steps(state: AgentState, plan: Plan | None) -> list[dict[str, Any]]:
    if plan is None:
        return []
    status = state.get("step_status") or {}
    return [
        {
            "n": step.index + 1,
            "title": step.title,
            "kind": step.kind.value,
            "status": status.get(step.id, StepStatus.PENDING).value,
            "description": step.description,
        }
        for step in plan.steps
    ]


def _dataset(plan: Plan | None, state: AgentState) -> dict[str, Any] | None:
    step = plan.step(state.get("current_step_id")) if plan else None
    binding = step.dataset if step else None
    if binding is None and plan is not None:
        binding = next((s.dataset for s in plan.steps if s.dataset is not None), None)
    if binding is None:
        return None
    return {
        "id": binding.dataset_id,
        "path": binding.path,
        "sha256": binding.sha256,
        "target": binding.target_column,
        "n_samples": binding.n_samples,
    }


def _reproduction(state: AgentState, plan: Plan | None, last: Any) -> dict[str, Any]:
    """Exactly what a reader needs to run this again and get the same numbers."""
    dataset = _dataset(plan, state) or {}
    revision = state.get("current_revision")
    profile = last.profile if last is not None else settings.SANDBOX_DEFAULT_PROFILE
    return {
        "command": f"make reproduce RUN_ID={state.get('run_id', 'unknown')}",
        "dataset_id": dataset.get("id") or "none",
        "dataset_sha256": dataset.get("sha256") or "none",
        "seed": (state.get("metadata") or {}).get("seed", "unknown"),
        "image": _image_for(profile),
        "profile": profile,
        "code_sha256": revision.sha256 if revision else "no code was produced",
    }


def _image_for(profile: str) -> str:
    """The pinned digest of the image the profile ran, or its tag when unpinned."""
    from app.services.sandbox import SandboxLaunchError, profile_for, resolve_image

    try:
        return resolve_image(profile_for(profile).image)
    except (SandboxLaunchError, ValueError):
        return "unknown"


def _limitations(
    state: AgentState,
    outcome: RunOutcome,
    criteria: list[CriterionResult],
    plan: Plan | None,
) -> list[str]:
    """Honest limits, derived from what the run demonstrably did not do."""
    limitations: list[str] = []

    unmeasured = [c.metric for c in criteria if c.observed is None]
    if unmeasured:
        limitations.append(
            "These criteria could not be checked because the metric was never produced: "
            + ", ".join(f"`{m}`" for m in unmeasured)
            + "."
        )
    missed = [
        c for c in criteria if c.required and not c.passed and c.observed is not None
    ]
    if missed:
        limitations.append(
            "The result is below the required bar on "
            + ", ".join(f"`{c.metric}`" for c in missed)
            + "; the numbers reported are real, they are simply not good enough yet."
        )

    skipped = [
        step
        for step in (plan.steps if plan else [])
        if (state.get("step_status") or {}).get(step.id) is StepStatus.SKIPPED
    ]
    if skipped:
        limitations.append(
            "Planned but not executed: "
            + ", ".join(f"{s.title} (`{s.kind.value}`)" for s in skipped)
            + "."
        )

    last = state.get("last_outcome")
    if last is not None and last.validation.warnings:
        limitations.append(
            "Static validation warned about: " + "; ".join(last.validation.warnings)
        )

    cycles = len(state.get("errors") or [])
    if cycles:
        limitations.append(
            f"The result was reached after {cycles} failed execution"
            f"{'s' if cycles != 1 else ''}; the debugging narrative in section 4 is part "
            "of the evidence, not an aside."
        )

    if outcome is not RunOutcome.SUCCEEDED:
        limitations.append(
            "No claim is made that the approach is the best available one — only that "
            "this is what it produced under the budget it was given."
        )
    else:
        limitations.append(
            "A single held-out split was measured. Repeated splits or cross-validated "
            "intervals would say more about how stable this number is."
        )
    return limitations


def _execution(state: AgentState) -> str:
    last = state.get("last_outcome")
    if last is None:
        return "no sandbox execution took place."
    return (
        f"classification `{last.classification}`, exit code {last.exit_code}, "
        f"{last.duration_ms} ms on the `{last.profile}` profile, revision "
        f"{last.revision}."
    )


def _usage_row(state: AgentState) -> str:
    usage = state.get("usage")
    if usage is None:
        return "No budget accounting was recorded for this run."
    return (
        f"*{usage.llm_calls} model calls ({usage.tokens_in} in / {usage.tokens_out} out), "
        f"{usage.sandbox_executions} sandbox executions, {usage.node_visits} node visits.*"
    )


def _mlflow_url(state: AgentState) -> str:
    ref = state.get("mlflow")
    return ref.ui_url if ref is not None else ""


def _artifact_row(deliverable: Deliverable) -> dict[str, Any]:
    return {
        "name": deliverable.name,
        "type": deliverable.artifact_type,
        "size": _human_bytes(deliverable.size_bytes),
        "sha256": deliverable.sha256[:16] + "…",
    }


def _was_resolved(errors: list[ErrorRecord], index: int, last: Any) -> bool:
    """Whether the attempt after this failure got further than the failure did."""
    if index + 1 < len(errors):
        return errors[index + 1].fingerprint != errors[index].fingerprint
    return last is not None and last.classification == "CLEAN"


def _duration(seconds: float) -> str:
    total = int(max(seconds, 0.0))
    return f"{total // 60:02d}:{total % 60:02d}"


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"  # pragma: no cover - unreachable, the loop returns first


def _strip_tables(markdown: str) -> str:
    """Remove markdown table rows, leaving the prose around them."""
    return _TABLE_LINE.sub("", markdown)


def _render_plainly(context: dict[str, Any]) -> str:
    """The same eight sections without a template engine.

    Only reachable when Jinja2 is missing or the template itself raised. It is short
    because it does not have to be good — it has to exist, so that "the report is always
    written" has no exception clause.
    """
    lines = [
        f"# {context['title']}",
        "",
        f"**Status:** {context['status']} · **Run:** `{context['run_id']}` · "
        f"**Duration:** {context['duration']} · **Date:** {context['date']}",
        "",
        "## 1. Objective",
        "",
        context["prompt"] or "(no prompt recorded)",
        "",
        "## 2. Result",
        "",
        context["headline"],
        "",
        context["criteria_table"],
        "",
        "## 3. Approach",
        "",
        "\n".join(
            f"{step['n']}. {step['title']} ({step['kind']}) — {step['status']}"
            for step in context["steps"]
        )
        or "No plan was produced.",
        "",
        "## 4. What went wrong and how it was fixed",
        "",
        "\n".join(
            f"- Attempt {cycle['revision']} [{cycle['kind']}]: {cycle['message']} "
            f"Diagnosis: {cycle['root_cause'] or 'none recorded'}"
            for cycle in context["debug_cycles"]
        )
        or "No execution failures occurred.",
        "",
        "## 5. Results in detail",
        "",
        "\n".join(f"- `{name}`: {value}" for name, value in context["metrics"].items())
        or "No metrics were produced.",
        "",
        "## 6. Reproducing this run",
        "",
        f"- Command: `{context['reproduction']['command']}`",
        f"- Dataset: `{context['reproduction']['dataset_id']}` "
        f"(sha256 `{context['reproduction']['dataset_sha256']}`)",
        f"- Seed: `{context['reproduction']['seed']}`",
        f"- Image: `{context['reproduction']['image']}`",
        "",
        "## 7. Limitations and next steps",
        "",
        "\n".join(f"- {item}" for item in context["limitations"]),
        "",
        "## 8. Artifacts",
        "",
        "\n".join(
            f"- `{a['name']}` ({a['type']}, {a['size']}, sha256 `{a['sha256']}`)"
            for a in context["artifacts"]
        )
        or "No artifacts were produced.",
        "",
    ]
    return "\n".join(lines)


__all__ = [
    "DATA_SECTIONS",
    "REPORT_FOOTER",
    "SECTION_TITLES",
    "assemble_report",
    "criteria_table",
    "debug_cycles",
    "render_report",
    "render_sections",
    "report_context",
    "split_sections",
]
