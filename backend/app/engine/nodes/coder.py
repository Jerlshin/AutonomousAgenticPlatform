"""`coder` — emit one self-contained Python program obeying the sandbox I/O contract.

Specification: AGENTS.md §7.3. The Coder has no tools. A tool-calling loop competes with
code generation for a 7B model's attention and measurably degrades both; retrieval belongs
to the Researcher and execution to `sandbox_exec`. This node does exactly one thing.

Two post-generation checks run before the code leaves the node, both of which save a
container launch:

* **Byte-identical output.** If the model returns exactly the previous revision, the same
  error will repeat. It is re-prompted once with that fact.
* **The static gate.** A rejected import is a 30 ms round trip here versus a 60-second
  container launch and a wasted debug iteration.

**Revision mode** (§7.3) is what closes the correctness loop. When the Debugger has left a
`Diagnosis`, the prompt gains the previous program, the traceback, the failing source
region and the directive, and the instruction changes from "write this" to "make this one
targeted change". The distinction matters: a model handed only a diagnosis rewrites from
scratch and reintroduces bugs in the parts that already worked, which is how a debug loop
turns into a random walk.

**Refinement mode** (§6.2) is the same idea one level up, and it is the Coder's half of
loop 2. The program did not crash — it ran, produced real numbers, and missed a threshold —
so there is no traceback to work from and nothing to diagnose. What the prompt gains instead
is the Evaluator's `refine_directive`: the measured gap, in numbers, and the specific change
that should close it. The two modes are mutually exclusive and chosen from the *last
execution*, not from whether a stale `Diagnosis` is still sitting in state: after a clean
run, the diagnosis from three nodes ago describes a failure that no longer exists, and
handing it back would send the revision after a bug that is already fixed.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableConfig

from app.engine.criteria import format_criteria_table
from app.engine.nodes.base import FailurePolicy, get_chat_client, node
from app.engine.prompts import UNTRUSTED_PREAMBLE, load_prompt, wrap_untrusted
from app.engine.state import (
    AgentState,
    CodeRevision,
    Diagnosis,
    ErrorRecord,
    EvalDecision,
    Plan,
    PlanStep,
    RunPhase,
    StepKind,
    Usage,
    Verdict,
)
from app.engine.structured import call_text, extract_code_and_sidecar
from app.services.sandbox import profile_for, sha256_text
from app.services.validator import validate_source

logger = logging.getLogger(__name__)

NO_CONTEXT_NOTE = (
    "No retrieved context is available for this run. Stay on well-known scikit-learn and "
    "standard-library APIs you are certain of, and prefer the simplest construction that "
    "satisfies the criteria. Do not improvise parameter names."
)


class CoderError(RuntimeError):
    """The model produced nothing that could be run."""


@node(name="coder", phase=RunPhase.IMPLEMENT, policy=FailurePolicy.RETRY_THEN_REPORT)
async def coder_node(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
    plan: Plan | None = state.get("plan")
    if plan is None:
        raise CoderError(
            "the coder was reached with no plan; there is nothing to implement."
        )

    step = plan.step(state.get("current_step_id")) or plan.steps[0]
    llm = get_chat_client(config, "coder")
    prompt = load_prompt("coder")
    profile = profile_for("train" if step.kind is StepKind.TRAIN else "exec")

    system = prompt.render(
        untrusted_preamble=UNTRUSTED_PREAMBLE,
        cpus=profile.cpus,
        memory=profile.memory,
        timeout=profile.timeout_s,
        task_kind=plan.task_kind,
        step_block=_step_block(step, plan),
        success_criteria_table=format_criteria_table(plan.success_criteria),
        context_block=_context_block(state),
        revision_block=_revision_block(state),
    )
    user = (
        "Write main.py for the step above. Output one ```python block containing the "
        "complete file, then one ```json block with the sidecar."
    )

    previous = state.get("current_revision")
    revision_number = len(state.get("code_revisions") or []) + 1

    text, usage = await call_text(llm, system=system, user=user)
    code, sidecar = extract_code_and_sidecar(text)
    usage = _accumulate(Usage(), usage)

    retry_reason = _rejection_reason(code, previous, profile.name)
    if retry_reason is not None:
        logger.info("Re-prompting the coder: %s", retry_reason)
        text, retry_usage = await call_text(
            llm, system=system, user=f"{user}\n\n{retry_reason}"
        )
        retry_code, retry_sidecar = extract_code_and_sidecar(text)
        usage = _accumulate(usage, retry_usage)
        if retry_code:
            code, sidecar = retry_code, retry_sidecar

    if not code:
        raise CoderError("the model returned no Python code block.")

    # `last_error` outlives the revision that fixed it, so it only describes *this*
    # revision while the last execution is still a failure. On a refinement the previous
    # run was CLEAN and there is no fingerprint to answer.
    error: ErrorRecord | None = state.get("last_error") if _is_fixing(state) else None
    revision = CodeRevision(
        revision=revision_number,
        content=code,
        requirements=[str(r) for r in sidecar.get("requirements") or []],
        sha256=sha256_text(code),
        rationale=str(sidecar.get("rationale") or ""),
        # The fingerprint this revision is answering is a fact the graph holds, not one
        # the model needs to remember correctly. `debug_cycles` in the report pairs
        # revisions with errors by this field.
        addresses_error=(sidecar.get("addresses_error") or None)
        if error is None
        else error.fingerprint,
    )

    report = validate_source(code, profile=profile.name)
    if not report.passed:
        # Not fatal here: `sandbox_exec` is the authority on refusing to launch, and it
        # records the rejection as a VALIDATION_REJECTED outcome the run can report on.
        logger.info(
            "Revision %d fails static validation: %s",
            revision_number,
            report.rejections,
        )

    return {
        "current_revision": revision,
        "code_revisions": revision,
        "usage": usage,
        "messages": [
            AIMessage(
                content=f"Revision {revision_number} written ({len(code)} bytes)."
            )
        ],
        "metadata": {
            **(state.get("metadata") or {}),
            "prompt_version_coder": prompt.version,
        },
    }


def _rejection_reason(
    code: str | None, previous: CodeRevision | None, profile: str
) -> str | None:
    """Why the first attempt should be re-prompted, or None to accept it."""
    if not code:
        return (
            "Your previous response contained no ```python block. Return the complete "
            "main.py inside one ```python fenced block."
        )
    if previous is not None and sha256_text(code) == previous.sha256:
        return (
            "You returned byte-identical code to the previous revision. It failed for a "
            "reason that has not changed, so re-running it will fail identically. Make "
            "the actual change."
        )
    report = validate_source(code, profile=profile)
    if not report.passed:
        return (
            "Static validation rejected your code before it could run — no container was "
            "launched. Fix these and return the complete file again:\n"
            + "\n".join(f"- {reason}" for reason in report.rejections)
        )
    return None


def _step_block(step: PlanStep, plan: Plan) -> str:
    """The step the Coder is implementing, plus its dataset binding if it has one."""
    lines = [
        f"**{step.title}** (`{step.kind.value}`, step {step.index + 1} of {len(plan.steps)})",
        "",
        step.description,
    ]
    if step.acceptance:
        lines += ["", "Acceptance checks:"] + [
            f"- {check}" for check in step.acceptance
        ]
    if step.dataset is not None:
        binding = step.dataset
        lines += [
            "",
            "Bound dataset — read from THIS path and no other:",
            f"- path: `{binding.path}`",
            f"- dataset id: `{binding.dataset_id}` (copy verbatim into metrics.json)",
            f"- sha256: `{binding.sha256}` (copy verbatim into metrics.json)",
        ]
        if binding.target_column:
            lines.append(f"- target column: `{binding.target_column}`")
        if binding.n_samples:
            lines.append(f"- rows: {binding.n_samples}")
    else:
        lines += [
            "",
            "No dataset is bound to this step. Do not invent a file path, and do not call "
            "any loader that downloads data.",
        ]
    return "\n".join(lines)


def _context_block(state: AgentState) -> str:
    """Retrieved API reference, when a Researcher has produced any."""
    pack = state.get("context_pack")
    if pack is None or not (pack.api_signatures or pack.key_facts):
        return NO_CONTEXT_NOTE

    lines: list[str] = []
    if pack.api_signatures:
        lines += [
            "API signatures (verbatim from the corpus):",
            *pack.api_signatures,
            "",
        ]
    if pack.key_facts:
        lines += ["Key facts:", *(f"- {fact}" for fact in pack.key_facts)]
    if pack.sufficiency != "sufficient":
        lines += [
            "",
            f"Retrieval was {pack.sufficiency}. Gaps: "
            + "; ".join(pack.gaps or ["unspecified"]),
            "Stay on APIs you are certain of where the context is thin.",
        ]
    return wrap_untrusted("context_pack", "\n".join(lines))


def _is_fixing(state: AgentState) -> bool:
    """Whether the run is answering a failure rather than refining a working program."""
    outcome = state.get("last_outcome")
    return outcome is None or outcome.classification != "CLEAN"


def _revision_block(state: AgentState) -> str:
    """Why this is not the first attempt: a crash to fix, or a threshold to clear.

    Empty on the first attempt. The two modes are chosen from the last execution rather
    than from what happens to be left in state, because `last_diagnosis` and `last_error`
    persist after the revision that fixed them: a run that crashed, was fixed, ran cleanly
    and then missed a threshold still has both channels populated, and reading them would
    describe a failure that no longer exists.
    """
    previous: CodeRevision | None = state.get("current_revision")
    if previous is None:
        return ""

    if _is_fixing(state):
        return _debug_block(state, previous)
    return _refine_block(state, previous)


def _refine_block(state: AgentState, previous: CodeRevision) -> str:
    """The Evaluator's directive for a run that worked but was not good enough (§6.2).

    The measured gap comes from `refine_directive`, which the Evaluator assembles from
    `criteria_results` arithmetic rather than from model prose — so the number the Coder is
    told to beat is the number the criteria will actually be checked against.
    """
    verdict: Verdict | None = state.get("verdict")
    if verdict is None or verdict.decision is not EvalDecision.REFINE:
        return ""

    lines = [
        f"## The code ran, but missed the criteria — this is revision {previous.revision + 1}",
        "",
        "### Previous code",
        "",
        "```python",
        previous.content.rstrip(),
        "```",
        "",
        "### What the Evaluator measured",
        "",
        verdict.refine_directive or verdict.summary,
    ]

    if verdict.rubric:
        lines += ["", "Quality assessment of the previous revision:"]
        lines += [
            f"- {score.dimension} {score.score}/5 — {score.justification}"
            for score in verdict.rubric
        ]

    lines += [
        "",
        "### Rules for this revision",
        "",
        "1. The program WORKS. Do not rewrite it — change what the directive names and "
        "leave the rest alone.",
        "2. Keep the split, the seed and the evaluation protocol identical. Changing them "
        "makes the comparison with the previous attempt meaningless, and the comparison is "
        "the only evidence that the change helped.",
        "3. Output the COMPLETE file, not a diff or a fragment.",
        "4. Explain in `rationale` what you changed and why it should close the gap.",
        "5. Do NOT report a metric you did not compute. Missing the target honestly is a "
        "result; a fabricated number is not.",
    ]
    return "\n".join(lines)


def _debug_block(state: AgentState, previous: CodeRevision) -> str:
    """The fix directive, the previous program, and the failure it has to answer.

    Assembled here rather than left to the Debugger because the Coder needs three things
    the Debugger's `Diagnosis` deliberately does not carry — the code that failed, the
    traceback, and the failing source region — and duplicating them into the diagnosis
    would put the same bytes through two model contexts instead of one.
    """
    diagnosis: Diagnosis | None = state.get("last_diagnosis")
    error: ErrorRecord | None = state.get("last_error")
    if diagnosis is None or error is None:
        return ""

    lines = [
        f"## You are fixing a specific failure — this is revision {previous.revision + 1}",
        "",
        "### Previous code",
        "",
        "```python",
        previous.content.rstrip(),
        "```",
        "",
        "### What went wrong",
        "",
        f"Error kind: `{error.kind.value}` · Fingerprint: `{error.fingerprint}`",
        "",
        wrap_untrusted(
            "sandbox_stderr",
            error.traceback.strip() or error.message,
            trust="untrusted",
        ),
    ]

    if error.offending_source:
        lines += [
            "",
            f"### Failing source region (line {error.line})",
            "",
            "```python",
            error.offending_source,
            "```",
        ]

    lines += [
        "",
        "### Diagnosis",
        "",
        f"Root cause: {diagnosis.root_cause}",
        f"Fix strategy: {diagnosis.fix_strategy}",
        "",
        "Targeted changes:",
    ]
    lines += [f"- {change}" for change in diagnosis.targeted_changes]

    if diagnosis.prior_art:
        lines += ["", "Fixes that worked for this error in earlier runs:"]
        lines += [f"- {item}" for item in diagnosis.prior_art]

    if diagnosis.confidence < 0.4:
        lines += [
            "",
            f"The diagnosis is low-confidence ({diagnosis.confidence:.2f}). Read the "
            "traceback yourself and prefer your own reading of it where the two differ.",
        ]

    lines += [
        "",
        "### Rules for this revision",
        "",
        "1. Make the TARGETED change. Do not rewrite working code — every rewrite risks "
        "new bugs in parts that were already correct.",
        "2. Output the COMPLETE file, not a diff or a fragment.",
        f'3. Set `addresses_error` to "{error.fingerprint}" in the JSON sidecar.',
        "4. If you believe the diagnosis is wrong, implement your own fix and say so in "
        "`rationale`.",
    ]
    return "\n".join(lines)


def _accumulate(current: Usage, new: Usage) -> Usage:
    return Usage(
        tokens_in=current.tokens_in + new.tokens_in,
        tokens_out=current.tokens_out + new.tokens_out,
        llm_calls=current.llm_calls + new.llm_calls,
    )


__all__ = ["coder_node", "CoderError", "NO_CONTEXT_NOTE"]
