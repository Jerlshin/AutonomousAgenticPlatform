"""Docker sandbox driver — the subsystem that executes model-written code.

Normative specification: `docs/ARCHITECTURE.md` §10. The threat model assumes the code is
**actively adversarial**, not merely buggy: a prompt-injected corpus document is a
realistic path to hostile code generation. The container, not the static validator, is the
security boundary, and it is configured from a fixed profile table — no user- or
model-controlled string ever reaches container configuration. Only the *contents* of files
inside the bind-mounted directory are model-controlled.

The isolation properties this driver is responsible for holding:

* `--network none` — no host, no LAN, no internet, no sibling containers.
* read-only rootfs, with `/workspace` and `/tmp` as `noexec,nosuid,nodev` tmpfs.
* UID/GID 65534 (`nobody`), all capabilities dropped, `no-new-privileges`.
* CPU, memory (with swap pinned equal, so an allocation loop is OOM-killed rather than
  swapping the host to death), PID and file-descriptor limits from the profile.
* `/datasets` read-only, `/artifacts` read-write, and nothing else mounted.

`auto_remove` is deliberately False: with it on, the container is gone before `wait()`
returns and `OOMKilled`/`ExitCode` become unreadable. Removal happens explicitly in a
`finally` block after the state has been inspected.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import platform
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.core import metrics
from app.core.config import settings
from app.engine.state import SandboxProfileName, ValidationReport
from app.schemas.metrics import parse_metrics_file
from app.services.validator import validate_source

logger = logging.getLogger(__name__)

# Last 8 KiB of each stream travels in state; the full capped logs stay on the run volume.
TAIL_BYTES = 8 * 1024

# 512 MiB maximum single file, so a runaway write cannot fill the run volume.
FSIZE_LIMIT = 512 * 1024 * 1024

# How long to wait for the log stream to close once the container has exited. Bounded so a
# wedged daemon connection cannot hold a finished run open indefinitely.
LOG_DRAIN_TIMEOUT_S = 10

# How long the launch waits for the log attach to be established before starting the
# container. Exceeding it costs the run's output, not the run.
ATTACH_TIMEOUT_S = 10

DIGESTS_PATH = (
    Path(__file__).resolve().parents[3]
    / "infrastructure"
    / "docker"
    / "sandbox"
    / "digests.json"
)

# Artifact path → Deliverable type, by extension (ARCHITECTURE.md §7.4 layout).
_ARTIFACT_TYPES: tuple[tuple[str, str], ...] = (
    (".joblib", "model"),
    (".pt", "model"),
    (".pth", "model"),
    (".pkl", "model"),
    (".png", "plot"),
    (".svg", "plot"),
    (".jpg", "plot"),
    (".json", "metrics"),
    (".log", "log"),
    (".md", "report"),
)


class SandboxLaunchError(RuntimeError):
    """A Docker-level failure: daemon unreachable, image missing, mount refused.

    This is infrastructural, not agentic — `sandbox_exec` declares `FAIL_RUN` for it
    precisely so it is surfaced rather than masked as a code error the Debugger would
    then chase for four fruitless iterations.
    """


@dataclass(frozen=True)
class SandboxProfile:
    """Resource envelope for one class of execution (ARCHITECTURE.md §10.3)."""

    name: SandboxProfileName
    image: str
    cpus: float
    memory: str
    pids: int
    timeout_s: int
    nofile: int
    network_mode: str = "none"


def profile_for(name: str) -> SandboxProfile:
    """The profile table, resolved against configuration.

    Built on demand rather than at import so tests and a reloaded configuration see the
    current settings rather than the ones present when the module was first imported.
    """
    profiles: dict[str, SandboxProfile] = {
        "exec": SandboxProfile(
            name="exec",
            image=settings.SANDBOX_IMAGE,
            cpus=2.0,
            memory=settings.SANDBOX_EXEC_MEMORY,
            pids=128,
            timeout_s=settings.SANDBOX_EXEC_TIMEOUT_S,
            nofile=1024,
        ),
        "train": SandboxProfile(
            name="train",
            image=settings.SANDBOX_TRAIN_IMAGE,
            cpus=4.0,
            memory=settings.SANDBOX_TRAIN_MEMORY,
            pids=512,
            timeout_s=settings.SANDBOX_TRAIN_TIMEOUT_S,
            nofile=4096,
        ),
        # Opt-in, off by default: attaches to an `internal: true` bridge whose only other
        # member is MLflow. There is still no route to the internet, the host, or any
        # other datastore.
        "train-tracked": SandboxProfile(
            name="train-tracked",
            image=settings.SANDBOX_TRAIN_IMAGE,
            cpus=4.0,
            memory=settings.SANDBOX_TRAIN_MEMORY,
            pids=512,
            timeout_s=settings.SANDBOX_TRAIN_TIMEOUT_S,
            nofile=4096,
            network_mode="sandbox_tracked_net",
        ),
    }
    try:
        return profiles[name]
    except KeyError:
        raise ValueError(
            f"unknown sandbox profile {name!r}; expected one of {sorted(profiles)}"
        ) from None


class ArtifactRef(BaseModel):
    """One file the program wrote under /artifacts."""

    path: str  # relative to the artifacts directory
    abs_path: str
    artifact_type: str
    sha256: str
    size_bytes: int
    mime_type: str


class SandboxResult(BaseModel):
    """Everything `sandbox_exec` needs to classify the run (ARCHITECTURE.md §10.9)."""

    execution_id: uuid.UUID
    profile: SandboxProfileName
    image: str
    exit_code: int | None  # None only if the container never started
    timed_out: bool = False
    oom_killed: bool = False
    duration_ms: int = 0
    max_rss_bytes: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    stdout_ref: str = ""
    stderr_ref: str = ""
    metrics: dict[str, Any] | None = None  # parsed + validated /artifacts/metrics.json
    metrics_errors: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    validation: ValidationReport
    revision: int
    workdir: str = ""
    artifacts_dir: str = ""

    @property
    def launched(self) -> bool:
        return self.validation.passed and self.exit_code is not None


OutputCallback = Callable[[str, str], None]  # (stream, line)


class DockerSandboxDriver:
    """Launches sandbox containers as siblings via the host Docker daemon.

    The worker mounts `/var/run/docker.sock`, which is equivalent to root on the host —
    accepted for a single-user local platform on one condition: **the worker never
    executes agent-generated code**. Keeping that boundary intact is the security property
    the whole design rests on, so everything this class does with model output is write it
    to a file inside a directory it then bind-mounts. It never interpolates model output
    into a command, an image name, a mount path, or a label.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        runs_root: str | Path | None = None,
        poll_interval: float = 0.25,
        stats_interval: float = 2.0,
    ) -> None:
        self._client = client
        self.runs_root = Path(runs_root or settings.RUNS_ROOT)
        self.poll_interval = poll_interval
        self.stats_interval = stats_interval

    # -- docker client ---------------------------------------------------------

    @property
    def client(self) -> Any:
        """The Docker client, created on first use.

        Lazy so that importing the engine — or running the entire test suite against a
        fake client — never requires the Docker SDK or a reachable daemon.
        """
        if self._client is None:
            try:
                import docker
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise SandboxLaunchError(
                    "the docker SDK is not installed. Install it to execute sandboxed "
                    "code: pip install docker"
                ) from exc
            try:
                self._client = docker.DockerClient(base_url=settings.DOCKER_HOST)
            except Exception as exc:  # pragma: no cover - depends on the environment
                raise SandboxLaunchError(
                    f"cannot reach the Docker daemon at {settings.DOCKER_HOST}: {exc}"
                ) from exc
        return self._client

    # -- run volume ------------------------------------------------------------

    def revision_dir(self, run_id: str, revision: int) -> Path:
        """`/runs/{run_id}/rev-{n:03d}` — the handoff surface for one revision."""
        return self.runs_root / str(run_id) / f"rev-{revision:03d}"

    def final_dir(self, run_id: str) -> Path:
        return self.runs_root / str(run_id) / "final"

    def _prepare_workdir(
        self, run_id: str, revision: int, code: str, requirements: list[str]
    ) -> Path:
        """Materialise the revision directory and assert the path-traversal invariant."""
        # `run_id` reaches this method from state; it is a UUID everywhere it is created,
        # and validating it here is what makes the directory name safe to build by
        # interpolation at all.
        try:
            uuid.UUID(str(run_id))
        except (ValueError, AttributeError, TypeError):
            raise SandboxLaunchError(
                f"run_id {run_id!r} is not a UUID; refusing to build a mount path from it."
            ) from None

        workdir = self.revision_dir(run_id, revision)
        artifacts_dir = workdir / "artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        resolved = Path(os.path.realpath(artifacts_dir))
        root = Path(os.path.realpath(self.runs_root))
        if not resolved.is_relative_to(root):
            raise SandboxLaunchError(
                f"resolved artifacts path {resolved} escapes the runs root {root}."
            )

        (workdir / "main.py").write_text(code, encoding="utf-8")
        (workdir / "requirements.txt").write_text(
            "\n".join(requirements) + ("\n" if requirements else ""), encoding="utf-8"
        )
        _grant_sandbox_user_access(workdir, artifacts_dir)
        return workdir

    # -- execution -------------------------------------------------------------

    async def execute(
        self,
        *,
        run_id: str,
        revision: int,
        code: str,
        profile: str = "exec",
        step_id: str | None = None,
        seed: int = 42,
        requirements: list[str] | None = None,
        on_output: OutputCallback | None = None,
        execution_id: uuid.UUID | None = None,
    ) -> SandboxResult:
        """Validate, launch, stream and collect one execution.

        Returns a `SandboxResult` for every outcome the *program* can produce, including
        rejection, crash, timeout and OOM. It raises only `SandboxLaunchError`, and only
        for infrastructural failures.
        """
        prof = profile_for(profile)
        # The caller may pre-allocate the id so it can label the events it emits before
        # the container exists — the live console needs a correlation key at launch, not
        # at exit (`engine/nodes/sandbox_exec.py`).
        execution_id = execution_id or uuid.uuid4()

        report = validate_source(code, profile=prof.name)
        if not report.passed:
            # Zero container cost: the Coder gets its rejections back in milliseconds.
            logger.info(
                "Static validation rejected revision %d of run %s: %s",
                revision,
                run_id,
                "; ".join(report.rejections),
            )
            metrics.record_validation_rejection(report.rejections)
            metrics.record_sandbox_execution(
                profile=prof.name, classification="VALIDATION_REJECTED"
            )
            return SandboxResult(
                execution_id=execution_id,
                profile=prof.name,
                image=prof.image,
                exit_code=None,
                validation=report,
                revision=revision,
            )

        workdir = self._prepare_workdir(run_id, revision, code, requirements or [])
        artifacts_dir = workdir / "artifacts"
        stdout_path = workdir / "stdout.log"
        stderr_path = workdir / "stderr.log"

        container = await asyncio.to_thread(
            self._create_container,
            run_id=run_id,
            revision=revision,
            step_id=step_id,
            seed=seed,
            profile=prof,
            artifacts_dir=artifacts_dir,
        )

        started = time.monotonic()
        timed_out = False
        state: dict[str, Any] = {}
        max_rss: int | None = None

        # A dedicated daemon thread rather than `asyncio.to_thread`. The attach below can
        # block indefinitely if the daemon connection wedges, and a stuck `to_thread` call
        # holds a worker out of the shared executor for the life of the process — which on
        # a worker running back-to-back sandboxes is a slow strangulation. A daemon thread
        # costs a stack and dies with the process.
        attached = threading.Event()
        pump = threading.Thread(
            target=self._pump_logs,
            args=(container, stdout_path, stderr_path, on_output, attached),
            name=f"pluton-sbx-logs-{revision:03d}",
            daemon=True,
        )
        try:
            # The attach is established *before* the container starts, and the start waits
            # for it. docker-py implements `demux` on the attach endpoint only, and an
            # attach issued after the container has already exited never returns — so a
            # program that prints and exits in 50 ms, which is the common case rather than
            # an edge one, would otherwise produce no captured output at all.
            pump.start()
            await asyncio.to_thread(attached.wait, ATTACH_TIMEOUT_S)

            try:
                await asyncio.to_thread(container.start)
            except Exception as exc:
                raise SandboxLaunchError(
                    f"could not start the sandbox container for run {run_id} "
                    f"rev {revision}: {exc}"
                ) from exc

            state, max_rss, timed_out = await self._await_exit(
                container, prof.timeout_s
            )
            # The stream ends when the container does. Bounding the wait keeps a wedged
            # daemon connection from holding the whole run open.
            await asyncio.to_thread(pump.join, LOG_DRAIN_TIMEOUT_S)
            if pump.is_alive():
                logger.warning(
                    "Sandbox log capture for run %s rev %d did not finish within %ss; "
                    "the captured logs may be truncated.",
                    run_id,
                    revision,
                    LOG_DRAIN_TIMEOUT_S,
                )
        finally:
            with _suppressing("removing sandbox container"):
                await asyncio.to_thread(container.remove, force=True)

        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = state.get("ExitCode")
        oom_killed = bool(state.get("OOMKilled"))
        if timed_out:
            # SIGKILL, as the runtime reports it. The container never got to choose.
            exit_code = 137

        parsed_metrics, metrics_errors = parse_metrics_file(
            artifacts_dir / "metrics.json", artifacts_dir
        )

        # Classified here from the container's own state rather than from the caller's
        # later routing decision: `sandbox_exec` refines this into the §10.9 vocabulary
        # using stderr, but the resource outcomes — timeout, OOM, non-zero exit — are
        # facts the driver already holds and the ones the Sandbox Health board is about.
        metrics.record_sandbox_execution(
            profile=prof.name,
            classification=_classification(timed_out, oom_killed, exit_code),
            duration_s=duration_ms / 1000.0,
            max_rss_bytes=max_rss,
            timed_out=timed_out,
            oom_killed=oom_killed,
        )

        return SandboxResult(
            execution_id=execution_id,
            profile=prof.name,
            image=prof.image,
            exit_code=exit_code,
            timed_out=timed_out,
            oom_killed=oom_killed,
            duration_ms=duration_ms,
            max_rss_bytes=max_rss,
            stdout_tail=_tail(stdout_path),
            stderr_tail=_tail(stderr_path),
            stdout_ref=str(stdout_path),
            stderr_ref=str(stderr_path),
            metrics=parsed_metrics,
            metrics_errors=metrics_errors,
            artifacts=enumerate_artifacts(artifacts_dir),
            validation=report,
            revision=revision,
            workdir=str(workdir),
            artifacts_dir=str(artifacts_dir),
        )

    # -- container construction ------------------------------------------------

    def _create_container(
        self,
        *,
        run_id: str,
        revision: int,
        step_id: str | None,
        seed: int,
        profile: SandboxProfile,
        artifacts_dir: Path,
    ) -> Any:
        """Create the container from the fixed profile table (ARCHITECTURE.md §10.4)."""
        try:
            import docker
        except ImportError as exc:  # pragma: no cover - depends on the environment
            raise SandboxLaunchError(
                "the docker SDK is not installed. Install it to execute sandboxed code: "
                "pip install docker"
            ) from exc

        # `/workspace` is a tmpfs, so nothing the driver wrote to the revision directory
        # is visible inside the container — the entrypoint has to be mounted in explicitly
        # or `python /workspace/main.py` fails with ENOENT before any code runs. The mount
        # is a single *file*, read-only, layered over the tmpfs (runc orders mounts by
        # destination depth): the program gets exactly its own source and cannot rewrite
        # it mid-execution, while /workspace stays writable scratch.
        mounts = [
            docker.types.Mount(
                "/datasets", settings.DATASETS_VOLUME, type="volume", read_only=True
            ),
            docker.types.Mount(
                "/artifacts", str(artifacts_dir), type="bind", read_only=False
            ),
            docker.types.Mount(
                "/workspace/main.py",
                str(artifacts_dir.parent / "main.py"),
                type="bind",
                read_only=True,
            ),
        ]
        ulimits = [
            docker.types.Ulimit(
                name="nofile", soft=profile.nofile, hard=profile.nofile
            ),
            docker.types.Ulimit(name="nproc", soft=profile.pids, hard=profile.pids),
            docker.types.Ulimit(name="core", soft=0, hard=0),
            docker.types.Ulimit(name="fsize", soft=FSIZE_LIMIT, hard=FSIZE_LIMIT),
        ]

        # Deterministic by construction, which is what makes re-execution after a worker
        # crash idempotent: an existing container for this name is the previous attempt.
        name = f"pluton-sbx-{run_id}-{revision:03d}"
        self._remove_stale(name)

        try:
            return self.client.containers.create(
                image=resolve_image(profile.image),
                command=["python", "-I", "-u", "/workspace/main.py"],
                name=name,
                labels={
                    "pluton.run_id": str(run_id),
                    "pluton.step_id": str(step_id or ""),
                    "pluton.profile": profile.name,
                    "pluton.created_at": datetime.now(UTC).isoformat(),
                },
                user="65534:65534",  # nobody:nogroup
                working_dir="/workspace",
                network_mode=profile.network_mode,
                read_only=True,  # immutable rootfs
                # S108: these paths are inside the *container*, on a tmpfs this call
                # is creating with noexec,nosuid,nodev — the finding is about host temp
                # directories, and the container has no host filesystem to share.
                tmpfs={
                    "/workspace": "rw,noexec,nosuid,nodev,size=512m,mode=1777",
                    "/tmp": "rw,noexec,nosuid,nodev,size=128m,mode=1777",  # noqa: S108
                },
                mounts=mounts,
                environment={
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONHASHSEED": "0",
                    "PYTHONUNBUFFERED": "1",
                    "MPLBACKEND": "Agg",  # matplotlib must never seek a display
                    "OMP_NUM_THREADS": str(int(profile.cpus)),
                    "OPENBLAS_NUM_THREADS": str(int(profile.cpus)),
                    "MKL_NUM_THREADS": str(int(profile.cpus)),
                    "HOME": "/tmp",  # noqa: S108 - the container's tmpfs, see above
                    "PLUTON_RUN_ID": str(run_id),
                    "PLUTON_SEED": str(seed),
                    "PLUTON_ARTIFACTS": "/artifacts",
                    "PLUTON_DATASETS": "/datasets",
                },
                nano_cpus=int(profile.cpus * 1e9),
                mem_limit=profile.memory,
                memswap_limit=profile.memory,  # equal to mem_limit: no swap
                pids_limit=profile.pids,
                cap_drop=["ALL"],
                security_opt=_security_opt(),
                ulimits=ulimits,
                runtime=settings.SANDBOX_RUNTIME,
                detach=True,
                stdin_open=False,
                tty=False,
                auto_remove=False,
            )
        except SandboxLaunchError:
            raise
        except Exception as exc:
            raise SandboxLaunchError(
                f"could not create sandbox container for run {run_id} rev {revision}: {exc}"
            ) from exc

    def _remove_stale(self, name: str) -> None:
        """Clear a container left behind by a crashed worker, so the name is free."""
        with _suppressing(f"removing stale container {name}"):
            self.client.containers.get(name).remove(force=True)

    # -- lifecycle -------------------------------------------------------------

    async def _await_exit(
        self, container: Any, timeout_s: int
    ) -> tuple[dict[str, Any], int | None, bool]:
        """Poll until the container exits or the wall clock runs out.

        Polling rather than `container.wait(timeout=…)` because the SDK surfaces that
        timeout as a transport-level exception that is awkward to tell apart from a
        genuinely broken connection — and because the loop is where peak RSS is sampled.
        """
        deadline = time.monotonic() + timeout_s
        next_sample = 0.0
        max_rss: int | None = None

        while True:
            state = await asyncio.to_thread(self._read_state, container)
            if state.get("Status") not in (
                "created",
                "running",
                "restarting",
                "paused",
            ):
                return state, max_rss, False

            now = time.monotonic()
            if now >= next_sample:
                sample = await asyncio.to_thread(self._sample_rss, container)
                if sample is not None:
                    max_rss = max(max_rss or 0, sample)
                next_sample = now + self.stats_interval

            if now >= deadline:
                logger.warning(
                    "Sandbox execution exceeded %ss; killing container", timeout_s
                )
                with _suppressing("killing timed-out container"):
                    await asyncio.to_thread(container.kill, "SIGKILL")
                state = await asyncio.to_thread(self._read_state, container)
                return state, max_rss, True

            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _read_state(container: Any) -> dict[str, Any]:
        container.reload()
        return dict(container.attrs.get("State") or {})

    @staticmethod
    def _sample_rss(container: Any) -> int | None:
        """Peak RSS from one stats sample; None whenever the daemon will not say."""
        try:
            stats = container.stats(stream=False)
        except Exception:  # pragma: no cover - transient daemon errors
            return None
        memory = (stats or {}).get("memory_stats") or {}
        value = memory.get("max_usage") or memory.get("usage")
        return int(value) if isinstance(value, (int, float)) else None

    def _pump_logs(
        self,
        container: Any,
        stdout_path: Path,
        stderr_path: Path,
        on_output: OutputCallback | None,
        attached: threading.Event,
    ) -> None:
        """Drain the demultiplexed log stream to disk, capped, notifying the caller.

        Runs in its own thread: the SDK's stream is a blocking generator. Capping at
        `SANDBOX_MAX_OUTPUT_BYTES` per stream is what stops a program that prints in a
        loop from filling the run volume.

        `attached` is released as soon as the stream is established, which is the barrier
        `execute` waits on before starting the container.
        """
        cap = settings.SANDBOX_MAX_OUTPUT_BYTES
        written = {"stdout": 0, "stderr": 0}

        # The files are created before attaching, so `stdout_ref` and `stderr_ref` always
        # point at something real — a reader should find an empty log, not a missing path.
        with stdout_path.open("wb") as out, stderr_path.open("wb") as err:
            try:
                # `attach`, not `logs`: docker-py accepts `demux` on the attach endpoint
                # only, and `logs(demux=True)` raises TypeError — which this method used
                # to swallow into a warning, so a real container produced empty stdout and
                # stderr and every failure looked like a silent crash.
                stream: Iterable[tuple[bytes | None, bytes | None]] = container.attach(
                    stdout=True, stderr=True, stream=True, logs=True, demux=True
                )
            except Exception as exc:  # pragma: no cover - transient daemon errors
                logger.warning("Could not attach to sandbox log stream: %s", exc)
                return
            finally:
                # Released whether or not the attach succeeded: a failed attach must not
                # also add the whole attach timeout to the launch.
                attached.set()

            for stdout_chunk, stderr_chunk in stream:
                for name, chunk, handle in (
                    ("stdout", stdout_chunk, out),
                    ("stderr", stderr_chunk, err),
                ):
                    if not chunk:
                        continue
                    remaining = cap - written[name]
                    if remaining <= 0:
                        continue
                    if len(chunk) > remaining:
                        chunk = (
                            chunk[:remaining] + b"\n[truncated: output limit reached]\n"
                        )
                    handle.write(chunk)
                    handle.flush()
                    written[name] += len(chunk)
                    if on_output is not None:
                        with _suppressing("emitting sandbox output"):
                            on_output(name, chunk.decode("utf-8", errors="replace"))


# ------------------------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------------------------


def _classification(timed_out: bool, oom_killed: bool, exit_code: int | None) -> str:
    """The §10.9 classification the *driver* can determine on its own.

    OOM is checked before the timeout even though a container can be both: a process
    killed for memory that also ran long is an OOM story, and reporting it as a timeout
    would send an operator tuning `SANDBOX_EXEC_TIMEOUT_S` at a memory problem.
    """
    if oom_killed:
        return "OOM"
    if timed_out:
        return "TIMEOUT"
    if exit_code == 0:
        return "CLEAN"
    if exit_code is None:
        return "UNKNOWN_FAILURE"
    return "RUNTIME_ERROR"


def resolve_image(tag: str) -> str:
    """Prefer the digest pinned by `make build-sandbox` over a mutable tag.

    §10.10 specifies refusing any image absent from `digests.json`. That is enforced only
    once the file exists: refusing outright would make a fresh clone — where the sandbox
    images have not been built yet — unable to run at all, and the tag is still resolved
    locally by the daemon rather than pulled.
    """
    if not DIGESTS_PATH.is_file():
        return tag
    try:
        import json

        digests = json.loads(DIGESTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "Could not read pinned sandbox digests (%s); using tag %s", exc, tag
        )
        return tag

    if not isinstance(digests, dict):
        return tag
    pinned = digests.get(tag)
    if pinned:
        return pinned
    raise SandboxLaunchError(
        f"image {tag!r} is not listed in {DIGESTS_PATH.name}. Run `make build-sandbox` to "
        "build and pin the sandbox images."
    )


def _security_opt() -> list[str]:
    """`no-new-privileges` everywhere; AppArmor only where there is an AppArmor."""
    options = ["no-new-privileges:true"]
    if platform.system() == "Linux":
        # Docker Desktop's LinuxKit VM has no AppArmor, and naming a profile there fails
        # container creation outright.
        options.append("apparmor=docker-default")
    return options


def _grant_sandbox_user_access(workdir: Path, artifacts_dir: Path) -> None:
    """Make the bind-mounted directories writable by UID 65534 inside the container.

    `chown` needs root, which the worker has inside its own container but a developer
    running the API natively does not. The fallback widens the mode instead: these are
    per-run scratch directories on a single-user local platform, and the alternative is
    every artifact write failing with EACCES.
    """
    try:
        for path in (workdir, artifacts_dir):
            os.chown(path, 65534, 65534)
        for child in workdir.iterdir():
            if child.is_file():
                os.chown(child, 65534, 65534)
    except (PermissionError, OSError, AttributeError):
        logger.debug(
            "chown to 65534 unavailable; widening mode on %s instead", artifacts_dir
        )
        with _suppressing("widening artifact directory mode"):
            artifacts_dir.chmod(0o777)


def _tail(path: Path, limit: int = TAIL_BYTES) -> str:
    """The last `limit` bytes of a log file, decoded leniently."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def enumerate_artifacts(artifacts_dir: Path) -> list[ArtifactRef]:
    """Every file under /artifacts, hashed, so a deliverable can be verified later."""
    if not artifacts_dir.is_dir():
        return []

    refs: list[ArtifactRef] = []
    for path in sorted(artifacts_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(artifacts_dir).as_posix()
        refs.append(
            ArtifactRef(
                path=relative,
                abs_path=str(path),
                artifact_type=classify_artifact(relative),
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
                mime_type=mimetypes.guess_type(relative)[0]
                or "application/octet-stream",
            )
        )
    return refs


def classify_artifact(relative_path: str) -> str:
    """Map a written file to a `Deliverable.artifact_type`."""
    lowered = relative_path.lower()
    if lowered.endswith("metrics.json"):
        return "metrics"
    if lowered.startswith("model/"):
        return "model"
    if lowered.startswith("plots/"):
        return "plot"
    for suffix, kind in _ARTIFACT_TYPES:
        if lowered.endswith(suffix):
            return kind
    return "data"


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _suppressing:
    """Log-and-continue context manager for genuinely best-effort cleanup steps."""

    def __init__(self, what: str) -> None:
        self.what = what

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            logger.debug("Ignoring failure while %s: %s", self.what, exc)
        return True


def get_sandbox_driver(**kwargs: Any) -> DockerSandboxDriver:
    """The configured driver.

    `USE_DOCKER_SANDBOX=false` selects an in-process stub in the configuration reference;
    that stub is not written, and silently running model-generated code in the worker
    process would be the exact boundary violation §10.2 depends on never happening. So it
    refuses instead.
    """
    if not settings.SANDBOX_ENABLED:
        raise SandboxLaunchError("SANDBOX_ENABLED is false; no code can be executed.")
    if not settings.USE_DOCKER_SANDBOX:
        raise SandboxLaunchError(
            "USE_DOCKER_SANDBOX is false, but the in-process stub is not implemented — "
            "running agent-generated code inside the worker would breach the isolation "
            "boundary. Set USE_DOCKER_SANDBOX=true."
        )
    return DockerSandboxDriver(**kwargs)
