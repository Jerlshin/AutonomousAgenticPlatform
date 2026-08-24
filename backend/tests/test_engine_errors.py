"""The error pipeline: traceback parsing, fingerprinting, hints (AGENTS.md §7.4).

The fingerprint is the load-bearing part of this module, so most of these tests are about
one property: two incidents of the same bug must collapse to one identity, and two
different bugs must not. Get it too loose and every `ValueError` in the system looks like
the same failure, so the stagnation guard fires on genuine progress. Get it too tight and
the same bug looks new every time, so the guard never fires at all and the loop thrashes
until the budget is gone.
"""

from __future__ import annotations

import pytest

from app.engine.errors import (
    ERROR_KIND_HINTS,
    classify_exception,
    consecutive_repeats,
    error_from_traceback,
    error_kind_hint,
    fingerprint,
    normalize_message,
    parse_traceback,
    source_region,
    synthetic_error,
    validation_error,
)
from app.engine.state import (
    DatasetBinding,
    ErrorKind,
    ErrorRecord,
    SandboxOutcome,
    ValidationReport,
)

KEY_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "/workspace/main.py", line 23, in main\n'
    "    y = df['target']\n"
    '  File "/usr/lib/python3.11/site-packages/pandas/core/frame.py", line 3761, in __getitem__\n'
    "    indexer = self.columns.get_loc(key)\n"
    "KeyError: 'target'\n"
)

SHAPE_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "/workspace/main.py", line 40, in <module>\n'
    "    model.fit(X, y)\n"
    "ValueError: Found input variables with inconsistent numbers of samples: [455, 114]\n"
)


class TestParseTraceback:
    def test_the_reported_frame_is_the_last_one_in_main_py(self):
        """A failure inside pandas is caused by the line of generated code that called it."""
        exc_type, message, file_name, line = parse_traceback(KEY_ERROR)
        assert exc_type == "KeyError"
        assert message == "'target'"
        assert file_name == "/workspace/main.py"
        assert line == 23

    def test_a_dotted_exception_name_is_reduced_to_its_class(self):
        stderr = "sklearn.exceptions.NotFittedError: this instance is not fitted yet"
        exc_type, message, _file, _line = parse_traceback(stderr)
        assert exc_type == "NotFittedError"
        assert "not fitted" in message

    def test_output_that_is_not_a_traceback_still_yields_a_message(self):
        """`UNKNOWN_FAILURE` must carry something the report can quote."""
        exc_type, message, file_name, line = parse_traceback("Killed\n")
        assert exc_type == ""
        assert message == "Killed"
        assert file_name is None and line is None

    def test_empty_stderr_is_not_an_error(self):
        assert parse_traceback("") == ("", "", None, None)


class TestFingerprint:
    def test_the_same_bug_on_different_values_shares_one_identity(self):
        left = fingerprint("ValueError", "could not convert string to float: 'male'")
        right = fingerprint("ValueError", "could not convert string to float: 'female'")
        assert left == right

    def test_different_bugs_keep_different_identities(self):
        assert fingerprint("KeyError", "'target'") != fingerprint(
            "KeyError", "'diagnosis'"
        )

    def test_a_message_that_is_only_a_literal_keeps_the_literal(self):
        """Collapsing every KeyError to one identity would make the counter useless."""
        assert fingerprint("KeyError", "'target'") == "KeyError:target"

    def test_line_numbers_and_addresses_do_not_change_identity(self):
        left = fingerprint("RuntimeError", "failed at 0x7f3a1c at line 42")
        right = fingerprint("RuntimeError", "failed at 0x9b2d4e at line 87")
        assert left == right

    def test_normalisation_strips_paths_numbers_and_quotes(self):
        assert normalize_message("could not open '/datasets/x.parquet' (errno 2)") == (
            "could not open  (errno )"
        )

    def test_an_unnamed_exception_still_produces_an_identity(self):
        assert fingerprint("", "").startswith("Unknown:")


class TestClassification:
    @pytest.mark.parametrize(
        ("exception_type", "expected"),
        [
            ("SyntaxError", ErrorKind.SYNTAX),
            ("ModuleNotFoundError", ErrorKind.IMPORT),
            ("NameError", ErrorKind.NAME),
            ("AttributeError", ErrorKind.TYPE),
            ("KeyError", ErrorKind.DATA),
            ("FileNotFoundError", ErrorKind.DATA),
            ("AssertionError", ErrorKind.ASSERTION),
            ("MemoryError", ErrorKind.OOM),
            ("ValueError", ErrorKind.VALUE),
            ("SomethingNobodyMapped", ErrorKind.RUNTIME),
        ],
    )
    def test_the_static_table_maps_exception_types_to_kinds(
        self, exception_type, expected
    ):
        assert classify_exception(exception_type, "some message") is expected

    def test_a_dimension_mismatch_is_a_shape_error_not_a_value_error(self):
        """The most common ML bug has a different fix from an ordinary bad value."""
        assert (
            classify_exception(
                "ValueError",
                "Found input variables with inconsistent numbers of samples",
            )
            is ErrorKind.VALUE
        )
        assert (
            classify_exception("ValueError", "operands could not be broadcast together")
            is ErrorKind.SHAPE
        )


class TestErrorRecord:
    def test_a_traceback_becomes_a_complete_record(self):
        source = "\n".join(f"line {n}" for n in range(1, 40))
        record = error_from_traceback(SHAPE_ERROR, revision=2, source=source)
        assert record.exception_type == "ValueError"
        assert record.line == 40
        assert record.revision == 2
        assert record.traceback == SHAPE_ERROR

    def test_the_offending_region_marks_the_failing_line(self):
        source = "\n".join(f"statement_{n}()" for n in range(1, 21))
        region = source_region(source, 10)
        assert region is not None
        assert "10 > statement_10()" in region
        assert "statement_5()" in region and "statement_15()" in region
        assert "statement_4()" not in region

    def test_a_line_outside_the_source_yields_no_region(self):
        assert source_region("one line", 99) is None
        assert source_region("one line", None) is None

    def test_a_failure_with_no_traceback_still_produces_a_usable_record(self):
        record = synthetic_error(
            ErrorKind.TIMEOUT, "execution exceeded the limit", revision=3
        )
        assert record.kind is ErrorKind.TIMEOUT
        assert record.fingerprint.startswith("timeout:")
        assert record.revision == 3


class TestValidationError:
    def test_a_syntax_rejection_is_reported_as_a_syntax_error(self):
        """`ast.parse` failing is a typo, not a policy violation, and needs a typo's hint."""
        record = validation_error(
            ["main.py:4: SyntaxError: invalid syntax"], revision=1
        )
        assert record.kind is ErrorKind.SYNTAX

    def test_a_policy_rejection_stays_a_validation_rejection(self):
        record = validation_error(
            ["line 2: module `requests` is not installed in the sandbox"], revision=1
        )
        assert record.kind is ErrorKind.VALIDATION_REJECTED
        assert "requests" in record.message

    def test_a_rejection_with_no_reasons_still_says_something(self):
        assert validation_error([], revision=1).message


class TestErrorKindHints:
    """§6.1 — the hints are deterministic because the environment's limits are facts."""

    def test_the_import_hint_names_the_module_and_what_is_available(self):
        record = ErrorRecord(
            kind=ErrorKind.IMPORT,
            fingerprint="ModuleNotFoundError:no-module-named",
            exception_type="ModuleNotFoundError",
            message="No module named 'requests'",
            revision=1,
        )
        hint = error_kind_hint(record)
        assert "`requests`" in hint
        assert "sklearn" in hint  # the allowlist is quoted, not described
        assert "no network" in hint

    def test_the_oom_hint_quotes_the_peak_memory_the_container_reached(self):
        record = synthetic_error(
            ErrorKind.OOM, "the container was OOM-killed", revision=1
        )
        hint = error_kind_hint(record, outcome=_outcome(max_rss_bytes=6 * 1024**3))
        assert "6144 MiB" in hint
        assert "batch size" in hint

    def test_the_timeout_hint_quotes_the_limit_and_what_was_spent(self):
        record = synthetic_error(ErrorKind.TIMEOUT, "exceeded the limit", revision=1)
        hint = error_kind_hint(record, outcome=_outcome(duration_ms=900_000))
        assert "900s" in hint  # the train profile's wall clock
        assert "RandomizedSearchCV" in hint

    def test_the_data_hint_names_the_dataset_the_plan_actually_bound(self):
        """The classic failure is reading a plausible file that does not exist."""
        record = ErrorRecord(
            kind=ErrorKind.DATA,
            fingerprint="KeyError:target",
            message="'target'",
            revision=1,
        )
        binding = DatasetBinding(
            dataset_id="sklearn.breast_cancer",
            path="/datasets/tabular/breast_cancer.parquet",
            sha256="a" * 64,
            task_kind="tabular-classification",
            target_column="diagnosis",
        )
        hint = error_kind_hint(record, dataset=binding)
        assert "sklearn.breast_cancer" in hint
        assert "breast_cancer.parquet" in hint
        assert "diagnosis" in hint

    def test_the_validation_hint_quotes_the_rejections(self):
        record = validation_error(["line 2: `os.system` is not permitted"], revision=1)
        outcome = _outcome(
            validation=ValidationReport(
                passed=False, rejections=["line 2: `os.system` is not permitted"]
            )
        )
        assert "os.system" in error_kind_hint(record, outcome=outcome)

    def test_the_contract_hint_distinguishes_missing_from_invalid(self):
        missing = synthetic_error(
            ErrorKind.CONTRACT_VIOLATION,
            "/artifacts/metrics.json was not written.",
            revision=1,
        )
        invalid = synthetic_error(
            ErrorKind.CONTRACT_VIOLATION,
            "metrics.json is missing required metric 'f1_macro'",
            revision=1,
        )
        assert "never written" in error_kind_hint(missing)
        assert "did not satisfy" in error_kind_hint(invalid)

    def test_a_kind_with_no_hint_says_so_rather_than_returning_nothing(self):
        record = synthetic_error(ErrorKind.NAME, "name 'x' is not defined", revision=1)
        assert ErrorKind.NAME not in ERROR_KIND_HINTS
        assert error_kind_hint(record) == (
            "No environment-specific hint applies to this failure."
        )


class TestConsecutiveRepeats:
    def test_no_errors_is_no_repeats(self):
        assert consecutive_repeats(None) == 0
        assert consecutive_repeats([]) == 0

    def test_only_the_trailing_run_counts(self):
        """Two of a bug, then a different one, then one more is not three of anything."""
        errors = [
            _error("A", 1),
            _error("A", 2),
            _error("B", 3),
            _error("A", 4),
        ]
        assert consecutive_repeats(errors) == 1

    def test_an_unbroken_run_is_counted_in_full(self):
        assert consecutive_repeats([_error("A", n) for n in range(1, 5)]) == 4


def _error(fp: str, revision: int) -> ErrorRecord:
    return ErrorRecord(
        kind=ErrorKind.RUNTIME, fingerprint=fp, message="boom", revision=revision
    )


def _outcome(**overrides) -> SandboxOutcome:
    import uuid

    base = {
        "execution_id": uuid.uuid4(),
        "profile": "train",
        "classification": "RUNTIME_ERROR",
        "exit_code": 1,
        "duration_ms": 1000,
        "validation": ValidationReport(passed=True),
        "revision": 1,
    }
    return SandboxOutcome(**{**base, **overrides})
