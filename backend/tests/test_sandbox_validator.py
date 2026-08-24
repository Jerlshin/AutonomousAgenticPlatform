"""The static validation gate (ARCHITECTURE.md §10.7).

The gate is defence in depth, not the security boundary — the container is. What these
tests pin down is that the *fast feedback* path works: a hallucinated download, a shell
escape or a write outside /artifacts is refused in milliseconds with a message the Coder
can act on, instead of costing a container launch and a debug iteration.
"""

from __future__ import annotations

import pytest

from app.services.validator import load_allowlist, validate_source

CLEAN_TRAIN_PROGRAM = """\
import json
import os

import numpy as np
from sklearn.linear_model import LogisticRegression


def main():
    os.makedirs("/artifacts", exist_ok=True)
    print("done", flush=True)
    with open("/artifacts/metrics.json", "w") as handle:
        json.dump({"metrics": {"accuracy": float(np.mean([1.0]))}}, handle)


if __name__ == "__main__":
    main()
"""


class TestAcceptance:
    def test_a_conforming_train_program_passes_without_warnings(self):
        report = validate_source(CLEAN_TRAIN_PROGRAM, profile="train")
        assert report.passed is True
        assert report.rejections == []
        assert report.warnings == []
        assert report.writes_metrics_json is True
        assert set(report.imports_seen) == {"json", "os", "numpy", "sklearn"}

    def test_the_allowlist_grants_the_scientific_core(self):
        allowlist = load_allowlist()
        assert {"numpy", "pandas", "sklearn", "torch", "matplotlib"} <= allowlist
        assert "requests" not in allowlist


class TestRejections:
    @pytest.mark.parametrize(
        "source,fragment",
        [
            ("import requests\n", "no network"),
            ("import urllib.request\n", "no network"),
            ("import subprocess\n", "not permitted"),
            ("import ctypes\n", "not permitted"),
            ("import pwn\n", "not installed in the sandbox"),
            ("import os\nos.system('ls')\n", "os.system"),
            ("from os import system\n", "from os import system"),
            ("data = eval(user_input)\n", "eval()"),
            ("import importlib\nimportlib.import_module(name)\n", "import_module"),
            ("x = __import__(name)\n", "__import__"),
            ("answer = input()\n", "input()"),
            ("open('/etc/passwd', 'w')\n", "rootfs is\nread-only".replace("\n", " ")),
            ("open('/datasets/x.parquet', 'w')\n", "read-only"),
            ("from . import helper\n", "relative import"),
        ],
    )
    def test_dangerous_constructs_are_refused(self, source, fragment):
        report = validate_source(source, profile="exec")
        assert report.passed is False
        assert any(fragment in rejection for rejection in report.rejections), (
            report.rejections
        )

    def test_syntax_errors_are_reported_with_their_line(self):
        report = validate_source("def broken(:\n    pass\n")
        assert report.passed is False
        assert "SyntaxError" in report.rejections[0]
        assert "main.py:1" in report.rejections[0]

    def test_every_violation_is_reported_at_once(self):
        """Three bad imports should cost one revision, not three."""
        report = validate_source("import requests\nimport socket\nimport httpx\n")
        assert len(report.rejections) >= 3

    def test_runaway_generation_is_refused(self):
        report = validate_source("x = 1\n" * 5000)
        assert report.passed is False
        assert "line limit" in report.rejections[0]

    def test_a_single_enormous_line_is_refused_on_bytes(self):
        """The line count alone would miss a megabyte of generated data on one line."""
        report = validate_source(f"DATA = '{'x' * (210 * 1024)}'\n")
        assert report.passed is False
        assert "byte limit" in report.rejections[0]

    def test_the_write_mode_keyword_form_is_caught_too(self):
        """`open(path, mode='w')` is the same write as `open(path, 'w')`."""
        report = validate_source("open('/etc/passwd', mode='w')\n")
        assert report.passed is False
        assert any("read-only" in reason for reason in report.rejections)

    def test_a_computed_path_is_left_to_the_container_to_refuse(self):
        """This gate is literal-path analysis; the read-only rootfs is the real boundary."""
        report = validate_source("open(destination, 'w')\n")
        assert not any("read-only" in reason for reason in report.rejections)

    def test_an_attribute_call_on_a_non_name_does_not_crash_the_auditor(self):
        report = validate_source("import json\nx = json.loads('{}').get('k')\n")
        assert report.passed is True

    def test_reading_datasets_is_fine(self):
        report = validate_source("open('/datasets/x.parquet')\n")
        assert report.passed is True


class TestMetricsContract:
    def test_a_train_program_without_metrics_json_is_refused(self):
        report = validate_source("print('hi')\n", profile="train")
        assert report.passed is False
        assert any("metrics.json" in reason for reason in report.rejections)

    def test_an_exec_program_without_metrics_json_is_only_warned(self):
        report = validate_source("print('hi')\n", profile="exec")
        assert report.passed is True
        assert any("metrics.json" in warning for warning in report.warnings)


class TestWarnings:
    def test_unbounded_loop_is_warned_not_blocked(self):
        report = validate_source("while True:\n    pass\n", profile="exec")
        assert report.passed is True
        assert any("while True" in warning for warning in report.warnings)

    def test_a_loop_with_a_break_is_not_warned(self):
        report = validate_source("while True:\n    break\n", profile="exec")
        assert not any("while True" in warning for warning in report.warnings)

    def test_missing_main_guard_is_warned(self):
        report = validate_source("print('hi')\n", profile="exec")
        assert any("__main__" in warning for warning in report.warnings)
