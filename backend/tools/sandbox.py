import tempfile
import time
import asyncio
from pathlib import Path

from core.config import settings
from core.models import CodeArtifact, ExecutionResult


class PythonSandbox:
    def __init__(
        self,
        timeout_seconds: int = settings.sandbox_timeout_seconds,
        use_docker: bool = settings.use_docker_sandbox,
        docker_image: str = settings.sandbox_image,
    ):
        self.timeout_seconds = timeout_seconds
        self.use_docker = use_docker
        self.docker_image = docker_image

    async def execute(self, artifact: CodeArtifact) -> ExecutionResult:
        started = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="agent-sandbox-") as sandbox_dir:
            script_path = Path(sandbox_dir) / artifact.filename
            script_path.write_text(artifact.content, encoding="utf-8")
            command = self._docker_command(sandbox_dir, artifact.filename) if self.use_docker else [
                "python3",
                str(script_path),
            ]

            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    cwd=sandbox_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                
                try:
                    stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=self.timeout_seconds)
                    elapsed = time.perf_counter() - started
                    return ExecutionResult(
                        success=process.returncode == 0,
                        stdout=stdout_bytes.decode("utf-8") if stdout_bytes else "",
                        stderr=stderr_bytes.decode("utf-8") if stderr_bytes else "",
                        exit_code=process.returncode,
                        execution_time=elapsed,
                        command=" ".join(command),
                        artifact_id=artifact.artifact_id,
                    )
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except Exception:
                        pass
                    await process.communicate()
                    elapsed = time.perf_counter() - started
                    return ExecutionResult(
                        success=False,
                        stdout=None,
                        stderr=f"Execution timed out after {self.timeout_seconds} seconds.",
                        exit_code=124,
                        execution_time=elapsed,
                        command=" ".join(command),
                        artifact_id=artifact.artifact_id,
                    )

            except Exception as exc:
                elapsed = time.perf_counter() - started
                return ExecutionResult(
                    success=False,
                    stdout=None,
                    stderr=f"Sandbox execution error: {exc}",
                    exit_code=1,
                    execution_time=elapsed,
                    command=" ".join(command),
                    artifact_id=artifact.artifact_id,
                )

    def _docker_command(self, sandbox_dir: str, filename: str) -> list[str]:
        return [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--memory",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "128",
            "-v",
            f"{sandbox_dir}:/workspace",
            self.docker_image,
            "python",
            f"/workspace/{filename}",
        ]


sandbox = PythonSandbox()
