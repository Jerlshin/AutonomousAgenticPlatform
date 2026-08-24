"""Test doubles for the agent graph.

Two fakes carry the whole suite, matching the strategy in `docs/AGENTS.md` §12.2:

* `FakeChatModel` — scripted responses keyed by call index, so control flow is exercised
  deterministically with no model, no Ollama and no network.
* `FakeDockerClient` / `FakeSandboxDriver` — a scripted container runtime that writes real
  files into the real bind-mount directory, so artifact hashing, metrics parsing and
  bundle assembly run against actual bytes rather than mocks of them.

There is no `sleep()` anywhere: the fake container reports its exit state on the first
poll, and timeout paths are driven by setting the profile's wall clock to zero.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage


def run(coro: Any) -> Any:
    """Drive a coroutine from a synchronous test.

    Used instead of pytest-asyncio so the suite has one fewer dependency between it and
    being runnable in a bare checkout.
    """
    return asyncio.run(coro)


class FakeChatModel:
    """A chat client returning scripted replies in order.

    The last reply repeats once the script is exhausted, so a test that only cares about
    the first response does not have to predict how many repair attempts a node makes.
    """

    def __init__(
        self, replies: list[str], *, tokens_in: int = 100, tokens_out: int = 50
    ) -> None:
        self.replies = list(replies)
        self.calls: list[list[Any]] = []
        self.bound: dict[str, Any] = {}
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out

    def bind(self, **kwargs: Any) -> FakeChatModel:
        self.bound.update(kwargs)
        return self

    async def ainvoke(self, messages: list[Any], **_kwargs: Any) -> AIMessage:
        self.calls.append(messages)
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return AIMessage(
            content=self.replies[index],
            usage_metadata={
                "input_tokens": self._tokens_in,
                "output_tokens": self._tokens_out,
                "total_tokens": self._tokens_in + self._tokens_out,
            },
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeContainer:
    """A container that reports a scripted terminal state and writes scripted files."""

    def __init__(
        self,
        create_kwargs: dict[str, Any],
        *,
        exit_code: int = 0,
        oom_killed: bool = False,
        never_exits: bool = False,
        stdout: bytes = b"",
        stderr: bytes = b"",
        files: dict[str, str] | None = None,
        max_rss: int | None = 128 * 1024 * 1024,
    ) -> None:
        self.create_kwargs = create_kwargs
        self.exit_code = exit_code
        self.oom_killed = oom_killed
        self.never_exits = never_exits
        self.stdout = stdout
        self.stderr = stderr
        self.files = files or {}
        self.max_rss = max_rss

        self.started = False
        self.killed_with: str | None = None
        self.removed = False
        self.attrs: dict[str, Any] = {"State": {"Status": "created"}}

    @property
    def artifacts_dir(self) -> Path:
        """The host directory bind-mounted at /artifacts, from the create call."""
        for mount in self.create_kwargs.get("mounts", []):
            if mount.get("Target") == "/artifacts":
                return Path(mount["Source"])
        raise AssertionError("the container was created without an /artifacts mount")

    def start(self) -> None:
        self.started = True
        # The program "runs": whatever it was scripted to write appears on the mount.
        for relative, content in self.files.items():
            path = self.artifacts_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.attrs = {"State": {"Status": "running"}}

    def logs(self, **_kwargs: Any) -> list[tuple[bytes | None, bytes | None]]:
        return [(self.stdout or None, None), (None, self.stderr or None)]

    def reload(self) -> None:
        if self.never_exits:
            self.attrs = {"State": {"Status": "running"}}
            return
        if self.killed_with:
            self.attrs = {
                "State": {"Status": "exited", "ExitCode": 137, "OOMKilled": False}
            }
            return
        self.attrs = {
            "State": {
                "Status": "exited",
                "ExitCode": self.exit_code,
                "OOMKilled": self.oom_killed,
            }
        }

    def stats(self, **_kwargs: Any) -> dict[str, Any]:
        return {"memory_stats": {"max_usage": self.max_rss}} if self.max_rss else {}

    def kill(self, signal: str = "SIGKILL") -> None:
        self.killed_with = signal

    def remove(self, force: bool = False) -> None:
        self.removed = True


class _FakeContainerCollection:
    def __init__(self, owner: FakeDockerClient) -> None:
        self.owner = owner

    def create(self, **kwargs: Any) -> FakeContainer:
        container = FakeContainer(kwargs, **self.owner.container_kwargs)
        self.owner.created.append(container)
        return container

    def get(self, name: str) -> FakeContainer:
        raise KeyError(f"no such container: {name}")


class FakeDockerClient:
    """A Docker client whose containers behave as the test scripted them."""

    def __init__(self, **container_kwargs: Any) -> None:
        self.container_kwargs = container_kwargs
        self.created: list[FakeContainer] = []
        self.containers = _FakeContainerCollection(self)

    @property
    def last(self) -> FakeContainer:
        return self.created[-1]


# ------------------------------------------------------------------------------------
#  Sandbox driver double
# ------------------------------------------------------------------------------------

CLEAN_METRICS: dict[str, Any] = {
    "schema_version": "1.0",
    "task_kind": "tabular-classification",
    "framework": "scikit-learn",
    "dataset": {
        "id": "sklearn.breast_cancer",
        "sha256": "a" * 64,
        "n_samples": 569,
        "seed": 42,
    },
    "params": {"estimator": "LogisticRegression"},
    "metrics": {"accuracy": 0.9737, "f1_macro": 0.9712},
    "runtime": {"train_seconds": 1.8},
}


class FakeSandboxDriver:
    """A sandbox driver that writes real artifacts without launching a container.

    It runs the true validation and metrics-parsing path — the same `validate_source` and
    `parse_metrics_file` the Docker driver uses — so a test that asserts on
    `CONTRACT_VIOLATION` is asserting on the real classifier, not on a canned string.

    `script` makes the outcome vary per execution, which is what the correctness loop needs
    tested: "crashes, then the fix works" is a different assertion from "crashes", and a
    driver that can only do one of them cannot express it. Each entry overrides the
    driver's defaults for one call; the last entry repeats once the script runs out, so a
    test that only cares about the first two executions does not have to predict how many
    the graph will make.
    """

    def __init__(
        self,
        runs_root: Path,
        *,
        exit_code: int = 0,
        metrics: dict[str, Any] | None = None,
        artifacts: dict[str, str] | None = None,
        stdout: str = "training complete\n",
        stderr: str = "",
        timed_out: bool = False,
        oom_killed: bool = False,
        script: list[dict[str, Any]] | None = None,
    ) -> None:
        self.runs_root = Path(runs_root)
        self.exit_code = exit_code
        self.metrics = metrics
        self.artifacts = artifacts or {}
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.oom_killed = oom_killed
        self.script = list(script) if script else []
        self.calls: list[dict[str, Any]] = []

    def _spec(self) -> dict[str, Any]:
        """The overrides for this execution, defaulting to the driver's own settings."""
        base = {
            "exit_code": self.exit_code,
            "metrics": self.metrics,
            "artifacts": self.artifacts,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "oom_killed": self.oom_killed,
        }
        if not self.script:
            return base
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return {**base, **self.script[index]}

    def revision_dir(self, run_id: str, revision: int) -> Path:
        return self.runs_root / str(run_id) / f"rev-{revision:03d}"

    def final_dir(self, run_id: str) -> Path:
        return self.runs_root / str(run_id) / "final"

    async def execute(self, **kwargs: Any) -> Any:
        from app.schemas.metrics import parse_metrics_file
        from app.services.sandbox import SandboxResult, enumerate_artifacts, profile_for
        from app.services.validator import validate_source

        self.calls.append(kwargs)
        spec = self._spec()
        profile = profile_for(kwargs.get("profile", "exec"))
        revision = kwargs["revision"]

        report = validate_source(kwargs["code"], profile=profile.name)
        if not report.passed:
            return SandboxResult(
                execution_id=uuid.uuid4(),
                profile=profile.name,
                image=profile.image,
                exit_code=None,
                validation=report,
                revision=revision,
            )

        workdir = self.revision_dir(kwargs["run_id"], revision)
        artifacts_dir = workdir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (workdir / "main.py").write_text(kwargs["code"], encoding="utf-8")
        (workdir / "stdout.log").write_text(spec["stdout"], encoding="utf-8")
        (workdir / "stderr.log").write_text(spec["stderr"], encoding="utf-8")

        if spec["metrics"] is not None:
            (artifacts_dir / "metrics.json").write_text(
                json.dumps(spec["metrics"]), encoding="utf-8"
            )
        for relative, content in spec["artifacts"].items():
            path = artifacts_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        parsed, errors = parse_metrics_file(
            artifacts_dir / "metrics.json", artifacts_dir
        )
        return SandboxResult(
            execution_id=uuid.uuid4(),
            profile=profile.name,
            image=profile.image,
            exit_code=spec["exit_code"],
            timed_out=spec["timed_out"],
            oom_killed=spec["oom_killed"],
            duration_ms=1234,
            max_rss_bytes=64 * 1024 * 1024,
            stdout_tail=spec["stdout"],
            stderr_tail=spec["stderr"],
            stdout_ref=str(workdir / "stdout.log"),
            stderr_ref=str(workdir / "stderr.log"),
            metrics=parsed,
            metrics_errors=errors,
            artifacts=enumerate_artifacts(artifacts_dir),
            validation=report,
            revision=revision,
            workdir=str(workdir),
            artifacts_dir=str(artifacts_dir),
        )
