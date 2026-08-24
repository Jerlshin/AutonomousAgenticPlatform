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
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.engine.nodes.base import FailurePolicy, get_chat_client, node
from app.engine.prompts import UNTRUSTED_PREAMBLE, load_prompt, wrap_untrusted
from app.engine.reporting import (
    assemble_report,
    criteria_table,
    render_report,
    report_context,
)
from app.engine.state import AgentState, RunPhase
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
    """The debugging narrative, pre-assembled so section 4 cannot omit an attempt."""
    cycles = context["debug_cycles"]
    if not cycles:
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
    return "\n".join(lines)


def _artifacts_block(context: dict[str, Any]) -> str:
    if not context["artifacts"]:
        return "### Artifacts\n\nNone were produced."
    listing = "\n".join(
        f"- `{a['name']}` ({a['type']}, {a['size']})" for a in context["artifacts"]
    )
    return f"### Artifacts\n\n{listing}\n\nThe artifact table is appended for you."


__all__ = ["fallback_report", "reporter_node"]
