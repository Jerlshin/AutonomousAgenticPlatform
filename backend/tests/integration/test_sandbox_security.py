"""Container-level isolation assertions for the execution sandbox (ARCHITECTURE.md §10, §13.1).

These are the tests most worth running automatically, because a regression in sandbox
isolation is the highest-severity failure this platform can have (§19.1). Every other test
in the suite runs against fakes; these run a real container against a real Docker daemon
and assert on what the kernel actually permitted.

**They deliberately bypass the static validator.** `validate_source` would reject every
probe program here at zero cost — that is what it is for, and `test_sandbox_validator.py`
covers it exhaustively. But the validator is defence in depth, not the boundary. The claim
these tests exist to check is the one §10.2 rests on: *if hostile code did reach a
container, the container would contain it*. Asserting that requires getting hostile code
into a container, so the gate is stubbed out and the launch configuration is left exactly
as `DockerSandboxDriver` builds it.

Each probe prints an unambiguous marker rather than relying on an error string, so an
assertion cannot pass by accident when a message changes or a syscall fails for an
unrelated reason. `NETWORK_REACHABLE` appearing in stdout is a T2 finding; the absence of
`NETWORK_DENIED` is a broken test, and the two are distinguishable.

The whole module skips when Docker is unreachable, so `make test` on a laptop with Docker
Desktop stopped still passes. CI has a working daemon and runs them for real.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.engine.state import ValidationReport
from app.services import sandbox as sandbox_module
from app.services.sandbox import (
    DockerSandboxDriver,
    SandboxLaunchError,
    SandboxProfile,
)
from tests.fakes import run

pytestmark = pytest.mark.integration

# A stock image rather than `pluton-sandbox-exec:latest`: the sandbox images are §10.10 and
# are not built yet, and what is under test here is the *launch configuration* the driver
# produces, which needs nothing from the image but a Python interpreter. Point this at the
# real image once it exists and every assertion below still holds — that is the point.
DEFAULT_TEST_IMAGE = os.environ.get("PLUTON_TEST_SANDBOX_IMAGE", "python:3.11-slim")

# Small enough that an OOM or a fork bomb resolves in seconds rather than filling the
# runner's RAM on the way to proving a limit that is already configured.
PROBE_MEMORY = "256m"
PROBE_PIDS = 32
PROBE_CPUS = 0.5
PROBE_TIMEOUT_S = 60


# ------------------------------------------------------------------------------------
#  Fixtures
# ------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def docker_client() -> Any:
    """A live Docker client, or a skip.

    Session-scoped: connecting and pulling are the slow parts, and every test in this
    module wants the same daemon.
    """
    docker = pytest.importorskip("docker", reason="the docker SDK is not installed")
    try:
        client = docker.DockerClient(base_url=settings.DOCKER_HOST)
        client.ping()
    except Exception as exc:  # noqa: BLE001 - any failure here means "no daemon"
        pytest.skip(f"Docker daemon unreachable at {settings.DOCKER_HOST}: {exc}")
    return client


@pytest.fixture(scope="session")
def sandbox_image(docker_client: Any) -> str:
    """The probe image, pulled if the runner does not already have it."""
    try:
        docker_client.images.get(DEFAULT_TEST_IMAGE)
    except Exception:  # noqa: BLE001 - not present locally; try to fetch it
        try:
            docker_client.images.pull(DEFAULT_TEST_IMAGE)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"cannot obtain {DEFAULT_TEST_IMAGE}: {exc}")
    return DEFAULT_TEST_IMAGE


@pytest.fixture
def probe(
    monkeypatch: pytest.MonkeyPatch,
    docker_client: Any,
    sandbox_image: str,
    tmp_path: Path,
) -> Any:
    """`probe(code, **profile_overrides)` → the `SandboxResult` of running `code`.

    Stubs `validate_source` to pass and pins `profile_for` to a small, explicit profile.
    Everything else — the mounts, the ulimits, the capability drop, the tmpfs flags, the
    network mode — comes from `_create_container` unmodified, which is the only way these
    assertions say anything about production.
    """
    monkeypatch.setattr(
        sandbox_module,
        "validate_source",
        lambda source, profile="exec": ValidationReport(passed=True),
    )

    driver = DockerSandboxDriver(
        client=docker_client, runs_root=tmp_path, poll_interval=0.1, stats_interval=0.5
    )

    def _probe(code: str, **overrides: Any) -> Any:
        profile = SandboxProfile(
            name=overrides.pop("name", "exec"),
            image=overrides.pop("image", sandbox_image),
            cpus=overrides.pop("cpus", PROBE_CPUS),
            memory=overrides.pop("memory", PROBE_MEMORY),
            pids=overrides.pop("pids", PROBE_PIDS),
            timeout_s=overrides.pop("timeout_s", PROBE_TIMEOUT_S),
            nofile=overrides.pop("nofile", 256),
            network_mode=overrides.pop("network_mode", "none"),
        )
        assert not overrides, f"unknown profile overrides: {sorted(overrides)}"
        monkeypatch.setattr(sandbox_module, "profile_for", lambda _name: profile)
        return run(
            driver.execute(
                run_id=str(uuid.uuid4()), revision=1, code=code, profile="exec"
            )
        )

    return _probe


# ------------------------------------------------------------------------------------
#  T2 — network egress
# ------------------------------------------------------------------------------------

NETWORK_PROBE = """
import socket

# A literal address, so a refusal cannot be blamed on DNS being unavailable.
try:
    socket.create_connection(("1.1.1.1", 53), timeout=5).close()
    print("NETWORK_REACHABLE tcp")
except OSError as exc:
    print(f"NETWORK_DENIED tcp {type(exc).__name__}")

try:
    socket.gethostbyname("example.com")
    print("NETWORK_REACHABLE dns")
except OSError as exc:
    print(f"NETWORK_DENIED dns {type(exc).__name__}")

# `--network none` leaves exactly one interface: loopback.
with open("/proc/net/dev") as handle:
    names = [line.split(":")[0].strip() for line in handle.read().splitlines()[2:]]
print("INTERFACES", ",".join(sorted(n for n in names if n)))
"""


def test_network_egress_is_denied(probe: Any) -> None:
    """T2: the container has no route to the internet, the LAN, or the host."""
    result = probe(NETWORK_PROBE)

    assert result.exit_code == 0, result.stderr_tail
    assert "NETWORK_REACHABLE" not in result.stdout_tail
    assert "NETWORK_DENIED tcp" in result.stdout_tail
    assert "NETWORK_DENIED dns" in result.stdout_tail

    # Not merely "the connection failed" — there is no interface it could have used. The
    # check is for the absence of an `eth*` device rather than for a list equal to
    # `["lo"]`: an empty network namespace on Docker Desktop's LinuxKit kernel still
    # enumerates the tunnel pseudo-devices (gre0, tunl0, sit0 …), which have no peer and
    # carry nothing. A veth to a bridge would appear as eth0, and that is the regression
    # worth failing on.
    interfaces = result.stdout_tail.split("INTERFACES")[1].split()[0].split(",")
    assert "lo" in interfaces
    assert [name for name in interfaces if name.startswith("eth")] == [], interfaces


def test_the_container_has_no_route_off_itself(probe: Any) -> None:
    """The companion to the probe above: that one proves egress fails, this proves *why*.

    A CI runner with no outbound internet would fail to reach 1.1.1.1 from *any* network
    mode, so "the connection failed" alone does not distinguish `--network none` from a
    bridge on a firewalled host. An empty routing table does: with no network attached
    there is nowhere for a packet to go, whatever the host's firewall says.

    Asserted on `/proc/net/route` rather than `/etc/resolv.conf`, because Docker Desktop
    writes its resolver config into the container regardless of network mode — a file
    describing DNS the container has no interface to reach.
    """
    result = probe(
        'print("ROUTES")\n'
        'print(open("/proc/net/route").read())\n'
        'print("IFACES", open("/proc/net/dev").read())\n'
    )
    assert result.exit_code == 0, result.stderr_tail

    routes = result.stdout_tail.split("ROUTES", 1)[1].split("IFACES", 1)[0]
    # Line one is the header; anything after it is a route the container could use.
    entries = [line for line in routes.splitlines() if line.strip()][1:]
    assert entries == [], f"container has routes: {entries}"


# ------------------------------------------------------------------------------------
#  T1/T3 — filesystem
# ------------------------------------------------------------------------------------

FILESYSTEM_PROBE = """
import errno
import os

for path in ("/etc/pluton-probe", "/usr/lib/pluton-probe", "/pluton-probe", "/opt/probe"):
    try:
        with open(path, "w") as handle:
            handle.write("x")
        print(f"ROOTFS_WRITABLE {path}")
    except OSError as exc:
        print(f"ROOTFS_READONLY {path} {errno.errorcode.get(exc.errno, exc.errno)}")

try:
    with open("/datasets/pluton-probe", "w") as handle:
        handle.write("x")
    print("DATASETS_WRITABLE")
except OSError as exc:
    print(f"DATASETS_READONLY {errno.errorcode.get(exc.errno, exc.errno)}")

for path in ("/artifacts/ok.txt", "/tmp/ok.txt", "/workspace/ok.txt"):
    try:
        with open(path, "w") as handle:
            handle.write("x")
        print(f"WRITABLE {path}")
    except OSError as exc:
        print(f"UNEXPECTEDLY_READONLY {path} {exc}")
"""


def test_root_filesystem_is_immutable(probe: Any) -> None:
    """T1: `read_only=True` — the image's own filesystem cannot be modified."""
    result = probe(FILESYSTEM_PROBE)

    assert result.exit_code == 0, result.stderr_tail
    assert "ROOTFS_WRITABLE" not in result.stdout_tail
    for path in ("/etc/pluton-probe", "/usr/lib/pluton-probe", "/pluton-probe"):
        assert f"ROOTFS_READONLY {path} EROFS" in result.stdout_tail


def test_datasets_mount_is_read_only(probe: Any) -> None:
    """T1: the dataset registry is mounted `read_only=True` (§10.4)."""
    result = probe(FILESYSTEM_PROBE)

    assert "DATASETS_WRITABLE" not in result.stdout_tail
    assert "DATASETS_READONLY EROFS" in result.stdout_tail


def test_the_three_writable_paths_are_writable(probe: Any) -> None:
    """The other half of the contract: a correct program must still be able to work.

    An isolation test suite that only asserts refusals would pass on a container that
    refused everything, which is not a sandbox — it is a broken image.
    """
    result = probe(FILESYSTEM_PROBE)

    assert "UNEXPECTEDLY_READONLY" not in result.stdout_tail
    for path in ("/artifacts/ok.txt", "/tmp/ok.txt", "/workspace/ok.txt"):
        assert f"WRITABLE {path}" in result.stdout_tail
    # /artifacts is a bind mount, so what the program wrote is visible to the driver —
    # this is the path deliverables travel out on.
    assert (Path(result.artifacts_dir) / "ok.txt").read_text() == "x"


def test_scratch_mounts_are_noexec(probe: Any) -> None:
    """`/workspace` and `/tmp` are `noexec,nosuid,nodev` tmpfs (§10.4).

    Without `noexec`, a program could write a binary to the one writable place it has and
    execute it — which converts "the interpreter is constrained" into "the interpreter was
    a suggestion".
    """
    result = probe('print("MOUNTS")\nprint(open("/proc/mounts").read())\n')
    assert result.exit_code == 0, result.stderr_tail
    for line in result.stdout_tail.splitlines():
        if line.startswith("tmpfs /workspace ") or line.startswith("tmpfs /tmp "):
            assert "noexec" in line, line
            assert "nosuid" in line, line
            assert "nodev" in line, line


def test_fsize_ulimit_caps_a_runaway_write(
    probe: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3: a single file cannot grow without bound and fill the run volume.

    The real limit is 512 MiB; writing that much to prove it would cost a minute of CI
    time and a gigabyte of runner disk, so the module constant is lowered and the *same*
    ulimit is exercised.
    """
    monkeypatch.setattr(sandbox_module, "FSIZE_LIMIT", 1024 * 1024)
    result = probe(
        "import errno\n"
        "chunk = b'x' * (1024 * 1024)\n"
        "try:\n"
        "    with open('/artifacts/big.bin', 'wb') as handle:\n"
        "        for _ in range(16):\n"
        "            handle.write(chunk)\n"
        "            handle.flush()\n"
        "    print('FSIZE_UNLIMITED')\n"
        "except (OSError, IOError) as exc:\n"
        "    print('FSIZE_CAPPED', errno.errorcode.get(exc.errno, exc.errno))\n"
    )
    assert "FSIZE_UNLIMITED" not in result.stdout_tail
    # SIGXFSZ terminates the process by default, so the cap shows up either as the caught
    # EFBIG or as a signal death. Both are the limit working.
    assert "FSIZE_CAPPED" in result.stdout_tail or result.exit_code != 0


# ------------------------------------------------------------------------------------
#  T3 — memory and CPU
# ------------------------------------------------------------------------------------


def test_memory_limit_kills_a_runaway_allocation(probe: Any) -> None:
    """T3: `mem_limit` with `memswap_limit` equal — allocation is killed, not swapped.

    Pinning swap to the memory limit is the part that matters. With swap available the
    same program would drag the whole host into thrashing instead of dying, which on a
    single-box platform means the API and the worker die with it.
    """
    result = probe(
        "blocks = []\n"
        "for _ in range(64):\n"
        "    blocks.append(bytearray(32 * 1024 * 1024))\n"
        "print('ALLOCATED_2GB')\n",
        memory="256m",
    )
    assert "ALLOCATED_2GB" not in result.stdout_tail
    assert result.exit_code != 0
    # 137 is SIGKILL as the runtime reports it; `oom_killed` is the daemon saying why.
    assert result.oom_killed or result.exit_code == 137, (
        f"exit={result.exit_code} oom={result.oom_killed} stderr={result.stderr_tail}"
    )


def test_cpu_quota_is_enforced(probe: Any) -> None:
    """T3: `nano_cpus` caps CPU time below wall-clock time.

    Asserted as a ratio rather than an absolute duration, because a shared CI runner
    makes any absolute timing meaningless. With a 0.5-core quota the process can earn at
    most half a second of CPU per second of wall clock; the threshold is loose enough to
    survive a noisy runner and tight enough that removing the quota fails it — an
    unrestricted container pins one core and scores ~1.0.
    """
    result = probe(
        "import time\n"
        "wall = time.monotonic()\n"
        "cpu = time.process_time()\n"
        "deadline = wall + 4.0\n"
        "x = 0\n"
        "while time.monotonic() < deadline:\n"
        "    x += 1\n"
        "print('RATIO', (time.process_time() - cpu) / (time.monotonic() - wall))\n",
        cpus=0.5,
    )
    assert result.exit_code == 0, result.stderr_tail
    ratio = float(result.stdout_tail.split("RATIO")[1].split()[0])
    assert ratio < 0.8, f"CPU quota not enforced: earned {ratio:.2f} cores of 0.5"


# ------------------------------------------------------------------------------------
#  T4 — PID exhaustion
# ------------------------------------------------------------------------------------


def test_pid_limit_stops_a_fork_bomb(probe: Any) -> None:
    """T4: `pids_limit` plus the `nproc` ulimit bound task creation.

    A fork bomb is the cheapest denial-of-service a generated program can stumble into —
    a runaway `multiprocessing.Pool` inside a loop is not even malicious — and without a
    PID cap it takes the host's process table with it.
    """
    result = probe(
        "import threading\n"
        "import time\n"
        "\n"
        "def spin():\n"
        "    time.sleep(30)\n"
        "\n"
        "started = 0\n"
        "try:\n"
        "    for _ in range(500):\n"
        "        threading.Thread(target=spin, daemon=True).start()\n"
        "        started += 1\n"
        "    print('PIDS_UNLIMITED', started)\n"
        "except RuntimeError as exc:\n"
        "    print('PIDS_LIMITED', started, type(exc).__name__)\n",
        pids=32,
        timeout_s=30,
    )
    assert "PIDS_UNLIMITED" not in result.stdout_tail
    assert "PIDS_LIMITED" in result.stdout_tail, result.stderr_tail
    started = int(result.stdout_tail.split("PIDS_LIMITED")[1].split()[0])
    # The limit counts the main thread and the interpreter's own tasks too, so the exact
    # number is not 32 — the assertion is that it is bounded near the limit rather than
    # anywhere near the 500 the program asked for.
    assert started < 64, f"created {started} threads under a 32-PID limit"


# ------------------------------------------------------------------------------------
#  T5 — privilege
# ------------------------------------------------------------------------------------

PRIVILEGE_PROBE = """
import os

print("UID", os.getuid())
print("GID", os.getgid())

status = dict(
    line.split(":", 1)
    for line in open("/proc/self/status").read().splitlines()
    if ":" in line
)
print("CAPEFF", status.get("CapEff", "?").strip())
print("CAPPRM", status.get("CapPrm", "?").strip())
print("NONEWPRIVS", status.get("NoNewPrivs", "?").strip())
"""


def test_container_runs_unprivileged_with_no_capabilities(probe: Any) -> None:
    """T5: UID 65534, `cap_drop: ALL`, `no-new-privileges` (§10.4).

    An all-zero effective capability set is the assertion that matters. A container
    running as a non-root user *with* CAP_SYS_ADMIN retained would pass a uid check and
    still be a kernel-exploit surface.
    """
    result = probe(PRIVILEGE_PROBE)

    assert result.exit_code == 0, result.stderr_tail
    assert "UID 65534" in result.stdout_tail
    assert "GID 65534" in result.stdout_tail
    assert "CAPEFF 0000000000000000" in result.stdout_tail
    assert "CAPPRM 0000000000000000" in result.stdout_tail
    assert "NONEWPRIVS 1" in result.stdout_tail


# ------------------------------------------------------------------------------------
#  Wall clock
# ------------------------------------------------------------------------------------


def test_wall_clock_kill_reports_a_timeout(probe: Any) -> None:
    """A program that will not stop is killed and reported as a timeout, not a crash.

    The distinction is load-bearing downstream: `sandbox_exec` classifies TIMEOUT
    separately from RUNTIME_ERROR precisely so the Debugger is not handed a traceback that
    does not exist.
    """
    result = probe("import time\nwhile True:\n    time.sleep(1)\n", timeout_s=5)

    assert result.timed_out is True
    assert result.exit_code == 137  # SIGKILL, as the runtime reports it
    assert result.duration_ms >= 5000


# ------------------------------------------------------------------------------------
#  T7 — path traversal  (no daemon required)
# ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "run_id",
    ["../../etc", "not-a-uuid", "", "../" * 8 + "tmp", "6f1c..%2f..%2fetc"],
)
def test_run_id_must_be_a_uuid_before_a_mount_path_is_built(
    run_id: str, tmp_path: Path
) -> None:
    """T7: no non-UUID `run_id` can reach a bind-mount path.

    The containment check further down (`realpath` against the runs root) is the backstop;
    this is the gate. Both exist because a traversal that escapes the run directory takes
    the artifacts bind mount with it, which is the one writable path into the host.
    """
    driver = DockerSandboxDriver(client=object(), runs_root=tmp_path)

    with pytest.raises(SandboxLaunchError, match="not a UUID"):
        run(driver.execute(run_id=run_id, revision=1, code="print(1)"))


def test_a_rejected_program_never_reaches_the_daemon(tmp_path: Path) -> None:
    """The static gate short-circuits before any Docker call (§10.7).

    `client` is an object that raises on every attribute access, so the test fails loudly
    if the driver touches the daemon on the rejection path — which is what makes "a
    rejected program costs 30 ms, not a container launch" an assertion rather than a claim.
    """

    class ExplodingClient:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(
                f"the daemon was contacted ({name}) for a rejected program"
            )

    driver = DockerSandboxDriver(client=ExplodingClient(), runs_root=tmp_path)
    result = run(
        driver.execute(
            run_id=str(uuid.uuid4()),
            revision=1,
            code="import socket\nsocket.create_connection(('1.1.1.1', 53))\n",
        )
    )

    assert result.validation.passed is False
    assert result.exit_code is None
    assert any("no network" in reason for reason in result.validation.rejections)
