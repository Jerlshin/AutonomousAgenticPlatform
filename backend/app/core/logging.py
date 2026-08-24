"""Structured logging — JSON to stdout, with redaction (`docs/ARCHITECTURE.md` §12.3, §13.3).

Two things make this module worth having rather than a `logging.basicConfig` call.

**Context propagation.** Every record must carry `run_id`, `step_id`, `node`, `agent` and
`worker_id`. Threading those through every call site would mean every function in the
engine grows five parameters it does not use, so they are bound once per node into
`structlog.contextvars` and merged into each record by a processor. `bind_run_context` is
the one function that sets them, which is also what stops half the codebase inventing its
own key names for the same value.

**Redaction is a control, not a convenience.** §13.1 T10 is "secrets leaking into logs,
MLflow tags, or artifacts", and the mitigation is this processor. It works two ways, and
both are needed: by *key*, because `{"authorization": "Bearer …"}` is a secret whatever
the value looks like; and by *value*, because a token pasted into an exception message
(`asyncpg.InvalidPasswordError: password authentication failed for "postgres_password_dev"`)
has no key to match on. Key matching alone would have let that line through.

**Everything goes through one pipeline.** The rest of the codebase logs with the standard
library — `logging.getLogger(__name__)` — and rewriting several thousand call sites to use
`structlog.get_logger` would be churn for no gain. `configure_logging` installs
structlog's `ProcessorFormatter` on the root handler instead, so a stdlib record and a
structlog event land in stdout in the same shape, redacted by the same processor. A sink
that only covers the calls someone remembered to migrate is not a control.
"""

from __future__ import annotations

import logging
import logging.config
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

from app.core.config import settings

# §12.3's key pattern, verbatim. Matched as a *substring* so `X-Api-Key`, `db_password`
# and `refresh_token` are all caught — an exact-match list would need extending every time
# somebody names a field.
SECRET_KEY_PATTERN = re.compile(
    r"(?i)(password|token|secret|api[_-]?key|authorization)"
)

REDACTED = "«redacted»"

# Below this length a "secret" is either a placeholder or short enough that redacting every
# occurrence of it would mangle unrelated prose. `make init-secrets` generates 43-character
# values, so nothing real is exempted by this.
MIN_SECRET_VALUE_LEN = 8

# How deep a payload is walked before the redactor gives up. Log payloads are shallow by
# construction; the bound exists so a cyclic or pathological structure cannot turn a log
# call into a hang.
MAX_REDACTION_DEPTH = 6

# The context keys §12.3 requires on every record.
RUN_CONTEXT_KEYS = ("run_id", "step_id", "node", "agent", "worker_id")

_configured = False


def secret_values() -> tuple[str, ...]:
    """The configured secrets, for value-level redaction.

    Read from `settings` on each call rather than snapshotted at import: the test suite and
    `make dev` both mutate configuration after import, and a redactor holding a stale token
    is one that stops redacting the live one.
    """
    candidates = (
        settings.PLATFORM_API_TOKEN,
        settings.POSTGRES_PASSWORD,
        settings.SECRET_KEY,
    )
    return tuple(
        value
        for value in candidates
        if isinstance(value, str) and len(value) >= MIN_SECRET_VALUE_LEN
    )


def _scrub_text(text: str, secrets: tuple[str, ...]) -> str:
    """Replace any configured secret appearing inside a string."""
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def _redact_value(value: Any, secrets: tuple[str, ...], depth: int) -> Any:
    """Walk a value, scrubbing secret-looking keys and any literal secret it contains."""
    if depth > MAX_REDACTION_DEPTH:
        return value
    if isinstance(value, str):
        return _scrub_text(value, secrets) if secrets else value
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and SECRET_KEY_PATTERN.search(key)
                else _redact_value(item, secrets, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        cleaned = [_redact_value(item, secrets, depth + 1) for item in value]
        return (
            type(value)(cleaned) if isinstance(value, (list, tuple)) else set(cleaned)
        )
    return value


def redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor implementing §12.3's redaction rule.

    Placed last but one in the chain — after the event has been fully assembled and before
    it is rendered — so it sees exception text, positional formatting and every key any
    earlier processor added. A redactor that ran early would miss whatever came after it.
    """
    secrets = secret_values()
    redacted: dict[str, Any] = {}
    for key, value in event_dict.items():
        if isinstance(key, str) and SECRET_KEY_PATTERN.search(key):
            redacted[key] = REDACTED
        else:
            redacted[key] = _redact_value(value, secrets, 0)
    return redacted


def _renderer() -> Any:
    """JSON for shipping, coloured key-values for reading (`LOG_FORMAT`)."""
    if settings.LOG_FORMAT == "console":
        return structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    return structlog.processors.JSONRenderer()


# Shared by structlog events and by stdlib records forwarded through ProcessorFormatter,
# so the two cannot drift in shape or in what they redact.
def _shared_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        redact_processor,
    ]


def configure_logging(*, force: bool = False, stream: Any | None = None) -> None:
    """Install the logging pipeline. Idempotent; call once per process at startup.

    Idempotence matters more than it looks: `uvicorn --reload` re-imports the application
    module on every edit, and appending a second handler to the root logger each time is
    how a development box ends up printing every line four times.

    `stream` overrides the destination, which defaults to stdout. Its purpose is to let a
    caller — the test suite, principally — read exactly the bytes a log shipper would,
    rather than assert on an intermediate event dict that a later processor could still
    reintroduce a secret into.
    """
    global _configured
    if _configured and not force:
        return

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    structlog.configure(
        processors=[
            *_shared_processors(),
            # Hands the event dict to ProcessorFormatter rather than rendering it here, so
            # structlog events and stdlib records converge on one renderer below.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # `foreign_pre_chain` is what a *stdlib* record passes through on its way in. It
        # gets the same processors, redaction included — that is the whole point.
        #
        # `ExtraAdder` is load-bearing rather than decorative: the engine logs with
        # `logger.info("node.started", extra={"run_id": …, "node": …})`, and without it
        # those keys are set on the LogRecord and then dropped on the floor. `redact` runs
        # after it and after `format_exc_info`, so it sees both the extras and the rendered
        # traceback — the two places a secret actually shows up.
        foreign_pre_chain=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.ExtraAdder(),
            structlog.processors.add_log_level,
            timestamper,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_processor,
        ],
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _renderer(),
        ],
    )

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.LOG_LEVEL.upper())

    # uvicorn installs its own handlers on these three and would otherwise print every
    # request twice: once in its own format, once through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True

    _configured = True


def get_logger(name: str | None = None) -> Any:
    """A bound structlog logger. Equivalent to `logging.getLogger` for new call sites."""
    return structlog.get_logger(name)


def bind_run_context(**values: Any) -> None:
    """Bind §12.3's context vars for the current task. Unset keys are left alone.

    Bound with `structlog.contextvars`, which is task-local: two runs executing
    concurrently on the same worker each see their own `run_id`, and neither can read the
    other's. A module global would interleave them.
    """
    structlog.contextvars.bind_contextvars(
        **{k: v for k, v in values.items() if v is not None}
    )


def unbind_run_context(*keys: str) -> None:
    """Drop specific context vars, defaulting to everything §12.3 defines."""
    structlog.contextvars.unbind_contextvars(*(keys or RUN_CONTEXT_KEYS))


def clear_run_context() -> None:
    """Drop every bound context var. Called when a run ends, so the next one starts clean."""
    structlog.contextvars.clear_contextvars()


__all__ = [
    "MIN_SECRET_VALUE_LEN",
    "REDACTED",
    "RUN_CONTEXT_KEYS",
    "SECRET_KEY_PATTERN",
    "bind_run_context",
    "clear_run_context",
    "configure_logging",
    "get_logger",
    "redact_processor",
    "secret_values",
    "unbind_run_context",
]
