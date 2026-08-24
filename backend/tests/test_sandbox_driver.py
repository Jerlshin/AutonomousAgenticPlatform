"""The Docker sandbox driver (ARCHITECTURE.md §10).

Driven against a fake Docker client, so the isolation *configuration* is asserted exactly
— that is the part which must never regress. A change that quietly drops `network_mode` or
flips `read_only` would otherwise be invisible until something escaped.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.core.config import settings
from app.services.sandbox import (
    DockerSandboxDriver,
    SandboxLaunchError,
    classify_artifact,
    enumerate_artifacts,
    get_sandbox_driver,
    profile_for,
    sha256_text,
)
from tests.fakes import FakeDockerClient, run

pytest.importorskip("docker", reason="the Docker SDK builds the Mount/Ulimit payloads")

PROGRAM = """\
import json

if __name__ == "__main__":
    with open("/artifacts/metrics.json", "w") as handle:
        json.dump({}, handle)
"""

METRICS = {
    "schema_version": "1.0",
    "task_kind": "tabular-classification",
    "framework": "scikit-learn",
    "dataset": {
        "id": "sklearn.breast_cancer",
        "sha256": "b" * 64,
        "n_samples": 569,
        "seed": 42,
    },
    "metrics": {"accuracy": 0.97, "f1_macro": 0.96},
}


def driver_for(
    runs_root: Path, **container_kwargs
) -> tuple[DockerSandboxDriver, FakeDockerClient]:
    client = FakeDockerClient(**container_kwargs)
    return (
        DockerSandboxDriver(
            client=client, runs_root=runs_root, poll_interval=0, stats_interval=0
        ),
        client,
    )


class TestLaunchConfiguration:
    """§10.4 verbatim. These assertions are the isolation guarantees."""

    @pytest.fixture
    def created(self, runs_root):
        driver, client = driver_for(
            runs_root, files={"metrics.json": json.dumps(METRICS)}
        )
        run(
            driver.execute(
                run_id=str(uuid.uuid4()),
                revision=1,
                code=PROGRAM,
                profile="train",
                seed=7,
            )
        )
        return client.last.create_kwargs

    def test_the_container_has_no_network(self, created):
        assert created["network_mode"] == "none"

    def test_the_rootfs_is_read_only_with_noexec_scratch(self, created):
        assert created["read_only"] is True
        assert "noexec" in created["tmpfs"]["/workspace"]
        assert "nosuid" in created["tmpfs"]["/tmp"]

    def test_it_runs_as_nobody_with_no_capabilities(self, created):
        assert created["user"] == "65534:65534"
        assert created["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in created["security_opt"]

    def test_swap_is_pinned_to_the_memory_limit(self, created):
        """Without this an allocation loop swaps the host to death instead of being killed."""
        assert (
            created["mem_limit"]
            == created["memswap_limit"]
            == settings.SANDBOX_TRAIN_MEMORY
        )

    def test_resource_limits_come_from_the_profile(self, created):
        profile = profile_for("train")
        assert created["nano_cpus"] == int(profile.cpus * 1e9)
        assert created["pids_limit"] == profile.pids
        limits = {u["Name"]: u["Soft"] for u in created["ulimits"]}
        assert limits["nofile"] == profile.nofile
        assert limits["core"] == 0

    def test_datasets_are_read_only_and_artifacts_are_the_only_writable_mount(
        self, created
    ):
        mounts = {m["Target"]: m for m in created["mounts"]}
        assert mounts["/datasets"]["ReadOnly"] is True
        assert mounts["/datasets"]["Type"] == "volume"
        assert mounts["/artifacts"]["ReadOnly"] is False
        assert mounts["/artifacts"]["Type"] == "bind"
        # The entrypoint is the third mount and it is read-only: `/workspace` is a tmpfs,
        # so without it `python /workspace/main.py` cannot find the file the driver just
        # wrote. `/artifacts` remains the only writable mount, which is the property this
        # test is actually about.
        assert set(mounts) == {"/datasets", "/artifacts", "/workspace/main.py"}
        assert mounts["/workspace/main.py"]["ReadOnly"] is True
        assert mounts["/workspace/main.py"]["Type"] == "bind"
        assert [t for t, m in mounts.items() if not m["ReadOnly"]] == ["/artifacts"]

    def test_the_command_is_fixed_and_isolated(self, created):
        assert created["command"] == ["python", "-I", "-u", "/workspace/main.py"]
        assert created["stdin_open"] is False

    def test_auto_remove_is_off_so_the_exit_state_survives(self, created):
        """With auto_remove the container is gone before ExitCode can be read."""
        assert created["auto_remove"] is False

    def test_the_seed_and_paths_reach_the_program(self, created):
        env = created["environment"]
        assert env["PLUTON_SEED"] == "7"
        assert env["PLUTON_ARTIFACTS"] == "/artifacts"
        assert env["MPLBACKEND"] == "Agg"

    def test_the_container_name_is_deterministic(self, created):
        assert created["name"].startswith("pluton-sbx-")
        assert created["name"].endswith("-001")


class TestOutcomes:
    def test_a_clean_run_parses_metrics_and_hashes_artifacts(self, runs_root):
        driver, client = driver_for(
            runs_root,
            files={
                "metrics.json": json.dumps(METRICS),
                "plots/roc.png": "not really a png",
            },
            stdout=b"training complete\n",
        )
        result = run(
            driver.execute(
                run_id=str(uuid.uuid4()), revision=1, code=PROGRAM, profile="train"
            )
        )

        assert result.exit_code == 0
        assert result.timed_out is False
        assert result.metrics["metrics"]["accuracy"] == 0.97
        assert result.metrics_errors == []
        assert "training complete" in result.stdout_tail
        assert {a.path for a in result.artifacts} == {"metrics.json", "plots/roc.png"}
        assert all(len(a.sha256) == 64 for a in result.artifacts)
        assert client.last.removed is True

    def test_the_source_is_written_to_the_run_volume(self, runs_root):
        driver, _ = driver_for(runs_root, files={"metrics.json": json.dumps(METRICS)})
        run_id = str(uuid.uuid4())
        result = run(
            driver.execute(run_id=run_id, revision=3, code=PROGRAM, profile="train")
        )

        workdir = Path(result.workdir)
        assert workdir.name == "rev-003"
        assert (workdir / "main.py").read_text() == PROGRAM
        assert (workdir / "stdout.log").is_file()

    def test_a_timeout_kills_the_container(self, runs_root, monkeypatch):
        monkeypatch.setattr(settings, "SANDBOX_EXEC_TIMEOUT_S", 0)
        driver, client = driver_for(runs_root, never_exits=True)
        result = run(driver.execute(run_id=str(uuid.uuid4()), revision=1, code=PROGRAM))

        assert result.timed_out is True
        assert result.exit_code == 137
        assert client.last.killed_with == "SIGKILL"
        assert client.last.removed is True

    def test_an_oom_kill_is_reported_from_the_container_state(self, runs_root):
        driver, _ = driver_for(runs_root, exit_code=137, oom_killed=True)
        result = run(driver.execute(run_id=str(uuid.uuid4()), revision=1, code=PROGRAM))
        assert result.oom_killed is True

    def test_stderr_is_captured_for_a_crash(self, runs_root):
        traceback = b'Traceback (most recent call last):\n  File "/workspace/main.py", line 2\nKeyError: 1\n'
        driver, _ = driver_for(runs_root, exit_code=1, stderr=traceback)
        result = run(driver.execute(run_id=str(uuid.uuid4()), revision=1, code=PROGRAM))

        assert result.exit_code == 1
        assert "KeyError" in result.stderr_tail
        assert Path(result.stderr_ref).read_bytes() == traceback

    def test_missing_metrics_is_reported_rather_than_raised(self, runs_root):
        driver, _ = driver_for(runs_root)
        result = run(
            driver.execute(
                run_id=str(uuid.uuid4()), revision=1, code=PROGRAM, profile="train"
            )
        )
        assert result.metrics is None
        assert "was not written" in result.metrics_errors[0]

    def test_output_is_capped(self, runs_root, monkeypatch):
        monkeypatch.setattr(settings, "SANDBOX_MAX_OUTPUT_BYTES", 64)
        driver, _ = driver_for(runs_root, stdout=b"x" * 4096)
        result = run(driver.execute(run_id=str(uuid.uuid4()), revision=1, code=PROGRAM))
        assert Path(result.stdout_ref).stat().st_size < 200


class TestRefusalToLaunch:
    def test_rejected_code_never_reaches_the_daemon(self, runs_root):
        """A hallucinated import costs milliseconds, not a container launch."""
        driver, client = driver_for(runs_root)
        result = run(
            driver.execute(
                run_id=str(uuid.uuid4()), revision=1, code="import requests\n"
            )
        )

        assert result.validation.passed is False
        assert result.exit_code is None
        assert result.launched is False
        assert client.created == []

    def test_a_non_uuid_run_id_is_refused_before_any_path_is_built(self, runs_root):
        """The bind-mount source is interpolated from run_id; §7.4's traversal invariant."""
        driver, client = driver_for(runs_root)
        with pytest.raises(SandboxLaunchError, match="not a UUID"):
            run(driver.execute(run_id="../../etc", revision=1, code=PROGRAM))
        assert client.created == []

    def test_an_unknown_profile_is_refused(self, runs_root):
        driver, _ = driver_for(runs_root)
        with pytest.raises(ValueError, match="unknown sandbox profile"):
            run(
                driver.execute(
                    run_id=str(uuid.uuid4()), revision=1, code=PROGRAM, profile="gpu"
                )
            )

    def test_the_in_process_stub_is_refused_rather_than_silently_used(
        self, monkeypatch
    ):
        monkeypatch.setattr(settings, "USE_DOCKER_SANDBOX", False)
        with pytest.raises(SandboxLaunchError, match="isolation boundary"):
            get_sandbox_driver()


class TestHelpers:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("metrics.json", "metrics"),
            ("model/model.joblib", "model"),
            ("plots/confusion_matrix.png", "plot"),
            ("report_fragment.md", "report"),
            ("tables/report.csv", "data"),
        ],
    )
    def test_artifact_classification(self, path, expected):
        assert classify_artifact(path) == expected

    def test_enumerate_artifacts_is_empty_for_a_missing_directory(self, tmp_path):
        assert enumerate_artifacts(tmp_path / "nope") == []

    def test_text_hashing_is_stable(self):
        assert sha256_text("abc") == sha256_text("abc")
        assert len(sha256_text("abc")) == 64
