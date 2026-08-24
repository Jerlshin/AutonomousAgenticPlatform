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
import re
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

    def attach(self, **_kwargs: Any) -> list[tuple[bytes | None, bytes | None]]:
        """The demultiplexed stream the driver reads. `attach`, not `logs`: docker-py
        implements `demux` only on the attach endpoint."""
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
#  Vector store double
# ------------------------------------------------------------------------------------

# A chunk shaped exactly like `VectorStoreService`'s hit dicts (and `RetrievedChunk`'s
# fields), so a test can drop it straight into a `FakeVectorStore` without also having a
# live Qdrant to embed against.
DEFAULT_CHUNK: dict[str, Any] = {
    "point_id": "chunk-1",
    "collection": "rd_corpus",
    "score": 0.91,
    "source_uri": "file:///corpus/sklearn/pipeline.md",
    "title": "Pipelines and composite estimators",
    "section": "",
    "text": "Pipeline, StandardScaler and LogisticRegression basics.",
    "trust_level": "curated",
}


class FakeVectorStore:
    """A `VectorStoreService` double: canned hits for rd_corpus/code_exemplars, and a
    call log for run_memory reads and writes, so a test can assert what the graph looked
    up and what it recorded without a live Qdrant or embedding model.

    Defaults to one retrievable chunk, so the Researcher's default extraction reply
    (`researcher_extract_reply`, `sufficiency="sufficient"`, no signatures to verify) is
    not overridden by the "zero chunks can never be sufficient" rule in `researcher.py`
    — most tests want one deterministic research round, not two.
    """

    def __init__(
        self,
        *,
        rd_corpus_hits: list[dict[str, Any]] | None = None,
        code_exemplar_hits: list[dict[str, Any]] | None = None,
        run_memory_hits: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rd_corpus_hits = (
            [DEFAULT_CHUNK] if rd_corpus_hits is None else rd_corpus_hits
        )
        self.code_exemplar_hits = code_exemplar_hits or []
        self.run_memory_hits = run_memory_hits or []
        self.searches: list[tuple[str, str]] = []
        self.written: list[dict[str, Any]] = []

    async def search_rd_corpus(
        self, query: str, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        self.searches.append(("rd_corpus", query))
        return list(self.rd_corpus_hits)

    async def search_code_exemplars(
        self, query: str, **_kwargs: Any
    ) -> list[dict[str, Any]]:
        self.searches.append(("code_exemplars", query))
        return list(self.code_exemplar_hits)

    async def search_run_memory(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.searches.append(("run_memory", kwargs.get("fingerprint", "")))
        return list(self.run_memory_hits)

    async def write_run_memory(self, **kwargs: Any) -> str:
        point_id = str(uuid.uuid4())
        self.written.append({**kwargs, "point_id": point_id})
        return point_id


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
        # Honour a caller-allocated id the way the real driver does, so a test asserting
        # that `sandbox.started` and `sandbox.exit` name the same execution is asserting
        # on the real correlation and not on two independently generated uuids.
        execution_id = kwargs.get("execution_id") or uuid.uuid4()

        report = validate_source(kwargs["code"], profile=profile.name)
        if not report.passed:
            return SandboxResult(
                execution_id=execution_id,
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
            execution_id=execution_id,
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


# ------------------------------------------------------------------------------------
#  MLflow client double
# ------------------------------------------------------------------------------------

_FILTER_CLAUSE = re.compile(r"tags\.`([^`]+)`\s*=\s*'([^']*)'")


def _parse_filter(filter_string: str) -> list[tuple[str, str]]:
    """The two-clause `tags.\\`key\\` = 'value' and ...` shape `MLflowService` searches
    with — enough of MLflow's real filter DSL for the idempotent run lookups to work."""
    return _FILTER_CLAUSE.findall(filter_string or "")


class FakeMlflowExperiment:
    def __init__(self, experiment_id: str, name: str) -> None:
        self.experiment_id = experiment_id
        self.name = name
        self.lifecycle_stage = "active"


class FakeMlflowRunData:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.params: dict[str, str] = {}
        self.metrics: dict[str, float] = {}


class FakeMlflowRunInfo:
    def __init__(self, run_id: str, experiment_id: str, artifact_uri: str) -> None:
        self.run_id = run_id
        self.experiment_id = experiment_id
        self.artifact_uri = artifact_uri


class FakeMlflowRun:
    def __init__(self, run_id: str, experiment_id: str, artifact_uri: str) -> None:
        self.info = FakeMlflowRunInfo(run_id, experiment_id, artifact_uri)
        self.data = FakeMlflowRunData()


class FakeModelVersion:
    def __init__(self, name: str, version: str, run_id: str, source: str) -> None:
        self.name = name
        self.version = version
        self.run_id = run_id
        self.source = source
        self.description = ""
        self.tags: dict[str, str] = {}


class FakeMlflowClient:
    """A `MlflowClient` double: in-memory experiments, runs and a model registry.

    Enough of the real object's surface for `MLflowService` to drive end to end — tags,
    params, metrics, artifacts, the parent/child run hierarchy, and model registration —
    without a live MLflow server or the `mlflow` package installed, matching the fake-driver
    strategy the rest of this suite uses for Docker (`FakeDockerClient`) and Qdrant
    (`FakeVectorStore`).
    """

    def __init__(self) -> None:
        self._experiments: dict[str, FakeMlflowExperiment] = {}
        self._experiments_by_name: dict[str, str] = {}
        self.runs: dict[str, FakeMlflowRun] = {}
        self.artifacts: list[tuple[str, str, str | None]] = []
        self.registered_models: dict[str, dict[str, Any]] = {}
        self._next_id = 0

    def _new_id(self, prefix: str) -> str:
        self._next_id += 1
        return f"{prefix}-{self._next_id}"

    # -- experiments -----------------------------------------------------------

    def get_experiment_by_name(self, name: str) -> FakeMlflowExperiment | None:
        exp_id = self._experiments_by_name.get(name)
        return self._experiments.get(exp_id) if exp_id else None

    def create_experiment(self, name: str, tags: dict[str, str] | None = None) -> str:
        exp_id = self._new_id("exp")
        self._experiments[exp_id] = FakeMlflowExperiment(exp_id, name)
        self._experiments_by_name[name] = exp_id
        return exp_id

    def restore_experiment(self, experiment_id: str) -> None:
        exp = self._experiments.get(experiment_id)
        if exp is not None:
            exp.lifecycle_stage = "active"

    # -- runs --------------------------------------------------------------------

    def search_runs(
        self,
        experiment_ids: list[str],
        filter_string: str = "",
        max_results: int = 1000,
    ) -> list[FakeMlflowRun]:
        clauses = _parse_filter(filter_string)
        matches = [
            run
            for run in self.runs.values()
            if run.info.experiment_id in experiment_ids
            and all(run.data.tags.get(key) == value for key, value in clauses)
        ]
        return matches[:max_results]

    def create_run(
        self, experiment_id: str, tags: dict[str, str] | None = None
    ) -> FakeMlflowRun:
        run_id = self._new_id("run")
        run = FakeMlflowRun(
            run_id, experiment_id, artifact_uri=f"file:///fake-mlflow/{run_id}"
        )
        run.data.tags.update(tags or {})
        self.runs[run_id] = run
        return run

    def get_run(self, run_id: str) -> FakeMlflowRun:
        return self.runs[run_id]

    def set_tag(self, run_id: str, key: str, value: str) -> None:
        self.runs[run_id].data.tags[key] = value

    def log_param(self, run_id: str, key: str, value: str) -> None:
        self.runs[run_id].data.params[key] = str(value)

    def log_metric(
        self, run_id: str, key: str, value: float, step: int | None = None
    ) -> None:
        self.runs[run_id].data.metrics[key] = float(value)

    def log_artifact(
        self, run_id: str, local_path: str, artifact_path: str | None = None
    ) -> None:
        self.artifacts.append((run_id, local_path, artifact_path))

    def log_artifacts(
        self, run_id: str, local_dir: str, artifact_path: str | None = None
    ) -> None:
        self.artifacts.append((run_id, local_dir, artifact_path))

    # -- registry ------------------------------------------------------------------

    def get_registered_model(self, name: str) -> dict[str, Any]:
        if name not in self.registered_models:
            raise LookupError(f"registered model {name!r} does not exist")
        return self.registered_models[name]

    def create_registered_model(self, name: str) -> dict[str, Any]:
        entry = {"versions": {}, "aliases": {}}
        self.registered_models[name] = entry
        return entry

    def create_model_version(
        self, *, name: str, source: str, run_id: str
    ) -> FakeModelVersion:
        entry = self.registered_models.setdefault(name, {"versions": {}, "aliases": {}})
        version = str(len(entry["versions"]) + 1)
        mv = FakeModelVersion(name, version, run_id, source)
        entry["versions"][version] = mv
        return mv

    def set_registered_model_alias(self, name: str, alias: str, version: str) -> None:
        self.registered_models[name]["aliases"][alias] = version

    def get_model_version_by_alias(self, name: str, alias: str) -> FakeModelVersion:
        entry = self.registered_models.get(name) or {}
        version = (entry.get("aliases") or {}).get(alias)
        if version is None:
            raise LookupError(f"no model version aliased '{alias}' for {name!r}")
        return entry["versions"][version]

    def update_model_version(
        self, name: str, version: str, description: str | None = None
    ) -> None:
        self.registered_models[name]["versions"][version].description = (
            description or ""
        )

    def set_model_version_tag(
        self, name: str, version: str, key: str, value: str
    ) -> None:
        self.registered_models[name]["versions"][version].tags[key] = value


# ------------------------------------------------------------------------------------
#  DB session double
# ------------------------------------------------------------------------------------


class FakeDbSession:
    """An async-context-manager session that records every added row in memory."""

    def __init__(self, store: list[Any]) -> None:
        self._store = store

    def add(self, obj: Any) -> None:
        self._store.append(obj)

    async def commit(self) -> None:
        return None

    async def __aenter__(self) -> FakeDbSession:
        return self

    async def __aexit__(self, *_exc_info: Any) -> bool:
        return False


class FakeDbSessionFactory:
    """A `db_session_factory` double for `get_db_session_factory` (`engine/nodes/base.py`)."""

    def __init__(self) -> None:
        self.rows: list[Any] = []

    def __call__(self) -> FakeDbSession:
        return FakeDbSession(self.rows)


# ------------------------------------------------------------------------------------
#  Redis double
# ------------------------------------------------------------------------------------


class FakeRedis:
    """An in-memory stand-in for the slice of `redis.asyncio.Redis` this platform uses.

    Written rather than mocked for the same reason `FakeDockerClient` writes real files:
    the event path's correctness is *ordering* — `INCR` before `XADD`, `seq` monotonic,
    `XRANGE` in insertion order, `GETDEL` atomic — and a mock returning canned values
    asserts nothing about any of that. This one keeps real dicts and real lists, so a test
    that replays a backlog is exercising the real filter and the real cursor arithmetic.

    `xread` does not block. Blocking would make every WebSocket test wait out a five
    second timeout to observe an idle tail, so it sleeps briefly and reports nothing new,
    which is the same signal the real client gives when its block expires.
    """

    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.streams: dict[str, list[tuple[str, dict[str, str]]]] = {}
        self.channels: dict[str, list[dict[str, Any]]] = {}
        self.subscribers: dict[str, int] = {}
        self.expiries: dict[str, int] = {}
        self.closed = False
        self._seq = 0

    # -- strings ------------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def set(
        self, key: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and key in self.strings:
            return None
        self.strings[key] = str(value)
        if ex is not None:
            self.expiries[key] = ex
        return True

    async def getdel(self, key: str) -> str | None:
        return self.strings.pop(key, None)

    async def incr(self, key: str) -> int:
        value = int(self.strings.get(key, 0)) + 1
        self.strings[key] = str(value)
        return value

    async def delete(self, key: str) -> int:
        return int(self.strings.pop(key, None) is not None)

    async def exists(self, key: str) -> int:
        return int(
            key in self.strings
            or key in self.hashes
            or key in self.sets
            or key in self.streams
        )

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def eval(self, script: str, _numkeys: int, key: str, *args: str) -> int:
        """Just enough Lua for the run lock's compare-and-delete and compare-and-extend.

        Recognised by what the script does rather than by parsing it: there are exactly
        two scripts in this codebase and both are `GET`-then-act on one key.
        """
        if self.strings.get(key) != args[0]:
            return 0
        if "DEL" in script:
            self.strings.pop(key, None)
            return 1
        self.expiries[key] = int(args[1])
        return 1

    # -- hashes -------------------------------------------------------------------

    async def hset(self, key: str, *, mapping: dict[str, str]) -> int:
        target = self.hashes.setdefault(key, {})
        target.update({k: str(v) for k, v in mapping.items()})
        return len(mapping)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    # -- sets ---------------------------------------------------------------------

    async def sadd(self, key: str, member: str) -> int:
        target = self.sets.setdefault(key, set())
        before = len(target)
        target.add(member)
        return len(target) - before

    async def srem(self, key: str, member: str) -> int:
        target = self.sets.get(key, set())
        found = member in target
        target.discard(member)
        return int(found)

    async def scard(self, key: str) -> int:
        return len(self.sets.get(key, set()))

    async def zcard(self, _key: str) -> int:
        return 0

    # -- streams ------------------------------------------------------------------

    async def xadd(
        self,
        key: str,
        fields: dict[str, str],
        *,
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> str:
        self._seq += 1
        entry_id = f"{self._seq}-0"
        stream = self.streams.setdefault(key, [])
        stream.append((entry_id, dict(fields)))
        if maxlen is not None and len(stream) > maxlen:
            del stream[: len(stream) - maxlen]
        return entry_id

    async def xrange(
        self, key: str, min: str = "-", max: str = "+", count: int | None = None
    ) -> list[tuple[str, dict[str, str]]]:
        entries = list(self.streams.get(key, []))
        return entries[:count] if count else entries

    async def xread(
        self,
        streams: dict[str, str],
        *,
        count: int | None = None,
        block: int | None = None,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]] | None:
        await asyncio.sleep(0.005)
        out = []
        for key, last_id in streams.items():
            after = _stream_index(last_id)
            fresh = [
                (entry_id, fields)
                for entry_id, fields in self.streams.get(key, [])
                if _stream_index(entry_id) > after
            ]
            if fresh:
                out.append((key, fresh[:count] if count else fresh))
        return out or None

    async def xtrim(self, key: str, *, maxlen: int, approximate: bool = True) -> int:
        stream = self.streams.get(key, [])
        removed = max(0, len(stream) - maxlen)
        if removed:
            del stream[:removed]
        return removed

    async def scan_iter(self, *, match: str = "*", count: int = 100) -> Any:
        import fnmatch

        for key in list(self.streams) + list(self.strings):
            if fnmatch.fnmatch(key, match):
                yield key

    # -- pub/sub ------------------------------------------------------------------

    async def publish(self, channel: str, message: str) -> int:
        self.channels.setdefault(channel, []).append(json.loads(message))
        return self.subscribers.get(channel, 0)

    def pubsub(self) -> FakePubSub:
        return FakePubSub(self)

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


def _stream_index(entry_id: str) -> int:
    """The ordinal of a fake stream id. `0-0` and `$` both mean "from the start"."""
    if entry_id in ("$", "+"):
        return 1 << 62
    try:
        return int(str(entry_id).split("-", 1)[0])
    except (TypeError, ValueError):
        return 0


class FakePubSub:
    """A pub/sub handle that yields whatever was published while it was subscribed."""

    def __init__(self, client: FakeRedis) -> None:
        self.client = client
        self.channel: str | None = None
        self.delivered = 0

    async def subscribe(self, channel: str) -> None:
        self.channel = channel
        self.client.subscribers[channel] = self.client.subscribers.get(channel, 0) + 1

    async def listen(self) -> Any:
        while self.channel is not None:
            queued = self.client.channels.get(self.channel, [])
            while self.delivered < len(queued):
                message = queued[self.delivered]
                self.delivered += 1
                yield {"type": "message", "data": json.dumps(message)}
            await asyncio.sleep(0.005)

    async def aclose(self) -> None:
        if self.channel is not None:
            self.client.subscribers[self.channel] = max(
                0, self.client.subscribers.get(self.channel, 1) - 1
            )
        self.channel = None
