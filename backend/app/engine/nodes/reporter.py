"""`reporter` — write the document the run exists to produce (AGENTS.md §7.8).

This node runs on **every** terminal path: success, quality miss, crash, cancellation, a
Planner that never produced a plan. It is `finalizer`'s sole predecessor, which is what
makes the deliverable guarantee in §6.4's corollary structural — every terminating path
passes through here, and every path through here writes `report_markdown`.

Its failure policy is `SYNTHESISE_FALLBACK`, declared on the decorator rather than
implemented as a `try` inside the body. If the model is unreachable, returns an apology, or
emits three of the eight sections, `engine.reporting` renders the whole document from state
instead. The prose is mechanical; the data is complete; the report exists.

**The model is not trusted with numbers.** Sections 5, 6 and 8 and the criteria table are
tabulated from state by `assemble_report` and spliced in after generation, so the report
cannot contradict `metrics.json`. What the model is asked for is the part it is genuinely
better at: explaining to a colleague who did not watch the run what was attempted, what
broke, and what the result means.

**Episodic memory write** (§7.8, ARCHITECTURE.md §7.3.3): after the report is assembled,
every debug cycle that was actually resolved — a fingerprint whose next revision ran
cleaner — is distilled into one `run_memory` point: the fingerprint, a unified diff of the
fix, and a one-line summary. This only ever happens when `outcome == SUCCEEDED`. Recording
a "fix" from a run that did not actually succeed would poison the memory with approaches
that do not work, which is exactly what the Debugger's episodic lookup (§7.5) trusts it not
to contain. A write failure is logged and swallowed — the report this node exists to
produce is not allowed to depend on Qdrant being reachable.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.engine.nodes.base import (
    FailurePolicy,
    get_chat_client,
    get_run_memory_writer,
    node,
)
from app.engine.prompts import UNTRUSTED_PREAMBLE, load_prompt, wrap_untrusted
from app.engine.reporting import (
    assemble_report,
    criteria_table,
    render_report,
    report_context,
)
from app.engine.state import AgentState, CodeRevision, RunPhase
from app.engine.structured import call_text

logger = logging.getLogger(__name__)


def fallback_report(state: AgentState, exc: Exception | None = None) -> dict[str, Any]:
    """The `SYNTHESISE_FALLBACK` path: the deterministic template, rendered from state."""
    if exc is not None:
        logger.warning("Reporter falling back to the deterministic template: %s", exc)
    return {"report_markdown": render_report(report_context(state))}


@node(
    name="reporter",
    phase=RunPhase.REPORT,
    policy=FailurePolicy.SYNTHESISE_FALLBACK,
    fallback=fallback_report,
)
async def reporter_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    context = report_context(state)
    llm = get_chat_client(config, "reporter")
    prompt = load_prompt("reporter")

    system = prompt.render(
        untrusted_preamble=UNTRUSTED_PREAMBLE,
        status=context["status"],
        run_id=context["run_id"],
        duration=context["duration"],
        prompt_block=wrap_untrusted("user_prompt", context["prompt"] or "(empty)"),
        plan_block=_plan_block(context),
        criteria_block=_criteria_block(context),
        results_block=_results_block(context),
        failure_block=_failure_block(context),
        artifacts_block=_artifacts_block(context),
    )
    user = (
        "Write the report now. Start at `## 1. Objective` and write sections 1, 2, 3, 4 "
        "and 7. State the outcome plainly in the first paragraph of section 2."
    )

    text, usage = await call_text(llm, system=system, user=user)
    markdown = assemble_report(text, context)

    logger.info(
        "Report written for run %s (%s, %d chars)",
        context["run_id"],
        context["status"],
        len(markdown),
    )

    await _write_episodic_memory(config, state, context)

    return {
        "report_markdown": markdown,
        "usage": usage,
        "messages": [
            AIMessage(
                content=f"Report written: {context['status']}, {context['title']}"
            )
        ],
        "metadata": {
            **(state.get("metadata") or {}),
            "prompt_version_reporter": prompt.version,
        },
    }


def _plan_block(context: dict[str, Any]) -> str:
    """The plan as executed — step titles, statuses, dataset, assumptions."""
    if not context["steps"]:
        return (
            "### Plan\n\nNo plan was produced; the run ended before planning completed."
        )

    lines = [
        "### Plan as executed",
        "",
        f"Task kind `{context['task_kind']}`, judged on `{context['primary_metric']}`.",
        "",
    ]
    lines += [
        f"{step['n']}. **{step['title']}** (`{step['kind']}`) — {step['status']}. "
        f"{step['description']}"
        for step in context["steps"]
    ]
    dataset = context["dataset"]
    if dataset:
        lines += [
            "",
            f"Dataset `{dataset['id']}` at `{dataset['path']}`"
            + (f", target column `{dataset['target']}`" if dataset["target"] else "")
            + (f", {dataset['n_samples']} rows" if dataset["n_samples"] else "")
            + ".",
        ]
    if context["assumptions"]:
        lines += ["", "Assumptions recorded by the plan:"]
        lines += [f"- {assumption}" for assumption in context["assumptions"]]
    return "\n".join(lines)


def _criteria_block(context: dict[str, Any]) -> str:
    return "\n".join(
        [
            "### Criteria, already evaluated",
            "",
            context["headline"],
            "",
            criteria_table(context["criteria"]),
            "",
            "This table is appended to your section 2 verbatim. Do not restate it, and do "
            "not contradict it.",
        ]
    )


def _results_block(context: dict[str, Any]) -> str:
    """Every measured number, so the prose has something true to be about."""
    lines = ["### Measurements", ""]
    if context["metrics"]:
        lines += [f"- `{name}`: {value}" for name, value in context["metrics"].items()]
    else:
        lines.append("- No metrics were produced. Every metric is 'not measured'.")
    if context["params"]:
        lines += [
            "",
            "Parameters: "
            + ", ".join(f"`{k}={v}`" for k, v in context["params"].items())
            + ".",
        ]
    if context["plots"]:
        lines += ["", "Plots: " + ", ".join(f"`{p}`" for p in context["plots"]) + "."]
    lines += ["", f"Execution: {context['execution']}"]
    return "\n".join(lines)


def _failure_block(context: dict[str, Any]) -> str:
    """The debugging and refinement narrative, pre-assembled so section 4 omits nothing.

    Two kinds of thing go wrong in a run and the report has to account for both: a program
    that crashed (loop 1) and a program that ran and was not good enough (loop 2). The
    second leaves no traceback, so a section 4 written only from `errors` would describe a
    four-revision run as if it had gone right the first time.
    """
    cycles = context["debug_cycles"]
    quality = _quality_block(context)
    if not cycles:
        if quality:
            return quality
        lines = [
            "### Failures",
            "",
            'None. Write section 4 as: "No execution failures occurred."',
        ]
        return "\n".join(lines)

    lines = ["### Failures, in order", ""]
    for cycle in cycles:
        location = (
            f" at `{cycle['file']}` line {cycle['line']}"
            if cycle["file"] and cycle["line"]
            else ""
        )
        exception = f" ({cycle['exception_type']})" if cycle["exception_type"] else ""
        lines += [
            f"**Attempt {cycle['revision']} — {cycle['kind']}{exception}**",
            f"- What failed: {cycle['message']}{location}",
            f"- Diagnosis: {cycle['root_cause'] or 'none was recorded'}",
        ]
        if cycle["targeted_changes"]:
            lines.append("- Fix applied: " + "; ".join(cycle["targeted_changes"]))
        lines.append(
            "- Outcome: "
            + ("the next attempt got further" if cycle["resolved"] else "still failing")
        )
        lines.append("")
    lines.append(
        "Write one subsection per attempt above. Do not omit any, including the ones that "
        "did not work."
    )
    if quality:
        lines += ["", quality]
    return "\n".join(lines)


def _quality_block(context: dict[str, Any]) -> str:
    """Evaluations that sent the run back around, with the gap that motivated each.

    The numbers are the Evaluator's arithmetic, not the model's recollection of it: the
    whole point of tabulating section 4 rather than describing it is that a report cannot
    quietly disagree with `metrics.json`.
    """
    cycles = context["quality_cycles"]
    if not cycles:
        return ""

    lines = ["### Evaluations that sent the run back", ""]
    for cycle in cycles:
        lines += [
            f"**Evaluation {cycle['n']} — {cycle['decision']} "
            f"(criteria score {cycle['score']})**",
            f"- Fell short: {cycle['shortfall'] or cycle['summary']}",
            f"- What was done about it: {cycle['directive']}",
        ]
        if cycle["rubric"]:
            lines.append("- Quality rubric: " + ", ".join(cycle["rubric"]))
        lines.append("")
    lines.append(
        "Explain each of these in section 4 too. A run that produced a number, was judged "
        "insufficient and tried again has a story the reader needs, even though nothing "
        "crashed."
    )
    return "\n".join(lines)


def _artifacts_block(context: dict[str, Any]) -> str:
    if not context["artifacts"]:
        return "### Artifacts\n\nNone were produced."
    listing = "\n".join(
        f"- `{a['name']}` ({a['type']}, {a['size']})" for a in context["artifacts"]
    )
    return f"### Artifacts\n\n{listing}\n\nThe artifact table is appended for you."


async def _write_episodic_memory(
    config: RunnableConfig, state: AgentState, context: dict[str, Any]
) -> None:
    """Distil every resolved debug cycle into `run_memory`, on `SUCCEEDED` runs only."""
    if context["status"] != "SUCCEEDED":
        return

    writer = get_run_memory_writer(config)
    for point in _episodic_points(state, context):
        try:
            result = writer(**point)
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:  # noqa: BLE001 - a write failure must not cost the report
            logger.warning(
                "Failed to write run_memory point for %s: %s",
                point["error_fingerprint"],
                exc,
            )


def _episodic_points(
    state: AgentState, context: dict[str, Any]
) -> list[dict[str, Any]]:
    """One `run_memory` point per debug cycle the next revision actually resolved.

    Refuses a non-`SUCCEEDED` context itself, rather than trusting every caller to check
    first — the whole reason this gate exists is that a "fix" from a run that did not
    actually succeed would poison the memory with approaches that do not work.
    """
    if context["status"] != "SUCCEEDED":
        return []

    revisions = {r.revision: r for r in state.get("code_revisions") or []}
    cycles = context["debug_cycles"]
    points: list[dict[str, Any]] = []
    for cycle in cycles:
        if not cycle["resolved"]:
            continue
        before = revisions.get(cycle["revision"])
        after = revisions.get(cycle["fix_revision"]) if cycle["fix_revision"] else None
        summary = (
            cycle["fix_rationale"]
            or cycle["fix_strategy"]
            or "Fix applied; no summary was recorded."
        )
        points.append(
            {
                "run_id": context["run_id"],
                "task_kind": context["task_kind"],
                "outcome": context["status"],
                "error_fingerprint": cycle["fingerprint"],
                "error_excerpt": cycle["message"],
                "fix_summary": summary,
                "fix_diff": _unified_diff(before, after),
                "debug_iterations": len(cycles),
                "final_score": _final_score(context),
            }
        )
    return points


def _unified_diff(before: CodeRevision | None, after: CodeRevision | None) -> str:
    if before is None or after is None:
        return ""
    diff = difflib.unified_diff(
        before.content.splitlines(keepends=True),
        after.content.splitlines(keepends=True),
        fromfile=f"rev-{before.revision:03d}/main.py",
        tofile=f"rev-{after.revision:03d}/main.py",
    )
    # Budgeted, not maximal (principle P6): a diff is evidence for a future Debugger
    # prompt, not the whole program a second time.
    return "".join(diff)[:8000]


def _final_score(context: dict[str, Any]) -> float:
    primary = context.get("primary_metric")
    value = (context.get("metrics") or {}).get(primary) if primary else None
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else 0.0
    )


__all__ = ["fallback_report", "reporter_node"]
