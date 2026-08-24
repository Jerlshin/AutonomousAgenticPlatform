"""Static validation gate for agent-generated programs (ARCHITECTURE.md §10.7).

**This is defence in depth, not the security boundary.** The container is the boundary:
`--network none`, a read-only rootfs, UID 65534, all capabilities dropped. What this gate
buys is *speed of feedback* — a hallucinated `import requests` becomes a debug cycle in
30 ms instead of a 60-second container launch, and the rejection message names the exact
module rather than handing the Debugger an import traceback.

Every check is a literal, syntactic one over the AST. It makes no attempt to be
undefeatable; code that evades it still lands in a container that cannot reach the
network, cannot write outside `/artifacts`, and cannot escalate privilege.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.engine.state import ValidationReport

# infrastructure/docker/sandbox/allowlist.txt, four levels up from this module.
ALLOWLIST_PATH = (
    Path(__file__).resolve().parents[3]
    / "infrastructure"
    / "docker"
    / "sandbox"
    / "allowlist.txt"
)

# Used when the allowlist file is unavailable (an installed wheel, a stripped image).
# Kept deliberately narrow: failing closed on the scientific core is better than failing
# open on the whole of stdlib.
_FALLBACK_ALLOWLIST = frozenset(
    """
    abc argparse ast base64 bisect collections contextlib copy csv dataclasses datetime
    decimal enum functools gzip hashlib heapq io itertools json logging math operator os
    pathlib pickle random re shutil statistics string sys tempfile textwrap time typing
    uuid warnings zipfile numpy pandas scipy sklearn joblib matplotlib seaborn torch
    torchvision lightgbm xgboost statsmodels pyarrow datasets tokenizers mlflow
    """.split()
)

# `os` is allowed as a module but restricted by attribute (ARCHITECTURE.md §10.7).
# `getenv` is included beyond the spec's list because it reads the same mapping `environ`
# already permits — rejecting it fails correct code for no security gain.
ALLOWED_OS_ATTRS = frozenset(
    {"path", "environ", "getenv", "makedirs", "listdir", "sep"}
)

# Modules whose presence signals a hallucinated download rather than a real dependency.
NETWORK_MODULES = frozenset(
    {
        "socket",
        "http",
        "urllib",
        "requests",
        "httpx",
        "ftplib",
        "telnetlib",
        "smtplib",
        "aiohttp",
    }
)

# Modules that reach past the interpreter regardless of the allowlist.
FORBIDDEN_MODULES = frozenset(
    {"subprocess", "ctypes", "mmap", "pty", "resource", "signal", "multiprocessing"}
)

# S108: container paths, not host ones — the set of mounts §10.4 makes writable.
WRITABLE_PREFIXES = ("/artifacts", "/workspace", "/tmp")  # noqa: S108
WRITE_MODE = re.compile(r"[wax+]")

MAX_SOURCE_BYTES = 200 * 1024
MAX_SOURCE_LINES = 4000


@lru_cache(maxsize=1)
def load_allowlist() -> frozenset[str]:
    """Module names permitted inside the sandbox, from the versioned allowlist file."""
    try:
        text = ALLOWLIST_PATH.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - only when the repo layout is not present
        return _FALLBACK_ALLOWLIST

    modules: set[str] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        for token in line.split():
            # `os.path` in the file grants the `os` root; attribute access is policed
            # separately by ALLOWED_OS_ATTRS.
            modules.add(token.split(".", 1)[0])
    return frozenset(modules) or _FALLBACK_ALLOWLIST


@dataclass
class _Findings:
    rejections: list[str]
    warnings: list[str]
    imports: set[str]
    writes_metrics_json: bool


class _Auditor(ast.NodeVisitor):
    """Collects every violation in one pass, so the Coder sees them all at once.

    Reporting one rejection at a time would cost one LLM round trip per violation; a
    program with three bad imports should be fixed in a single revision.
    """

    def __init__(self, allowlist: frozenset[str]) -> None:
        self.allowlist = allowlist
        self.f = _Findings(
            rejections=[], warnings=[], imports=set(), writes_metrics_json=False
        )
        self._has_main_guard = False

    # -- imports ---------------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._check_module(alias.name, node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:  # relative import — there is no package, only main.py
            self.f.rejections.append(
                f"line {node.lineno}: relative import — the sandbox runs a single file, "
                "there is no package to import from."
            )
        elif node.module == "os":
            # `from os import system` would sidestep the attribute audit below, which
            # only sees `os.<attr>`.
            for alias in node.names:
                if alias.name not in ALLOWED_OS_ATTRS:
                    self.f.rejections.append(
                        f"line {node.lineno}: `from os import {alias.name}` is not "
                        "permitted. The sandbox allows only "
                        + ", ".join(f"os.{a}" for a in sorted(ALLOWED_OS_ATTRS))
                        + "."
                    )
            self.f.imports.add("os")
        elif node.module:
            self._check_module(node.module, node.lineno)
        self.generic_visit(node)

    def _check_module(self, dotted: str, lineno: int) -> None:
        root = dotted.split(".", 1)[0]
        self.f.imports.add(root)
        if root in NETWORK_MODULES:
            self.f.rejections.append(
                f"line {lineno}: `import {dotted}` — the sandbox has no network. Nothing "
                "can be downloaded or fetched; load data from /datasets instead."
            )
        elif root in FORBIDDEN_MODULES:
            self.f.rejections.append(
                f"line {lineno}: `import {dotted}` is not permitted in the sandbox."
            )
        elif root not in self.allowlist:
            self.f.rejections.append(
                f"line {lineno}: module `{root}` is not installed in the sandbox and "
                "cannot be installed. Allowed modules: "
                + ", ".join(sorted(self.allowlist))
            )

    # -- calls -----------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        if isinstance(func, ast.Name):
            self._check_name_call(func.id, node)
        elif isinstance(func, ast.Attribute):
            self._check_attribute_call(func, node)

        self.generic_visit(node)

    def _check_name_call(self, name: str, node: ast.Call) -> None:
        if name in {"eval", "exec", "compile"} and not self._all_literal(node.args):
            self.f.rejections.append(
                f"line {node.lineno}: `{name}()` on a non-literal argument is not permitted."
            )
        elif name == "__import__" and not self._all_literal(node.args[:1]):
            self.f.rejections.append(
                f"line {node.lineno}: `__import__()` with a computed name defeats the "
                "import allowlist."
            )
        elif name == "input":
            self.f.rejections.append(
                f"line {node.lineno}: `input()` — stdin is closed; the program runs unattended."
            )
        elif name == "open":
            self._check_open(node)

    def _check_attribute_call(self, func: ast.Attribute, node: ast.Call) -> None:
        base = func.value.id if isinstance(func.value, ast.Name) else None
        if base is None:
            return
        if (
            base == "importlib"
            and func.attr == "import_module"
            and not self._all_literal(node.args[:1])
        ):
            self.f.rejections.append(
                f"line {node.lineno}: `importlib.import_module()` with a computed name "
                "defeats the import allowlist."
            )

    def _check_open(self, node: ast.Call) -> None:
        path = _string_value(node.args[0]) if node.args else None
        mode = _string_value(node.args[1]) if len(node.args) > 1 else None
        for kw in node.keywords:
            if kw.arg == "mode":
                mode = _string_value(kw.value) or mode

        if path is None or not WRITE_MODE.search(mode or "r"):
            return
        if path.startswith("/datasets"):
            self.f.rejections.append(
                f"line {node.lineno}: opening '{path}' for writing — /datasets is mounted "
                "read-only. Write outputs under /artifacts."
            )
        elif path.startswith("/") and not path.startswith(WRITABLE_PREFIXES):
            self.f.rejections.append(
                f"line {node.lineno}: opening '{path}' for writing — the rootfs is "
                "read-only. Only /artifacts, /workspace and /tmp are writable."
            )

    # -- structure -------------------------------------------------------------

    def visit_While(self, node: ast.While) -> None:
        if _is_true(node.test) and not _contains_exit(node):
            self.f.warnings.append(
                f"line {node.lineno}: `while True` with no break, return or raise in the "
                "body — this will hit the wall-clock limit."
            )
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if _is_main_guard(node.test):
            self._has_main_guard = True
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and "metrics.json" in node.value:
            self.f.writes_metrics_json = True
        self.generic_visit(node)

    @staticmethod
    def _all_literal(args: list[ast.expr]) -> bool:
        return all(isinstance(a, ast.Constant) for a in args)

    def finish(self) -> _Findings:
        if not self._has_main_guard:
            self.f.warnings.append(
                'no `if __name__ == "__main__":` guard — add one so the entry point is '
                "explicit."
            )
        return self.f


def _string_value(node: ast.expr) -> str | None:
    """The value of a string literal, or None for anything computed at runtime."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _contains_exit(node: ast.While) -> bool:
    return any(
        isinstance(n, (ast.Break, ast.Return, ast.Raise)) for n in ast.walk(node)
    )


def _is_main_guard(test: ast.expr) -> bool:
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and any(_string_value(c) == "__main__" for c in test.comparators)
    )


def validate_source(source: str, *, profile: str = "exec") -> ValidationReport:
    """Audit `source` and return the report `sandbox_exec` routes on.

    `profile` decides one rule only: a `train` program that never mentions
    `metrics.json` is rejected rather than warned, because a training run that produces
    no metrics has no deliverable and the container time would be wasted.
    """
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_SOURCE_BYTES:
        return ValidationReport(
            passed=False,
            rejections=[
                f"source is {len(encoded)} bytes, over the {MAX_SOURCE_BYTES}-byte limit — "
                "runaway generation."
            ],
        )
    line_count = source.count("\n") + 1
    if line_count > MAX_SOURCE_LINES:
        return ValidationReport(
            passed=False,
            rejections=[
                f"source is {line_count} lines, over the {MAX_SOURCE_LINES}-line limit — "
                "runaway generation."
            ],
        )

    try:
        tree = ast.parse(source, filename="main.py")
    except SyntaxError as exc:
        return ValidationReport(
            passed=False,
            rejections=[f"main.py:{exc.lineno}: {type(exc).__name__}: {exc.msg}"],
        )

    auditor = _Auditor(load_allowlist())
    auditor.visit(tree)
    _audit_os_attributes(tree, auditor.f)
    findings = auditor.finish()

    if not findings.writes_metrics_json:
        message = (
            "the program never references /artifacts/metrics.json. Every training step "
            "must write it — it is the contract that makes the run measurable."
        )
        if profile.startswith("train"):
            findings.rejections.append(message)
        else:
            findings.warnings.append(message)

    return ValidationReport(
        passed=not findings.rejections,
        rejections=findings.rejections,
        warnings=findings.warnings,
        imports_seen=sorted(findings.imports),
        writes_metrics_json=findings.writes_metrics_json,
    )


def _audit_os_attributes(tree: ast.AST, findings: _Findings) -> None:
    """`os` is allowed as a module but only for the attributes in ALLOWED_OS_ATTRS."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
            and node.attr not in ALLOWED_OS_ATTRS
        ):
            findings.rejections.append(
                f"line {node.lineno}: `os.{node.attr}` is not permitted. The sandbox allows "
                "only " + ", ".join(f"os.{a}" for a in sorted(ALLOWED_OS_ATTRS)) + "."
            )
