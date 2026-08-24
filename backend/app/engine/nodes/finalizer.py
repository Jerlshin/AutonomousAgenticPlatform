"""`finalizer` — the sole edge into END (AGENTS.md §7.9).

Determines the run outcome, assembles the downloadable bundle, writes the deliverables
manifest and closes the run out. Its failure policy is `BEST_EFFORT`: every persistence
step is independently guarded, because a run that produced a trained model and valid
metrics must not lose them to a zip that could not be written.

The outcome is computed, not judged. `engine.criteria.determine_outcome` does the
arithmetic over `metrics.json`, and the Reporter reads the same function, so "did this run
succeed" has exactly one definition and the report cannot contradict the API.

Current scope: the run volume. Persisting `artifacts` rows, updating `tasks.status`,
emitting `run.completed` and releasing the Redis run lock arrive with the repository and
event layers; the hooks belong here and nowhere else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import zipfile
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig

from app.engine.criteria import COMPARATOR_SYMBOLS, determine_outcome
from app.engine.errors import ERROR_KIND_HINTS
from app.engine.nodes.base import FailurePolicy, get_sandbox, node
from app.engine.state import (
    AgentState,
    CriterionResult,
    Deliverable,
    DeliverableType,
    Plan,
    RunOutcome,
    RunPhase,
    StepStatus,
)
from app.services.sandbox import sha256_file

logger = logging.getLogger(__name__)

BUNDLE_NAME = "bundle.zip"
MANIFEST_NAME = "deliverables.json"
SUMMARY_NAME = "SUMMARY.md"
REPORT_NAME = "REPORT.md"


@node(name="finalizer", phase=RunPhase.COMPLETE, policy=FailurePolicy.BEST_EFFORT)
async def finalizer_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    # Zipping a fitted model is real, blocking I/O; keeping it off the event loop matters
    # because the worker runs several graphs concurrently.
    return await asyncio.to_thread(finalise_run, state, get_sandbox(config))


def finalise_run(state: AgentState, driver: Any) -> dict[str, Any]:
    """The finalizer's body, synchronous so its file I/O can be threaded off."""
    outcome, criteria_results = determine_outcome(state)
    final_dir: Path = driver.final_dir(state["run_id"])

    update: dict[str, Any] = {"outcome": outcome}
    new_deliverables: list[Deliverable] = []

    try:
        final_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Nothing further can be written, but the outcome is still a real result and the
        # deliverables already registered by sandbox_exec are still on disk.
        logger.error(
            "Could not create the final directory for run %s: %s", state["run_id"], exc
        )
        return update

    summary = render_summary(state, outcome, criteria_results)
    for name, content in (
        (SUMMARY_NAME, summary),
        (REPORT_NAME, state.get("report_markdown")),
    ):
        if not content:
            continue
        try:
            path = final_dir / name
            path.write_text(content, encoding="utf-8")
            new_deliverables.append(_deliverable(path, "report"))
        except OSError as exc:
            logger.error(
                "Could not write %s for run %s: %s", name, state["run_id"], exc
            )

    existing = list(state.get("deliverables") or [])
    bundle_path = final_dir / BUNDLE_NAME
    try:
        _write_bundle(bundle_path, state, existing + new_deliverables, driver)
    except (OSError, zipfile.BadZipFile) as exc:
        logger.error(
            "Could not assemble %s for run %s: %s", BUNDLE_NAME, state["run_id"], exc
        )
    else:
        new_deliverables.append(_deliverable(bundle_path, "bundle"))

    try:
        manifest_path = final_dir / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(
                _manifest(state, outcome, existing + new_deliverables), indent=2
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.error(
            "Could not write %s for run %s: %s", MANIFEST_NAME, state["run_id"], exc
        )

    if new_deliverables:
        update["deliverables"] = new_deliverables

    logger.info(
        "run.completed", extra={"run_id": state.get("run_id"), "outcome": outcome.value}
    )
    return update


def render_summary(
    state: AgentState,
    outcome: RunOutcome,
    criteria_results: list[CriterionResult],
) -> str:
    """A deterministic, factual account of the run.

    Distinct from `REPORT.md`, which the Reporter writes for a human to read. This is the
    operator's view: classification, exit code, fingerprints, budget spend — the facts you
    want when a run behaved oddly and the narrative is not what you are after.
    """
    plan: Plan | None = state.get("plan")
    last = state.get("last_outcome")
    usage = state.get("usage")
    lines = [
        f"# Run {state.get('run_id', 'unknown')}",
        "",
        f"**Outcome:** {outcome.value}  ",
        f"**Prompt:** {state.get('prompt', '')}",
        "",
        "## Plan",
    ]

    if plan is None:
        lines.append("No plan was produced — the run ended before planning completed.")
    else:
        status = state.get("step_status") or {}
        lines.append(
            f"Task kind `{plan.task_kind}`, primary metric `{plan.primary_metric}`."
        )
        lines.append("")
        lines += [
            f"{step.index + 1}. **{step.title}** (`{step.kind.value}`) — "
            f"{status.get(step.id, StepStatus.PENDING).value}"
            for step in plan.steps
        ]
        if plan.assumptions:
            lines += ["", "Assumptions:"] + [f"- {a}" for a in plan.assumptions]

    lines += ["", "## Criteria"]
    if criteria_results:
        lines += ["| Criterion | Target | Achieved | Status |", "|---|---|---|---|"]
        for result in criteria_results:
            symbol = COMPARATOR_SYMBOLS.get(result.comparator, result.comparator)
            observed = (
                "not measured" if result.observed is None else f"{result.observed:.4f}"
            )
            mark = (
                "Pass" if result.passed else ("Miss" if not result.required else "FAIL")
            )
            note = f" — {result.note}" if result.note else ""
            lines.append(
                f"| {result.metric} | {symbol} {result.threshold:g} | {observed} | {mark}{note} |"
            )
    else:
        lines.append("No criteria were evaluated — the run produced no metrics.")

    lines += ["", "## Execution"]
    if last is None:
        lines.append("No sandbox execution took place.")
    else:
        lines += [
            f"- Classification: `{last.classification}`",
            f"- Exit code: {last.exit_code}",
            f"- Duration: {last.duration_ms} ms",
            f"- Profile: `{last.profile}`, revision {last.revision}",
        ]
        if last.validation.rejections:
            lines += ["- Static validation rejections:"] + [
                f"  - {reason}" for reason in last.validation.rejections
            ]

    error = state.get("last_error")
    if error is not None:
        lines += [
            "",
            "## What went wrong",
            f"- Kind: `{error.kind.value}`",
            f"- Fingerprint: `{error.fingerprint}`",
            f"- Message: {error.message}",
        ]
        hint = ERROR_KIND_HINTS.get(error.kind)
        if hint:
            lines += ["", f"**Next step:** {hint}"]
        if error.traceback:
            lines += ["", "```", error.traceback.strip()[-2000:], "```"]

    deliverables = state.get("deliverables") or []
    lines += ["", "## Artifacts"]
    if deliverables:
        lines += ["| File | Type | Bytes | SHA-256 |", "|---|---|---|---|"]
        lines += [
            f"| `{d.name}` | {d.artifact_type} | {d.size_bytes} | `{d.sha256[:16]}…` |"
            for d in deliverables
        ]
    else:
        lines.append("No artifacts were produced.")

    if usage is not None:
        lines += [
            "",
            "## Budget",
            # The finalizer's own visit is folded in by the @node decorator after this
            # renders, so it is added here rather than reported one short.
            f"- Node visits: {usage.node_visits + 1}",
            f"- LLM calls: {usage.llm_calls} ({usage.tokens_in} in / {usage.tokens_out} out)",
            f"- Sandbox executions: {usage.sandbox_executions}",
            f"- Elapsed: {usage.elapsed_seconds:.1f}s",
        ]

    return "\n".join(lines) + "\n"


def _write_bundle(
    bundle_path: Path,
    state: AgentState,
    deliverables: list[Deliverable],
    driver: Any,
) -> None:
    """One downloadable file with the code, the logs and every artifact.

    Laid out to match the MLflow artifact structure (MLOPS.md §6), so a path means the
    same thing whether it is read from the bundle or from the tracking server.
    """
    revision = state.get("current_revision")
    workdir: Path | None = None
    if revision is not None:
        workdir = driver.revision_dir(state["run_id"], revision.revision)

    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if revision is not None:
            archive.writestr("code/main.py", revision.content)
        if workdir is not None:
            for name in ("stdout.log", "stderr.log"):
                source = workdir / name
                if source.is_file():
                    archive.write(source, f"logs/{name}")

        seen: set[str] = set()
        for deliverable in deliverables:
            source = Path(deliverable.path)
            # The bundle is not itself a member of the bundle.
            if not source.is_file() or source.resolve() == bundle_path.resolve():
                continue
            arcname = f"artifacts/{deliverable.name}"
            if deliverable.artifact_type in ("report", "bundle"):
                arcname = deliverable.name
            if arcname in seen:
                continue
            seen.add(arcname)
            archive.write(source, arcname)


def _manifest(
    state: AgentState, outcome: RunOutcome, deliverables: list[Deliverable]
) -> dict[str, Any]:
    last = state.get("last_outcome")
    return {
        "schema_version": "1.0",
        "run_id": state.get("run_id"),
        "task_id": state.get("task_id"),
        "outcome": outcome.value,
        "prompt": state.get("prompt"),
        "task_kind": state.get("task_kind"),
        "classification": last.classification if last else None,
        "metrics": last.metrics if last else None,
        "deliverables": [d.model_dump(mode="json") for d in deliverables],
    }


def _deliverable(path: Path, artifact_type: DeliverableType) -> Deliverable:
    return Deliverable(
        name=path.name,
        artifact_type=artifact_type,
        path=str(path),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )
