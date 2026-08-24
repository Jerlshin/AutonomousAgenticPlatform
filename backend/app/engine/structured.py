"""Schema-validated LLM output with a repair ladder (ARCHITECTURE.md §11.2).

Local 7–14B models are markedly worse at emitting valid JSON than frontier models, and a
node that returns unvalidated text into state violates design principle P2. Every
structured call therefore escalates through cheap repairs before giving up:

| Stage | Technique | Cost |
|---|---|---|
| 1 | Ollama's `format` parameter carrying the JSON Schema — constrained decoding, correct by construction where the server supports it | 1 call |
| 2 | Deterministic salvage: strip fences, take the outermost balanced object, repair trailing commas and single quotes | 0 calls |
| 3 | Re-prompt with the `ValidationError` verbatim plus the offending output — models fix their own schema errors reliably when shown the error | +1 call |
| 4 | Raise `StructuredOutputError`; the node applies its declared failure policy | — |

Every call in this module goes through `_invoke`, which streams the response through the
event bus as `token.delta` frames (§9.4) when a run is being watched and falls back to a
single `ainvoke` when it is not. The repair ladder is unaffected either way: it sees a
fully reassembled message, not a stream.

Stage 4 of the specification — field-wise extraction, one scalar at a time — is not
implemented. It costs +N calls to rescue a case stage 3 already handles in one, and until
`pluton_structured_output_attempts` shows stage 3 failing in practice there is nothing to
justify it.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from app.core import metrics
from app.engine import events
from app.engine.state import Usage

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json|python|py)?\s*\n(.*?)```", re.DOTALL)
_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


class StructuredOutputError(RuntimeError):
    """The model could not be made to produce output matching the schema."""

    def __init__(self, model_name: str, errors: str, raw: str) -> None:
        super().__init__(f"{model_name}: {errors}")
        self.model_name = model_name
        self.errors = errors
        self.raw = raw


class ChatClient(Protocol):
    """The slice of a LangChain chat model the engine actually uses.

    Narrow on purpose: nodes accept anything with this shape, which is what lets the whole
    graph be tested against a scripted fake with no model, no Ollama and no network.
    """

    async def ainvoke(self, input: list[BaseMessage], **kwargs: Any) -> AIMessage: ...


@dataclass
class LLMResult(Generic[T]):
    value: T
    usage: Usage
    attempts: int
    raw: str


def usage_from_response(message: BaseMessage) -> Usage:
    """Token counts from whichever field the provider populated.

    LangChain normalises to `usage_metadata`; Ollama reports `prompt_eval_count` and
    `eval_count` in `response_metadata`. Missing counts degrade to zero rather than
    guessing — an unbudgeted call is better than a fabricated budget.
    """
    meta = getattr(message, "usage_metadata", None) or {}
    tokens_in = meta.get("input_tokens")
    tokens_out = meta.get("output_tokens")

    if tokens_in is None or tokens_out is None:
        response_meta = getattr(message, "response_metadata", None) or {}
        tokens_in = (
            tokens_in
            if tokens_in is not None
            else response_meta.get("prompt_eval_count", 0)
        )
        tokens_out = (
            tokens_out if tokens_out is not None else response_meta.get("eval_count", 0)
        )

    return Usage(
        tokens_in=int(tokens_in or 0), tokens_out=int(tokens_out or 0), llm_calls=1
    )


def message_text(message: BaseMessage) -> str:
    """Response content as a string, flattening the content-block form."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return str(content)


def salvage_json(text: str) -> str | None:
    """Best-effort extraction of one JSON object from a model's prose.

    Handles the three ways local models mangle JSON output: wrapping it in a markdown
    fence, prefacing it with commentary, and leaving a trailing comma. Returns None when
    nothing object-shaped is present.
    """
    candidates: list[str] = []

    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)

    for candidate in candidates:
        block = _outermost_object(candidate)
        if block is None:
            continue
        for attempt in (block, _TRAILING_COMMA.sub(r"\1", block)):
            try:
                json.loads(attempt)
            except json.JSONDecodeError:
                continue
            return attempt
    return None


def _outermost_object(text: str) -> str | None:
    """The outermost balanced `{...}` span, ignoring braces inside string literals."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_code_and_sidecar(text: str) -> tuple[str | None, dict[str, Any]]:
    """Split the Coder's reply into its Python program and its JSON sidecar.

    The Coder's contract (AGENTS.md §7.3) is a ```python block followed by a ```json one.
    A missing or malformed sidecar is not fatal — the code is the deliverable, and losing
    the `rationale` is a far smaller loss than discarding a working program.
    """
    blocks = re.findall(r"```(python|py|json)?\s*\n(.*?)```", text, re.DOTALL)

    code: str | None = None
    sidecar: dict[str, Any] = {}
    for language, body in blocks:
        if language in ("python", "py") and code is None:
            code = body.strip("\n")
        elif language == "json":
            salvaged = salvage_json(body)
            if salvaged:
                parsed = json.loads(salvaged)
                if isinstance(parsed, dict):
                    sidecar = parsed

    if code is None and not blocks:
        # A reply that forgot the fence but is nonetheless valid Python is accepted. It
        # must parse: without that check, a model's refusal or apology becomes a
        # "revision" that reaches the sandbox as a guaranteed SyntaxError.
        stripped = text.strip()
        if stripped and _parses_as_python(stripped):
            code = stripped

    return code, sidecar


def _parses_as_python(text: str) -> bool:
    try:
        ast.parse(text)
    except SyntaxError:
        return False
    return True


def _with_json_format(llm: ChatClient, output_model: type[BaseModel]) -> ChatClient:
    """Stage 1: bind the JSON Schema for constrained decoding where it is supported."""
    bind = getattr(llm, "bind", None)
    if bind is None:
        return llm
    try:
        return bind(format=output_model.model_json_schema())
    except Exception:  # pragma: no cover - provider-specific
        logger.debug(
            "Chat client does not accept a `format` schema; falling back to prose JSON"
        )
        return llm


def _model_name(llm: ChatClient) -> str:
    """The model this client is bound to, for the `model` metric label.

    `ChatOllama` exposes it as `.model`; a bound runnable wraps that one level down. Both
    are read defensively — an unlabelled call is a gap in a dashboard, but a client whose
    attribute layout changed must not be able to raise out of the call it was decorating.
    """
    for candidate in (llm, getattr(llm, "bound", None)):
        name = getattr(candidate, "model", None)
        if isinstance(name, str) and name:
            return name
    return "unknown"


async def _invoke(llm: ChatClient, messages: list[BaseMessage]) -> BaseMessage:
    """One model call, streamed to the event bus when anything is listening.

    Streaming is conditional on two things, and both matter. There must be an ambient
    emitter — otherwise nobody would see the tokens and streaming would only add
    per-chunk overhead to a call whose result is used whole. And the client must expose
    `astream` — `FakeChatModel` does not, which is what keeps the entire test suite on
    the deterministic `ainvoke` path rather than making every node test also a test of
    chunk reassembly.

    The reassembled message is what the caller gets either way, so `call_structured`'s
    repair ladder and `usage_from_response` see exactly the same object in both modes.

    This is also the single choke point for §12.1's LLM metrics. Instrumenting here rather
    than at each node is what makes "every model call is counted" structural: a node added
    later cannot forget to, because there is no other way to reach a model.
    """
    started = time.perf_counter()
    model = _model_name(llm)
    # The ambient node name is the role: for every LLM-driven node the two are the same
    # word, and it is already bound by the `@node` envelope, so no call site has to pass it.
    role = events.current_node()
    ttft: float | None = None
    try:
        emitter = events.current_emitter()
        astream = getattr(llm, "astream", None)
        if emitter is None or astream is None:
            response = await llm.ainvoke(messages)
        else:
            node = role
            chunks: list[Any] = []
            async for chunk in astream(messages):
                chunks.append(chunk)
                text = message_text(chunk)
                if text:
                    # Only a chunk carrying text is a *token*; providers emit leading
                    # metadata-only chunks, and timing to one of those would report a TTFT
                    # the user never saw anything for.
                    if ttft is None:
                        ttft = time.perf_counter() - started
                    await emitter.emit_token(node or "unknown", text)

            if chunks:
                merged = chunks[0]
                for chunk in chunks[1:]:
                    merged = merged + chunk
                response = merged
            else:
                # A stream that yielded nothing is a provider quirk, not a model with
                # nothing to say. Falling back keeps one empty stream from failing a node.
                ttft = None
                response = await llm.ainvoke(messages)
    except Exception:
        metrics.record_llm_call(
            model=model,
            role=role,
            outcome="error",
            duration_s=time.perf_counter() - started,
        )
        raise

    usage = usage_from_response(response)
    metrics.record_llm_call(
        model=model,
        role=role,
        outcome="ok",
        duration_s=time.perf_counter() - started,
        tokens_in=usage.tokens_in,
        tokens_out=usage.tokens_out,
        ttft_s=ttft,
    )
    return response


async def call_structured(
    llm: ChatClient,
    *,
    output_model: type[T],
    system: str,
    user: str,
    max_repairs: int = 1,
) -> LLMResult[T]:
    """Call `llm` and return a validated `output_model`, repairing as needed."""
    constrained = _with_json_format(llm, output_model)
    messages: list[BaseMessage] = [
        SystemMessage(content=system),
        HumanMessage(content=user),
    ]

    usage = Usage()
    raw = ""
    last_errors = "no response"

    for attempt in range(max_repairs + 1):
        response = await _invoke(constrained, messages)
        usage = _merge(usage, usage_from_response(response))
        raw = message_text(response)

        payload = salvage_json(raw)
        if payload is not None:
            try:
                value = output_model.model_validate_json(payload)
            except ValidationError as exc:
                last_errors = _format_errors(exc)
            else:
                metrics.record_structured_output(
                    events.current_node(),
                    "constrained" if attempt == 0 else "repair",
                    attempt + 1,
                )
                return LLMResult(
                    value=value, usage=usage, attempts=attempt + 1, raw=raw
                )
        else:
            last_errors = "the response contained no JSON object."

        if attempt == max_repairs:
            break

        logger.info(
            "Structured output for %s failed validation on attempt %d; repairing",
            output_model.__name__,
            attempt + 1,
        )
        messages = [
            SystemMessage(content=system),
            HumanMessage(content=user),
            AIMessage(content=raw),
            HumanMessage(content=_repair_prompt(output_model, last_errors)),
        ]

    metrics.record_structured_output(
        events.current_node(), "exhausted", max_repairs + 1
    )
    raise StructuredOutputError(output_model.__name__, last_errors, raw)


async def call_text(llm: ChatClient, *, system: str, user: str) -> tuple[str, Usage]:
    """A plain completion, for nodes whose deliverable is prose or code rather than JSON."""
    response = await _invoke(
        llm, [SystemMessage(content=system), HumanMessage(content=user)]
    )
    return message_text(response), usage_from_response(response)


def _format_errors(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors()
    )


def _repair_prompt(output_model: type[BaseModel], errors: str) -> str:
    return (
        "Your previous response did not match the required schema.\n\n"
        f"Validation errors:\n{errors}\n\n"
        "Return the corrected JSON object and nothing else — no prose, no markdown fence. "
        "It must satisfy this schema:\n"
        f"{json.dumps(output_model.model_json_schema(), indent=2)}"
    )


def _merge(current: Usage, new: Usage) -> Usage:
    return Usage(
        tokens_in=current.tokens_in + new.tokens_in,
        tokens_out=current.tokens_out + new.tokens_out,
        llm_calls=current.llm_calls + new.llm_calls,
    )
