"""Structured logging and secret redaction (ARCHITECTURE.md §12.3, §13.1 T10).

Redaction is a security control, so it is tested the way a control is tested: not "does
the happy path redact" but "is there a shape of log record through which a secret can
still reach stdout". The three that matter are a key whose *name* looks secret, a value
that *is* the configured secret appearing anywhere in a message, and an exception whose
formatted traceback carries one — the last being the one key-matching alone would miss.

Every test drives the real pipeline and asserts on the JSON line that lands on stdout,
because that is what a log shipper reads. Asserting on an intermediate event dict would
pass while a processor further down the chain reintroduced the value.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest
import structlog

from app.core import logging as app_logging
from app.core.config import settings

TOKEN = "sk-live-9f3c2b7a41ee4c0f8d6b5a2e1c0d9f8e"
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch):
    """Configure the real pipeline against a buffer, and return a reader for its lines.

    The pipeline writes to a `StringIO` rather than to stdout so the assertions read the
    exact bytes a log shipper would, without depending on pytest's capture — which
    snapshots `sys.stdout` per test phase, so a handler bound during fixture setup writes
    somewhere the test body cannot read.
    """
    monkeypatch.setattr(settings, "PLATFORM_API_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", PASSWORD)
    monkeypatch.setattr(settings, "LOG_FORMAT", "json")
    buffer = io.StringIO()
    app_logging.configure_logging(force=True, stream=buffer)
    app_logging.clear_run_context()

    def read() -> list[dict[str, Any]]:
        lines = [
            line for line in buffer.getvalue().splitlines() if line.startswith("{")
        ]
        buffer.seek(0)
        buffer.truncate()
        return [json.loads(line) for line in lines]

    try:
        yield read
    finally:
        app_logging.clear_run_context()
        structlog.reset_defaults()
        logging.getLogger().handlers.clear()


# ------------------------------------------------------------------------------------
#  Shape
# ------------------------------------------------------------------------------------


def test_stdlib_records_are_rendered_as_json(emitted) -> None:  # noqa: ANN001
    """The rest of the codebase logs with `logging`; those records must land in one shape."""
    logging.getLogger("demo").info("node.started", extra={"node": "coder"})

    (record,) = emitted()
    assert record["event"] == "node.started"
    assert record["level"] == "info"
    assert record["node"] == "coder"
    assert record["timestamp"].endswith("Z")


def test_structlog_events_land_in_the_same_shape(emitted) -> None:  # noqa: ANN001
    app_logging.get_logger("demo").warning("budget.warning", spent=0.8)

    (record,) = emitted()
    assert record["event"] == "budget.warning"
    assert record["level"] == "warning"
    assert record["spent"] == 0.8


def test_run_context_is_merged_into_every_record(emitted) -> None:  # noqa: ANN001
    """§12.3: no call site passes `run_id`; the processor does."""
    app_logging.bind_run_context(run_id="r-1", node="planner", worker_id="w-9")
    logging.getLogger("demo").info("first")
    app_logging.get_logger("demo").info("second")

    first, second = emitted()
    for record in (first, second):
        assert record["run_id"] == "r-1"
        assert record["node"] == "planner"
        assert record["worker_id"] == "w-9"


def test_binding_ignores_none_so_an_absent_step_is_not_a_null_key(emitted) -> None:  # noqa: ANN001
    app_logging.bind_run_context(run_id="r-2", step_id=None)
    logging.getLogger("demo").info("hello")

    (record,) = emitted()
    assert record["run_id"] == "r-2"
    assert "step_id" not in record


def test_unbinding_drops_node_scope_but_keeps_the_run(emitted) -> None:  # noqa: ANN001
    """The split the `@node` envelope depends on: nodes come and go, the run does not."""
    app_logging.bind_run_context(
        run_id="r-3", node="coder", agent="Coder", step_id="s1"
    )
    app_logging.unbind_run_context("node", "agent", "step_id")
    logging.getLogger("demo").info("between nodes")

    (record,) = emitted()
    assert record["run_id"] == "r-3"
    assert "node" not in record
    assert "agent" not in record


def test_clearing_the_context_leaves_nothing_behind(emitted) -> None:  # noqa: ANN001
    app_logging.bind_run_context(run_id="r-4", worker_id="w-1")
    app_logging.clear_run_context()
    logging.getLogger("demo").info("after the run")

    (record,) = emitted()
    assert "run_id" not in record
    assert "worker_id" not in record


# ------------------------------------------------------------------------------------
#  Redaction by key  (§12.3's pattern)
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "POSTGRES_PASSWORD",
        "db_password",
        "token",
        "PLATFORM_API_TOKEN",
        "refresh_token",
        "secret",
        "SECRET_KEY",
        "client_secret",
        "api_key",
        "apiKey",
        "X-Api-Key",
        "api-key",
        "authorization",
        "Authorization",
    ],
)
def test_a_secret_looking_key_is_redacted_whatever_its_value(emitted, key: str) -> None:  # noqa: ANN001
    """Matched as a substring, so a field named later is caught without editing a list."""
    app_logging.get_logger("demo").info("request", **{key: "anything at all"})

    (record,) = emitted()
    assert record[key] == app_logging.REDACTED


def test_redaction_reaches_into_nested_payloads(emitted) -> None:  # noqa: ANN001
    """Log payloads are structures, and a secret one level down is still a secret."""
    app_logging.get_logger("demo").info(
        "outbound",
        request={"url": "http://x", "headers": {"Authorization": "Bearer abc"}},
        candidates=[{"api_key": "k1"}, {"api_key": "k2"}],
    )

    (record,) = emitted()
    assert record["request"]["headers"]["Authorization"] == app_logging.REDACTED
    assert record["request"]["url"] == "http://x"
    assert [c["api_key"] for c in record["candidates"]] == [app_logging.REDACTED] * 2


def test_an_innocent_key_is_left_alone(emitted) -> None:  # noqa: ANN001
    """A redactor that eats ordinary fields gets turned off, which is the real failure."""
    app_logging.get_logger("demo").info(
        "run", run_id="r-5", accuracy=0.97, node="coder"
    )

    (record,) = emitted()
    assert record["run_id"] == "r-5"
    assert record["accuracy"] == 0.97


# ------------------------------------------------------------------------------------
#  Redaction by value  (§13.1 T10)
# ------------------------------------------------------------------------------------


def test_a_configured_secret_is_scrubbed_out_of_a_message(emitted) -> None:  # noqa: ANN001
    """Key matching alone would let this through — there is no key to match on."""
    logging.getLogger("demo").warning("upstream rejected %s", TOKEN)

    (record,) = emitted()
    assert TOKEN not in json.dumps(record)
    assert app_logging.REDACTED in record["event"]


def test_a_configured_secret_is_scrubbed_out_of_a_traceback(emitted) -> None:  # noqa: ANN001
    """The realistic leak: a driver puts the DSN in the exception it raises."""
    try:
        raise ConnectionError(
            f"password authentication failed: postgresql://postgres:{PASSWORD}@db/x"
        )
    except ConnectionError:
        logging.getLogger("demo").exception("db.connect.failed")

    (record,) = emitted()
    assert PASSWORD not in json.dumps(record)
    assert app_logging.REDACTED in record["exception"]


def test_a_secret_nested_in_a_payload_value_is_scrubbed(emitted) -> None:  # noqa: ANN001
    app_logging.get_logger("demo").info(
        "config", env={"DSN": f"postgres://u:{PASSWORD}@h/d"}
    )

    (record,) = emitted()
    assert PASSWORD not in json.dumps(record)


def test_short_placeholder_secrets_are_not_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise a three-character development password mangles unrelated prose.

    `make init-secrets` writes 43-character values, so nothing real is exempted — and the
    configurations where a short value is in use are the ones §13.2 already refuses to
    expose beyond loopback.
    """
    monkeypatch.setattr(settings, "PLATFORM_API_TOKEN", "abc")
    monkeypatch.setattr(settings, "POSTGRES_PASSWORD", "x")
    monkeypatch.setattr(settings, "SECRET_KEY", "dev")

    assert app_logging.secret_values() == ()


def test_the_redaction_pattern_is_the_one_the_specification_states() -> None:
    """§12.3 states the pattern verbatim; a drift here is a drift from the spec."""
    assert app_logging.SECRET_KEY_PATTERN.pattern == (
        r"(?i)(password|token|secret|api[_-]?key|authorization)"
    )


def test_the_walk_is_depth_bounded(emitted) -> None:  # noqa: ANN001
    """A pathological structure must not turn a log call into a hang."""
    payload: dict[str, Any] = {"password": "leaf"}
    for _ in range(50):
        payload = {"nested": payload}

    app_logging.get_logger("demo").info("deep", payload=payload)
    assert emitted()  # it returned at all, which is the assertion


# ------------------------------------------------------------------------------------
#  Configuration
# ------------------------------------------------------------------------------------


def test_configure_logging_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`uvicorn --reload` re-imports the app on every edit; handlers must not accumulate."""
    monkeypatch.setattr(app_logging, "_configured", False)
    app_logging.configure_logging(force=True)
    handlers = len(logging.getLogger().handlers)

    app_logging.configure_logging()
    app_logging.configure_logging()

    assert len(logging.getLogger().handlers) == handlers


def test_console_format_selects_the_human_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "LOG_FORMAT", "console")
    assert isinstance(app_logging._renderer(), structlog.dev.ConsoleRenderer)

    monkeypatch.setattr(settings, "LOG_FORMAT", "json")
    assert isinstance(app_logging._renderer(), structlog.processors.JSONRenderer)
