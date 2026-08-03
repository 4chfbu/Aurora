from __future__ import annotations

import shutil
import subprocess
import os
from dataclasses import dataclass
from pathlib import Path

from aurora.config import get_settings


@dataclass
class CommandResult:
    command: str
    executed_command: str
    cwd: str
    stdout: str
    stderr: str
    exit_code: int
    backend: str
    failure_kind: str | None = None


class CommandRunner:
    def run(self, *, command: str, cwd: Path, timeout: int | None) -> CommandResult:
        raise NotImplementedError


class LocalCommandRunner(CommandRunner):
    def run(self, *, command: str, cwd: Path, timeout: int | None) -> CommandResult:
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                command=command,
                executed_command=command,
                cwd=str(cwd),
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace"),
                stderr=f"command timed out after {timeout}s\n" + ((exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")),
                exit_code=124,
                backend="local",
                failure_kind="command_timed_out",
            )
        return CommandResult(
            command=command,
            executed_command=command,
            cwd=str(cwd),
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            backend="local",
        )


class KaliContainerRunner(CommandRunner):
    def __init__(self, image: str | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.image = image or settings.default_worker_image
        self.network = settings.default_container_network
        self.engine = shutil.which("podman") or shutil.which("docker")

    def _env_args(self) -> list[str]:
        settings = get_settings()
        values = {
            # Codex requires an auth value, but CC Switch injects the real
            # upstream credential. Never copy the real key into a Worker.
            "OPENAI_API_KEY": "aurora-proxy-placeholder",
            "OPENAI_BASE_URL": settings.codex_proxy_base_url,
            "OPENAI_MODEL": settings.llm_model,
            "AURORA_SUBAGENTS_ENABLED": "true" if settings.subagents_enabled else "false",
            "AURORA_SUBAGENTS_MAX_PER_WORKER": str(settings.subagents_max_per_worker),
            "AURORA_SUBAGENTS_MAX_CONCURRENT": str(settings.subagents_max_concurrent),
        }
        inherited_keys = [
            "AURORA_SUBAGENTS_MAX_PER_WORKER",
            "AURORA_SUBAGENTS_MAX_CONCURRENT",
            "AURORA_SUBAGENT_CODEX_COMMAND",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "https_proxy",
            "http_proxy",
            "no_proxy",
        ]
        args: list[str] = []
        for key, value in values.items():
            args.extend(["-e", f"{key}={value}"])
        for key in inherited_keys:
            if key in values:
                continue
            if os.getenv(key):
                args.extend(["-e", key])
        return args

    def available(self) -> bool:
        if self.engine is None:
            return False
        inspected = subprocess.run(
            [self.engine, "image", "inspect", self.image],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        return inspected.returncode == 0

    def run(self, *, command: str, cwd: Path, timeout: int | None) -> CommandResult:
        if self.engine is None:
            raise RuntimeError("docker/podman not available")
        # A solver only receives its own worker directory.  Mounting the
        # repository root exposed unrelated challenge files to the model.
        workspace = cwd.resolve()
        relative_cwd = workspace.relative_to(Path.cwd().resolve())
        container_cwd = Path("/workspace")
        label_args = self._label_args(relative_cwd)
        executed = [
            self.engine,
            "run",
            "--rm",
            "--network",
            self.network,
            "--cpus",
            str(self.settings.worker_container_cpus),
            "--memory",
            self.settings.worker_container_memory,
            *label_args,
            *self._env_args(),
            "-v",
            f"{workspace}:/workspace:rw",
            "-w",
            str(container_cwd),
            self.image,
            "bash",
            "-lc",
            command,
        ]
        try:
            completed = subprocess.run(
                executed,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._stop_container(relative_cwd)
            return CommandResult(
                command=command,
                executed_command=" ".join(executed),
                cwd=str(container_cwd),
                stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace"),
                stderr=f"command timed out after {timeout}s\n" + ((exc.stderr or "") if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")),
                exit_code=124,
                backend="kali-container",
                failure_kind="command_timed_out",
            )
        return CommandResult(
            command=command,
            executed_command=" ".join(executed),
            cwd=str(container_cwd),
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
            backend="kali-container",
            failure_kind="resource_terminated" if completed.returncode == 137 else None,
        )

    def _stop_container(self, relative_cwd: Path) -> None:
        """Best-effort cleanup when the docker client itself is timed out."""
        if self.engine is None:
            return
        parts = relative_cwd.parts
        if len(parts) < 3 or parts[0] != "codex-workspaces":
            return
        name = f"aurora-{parts[2]}"
        try:
            subprocess.run([self.engine, "rm", "--force", name], text=True, capture_output=True, timeout=15, check=False)
        except Exception:
            pass

    def _label_args(self, relative_cwd: Path) -> list[str]:
        parts = relative_cwd.parts
        if len(parts) >= 3 and parts[0] == "codex-workspaces":
            project_id = parts[1]
            worker_id = parts[2]
            return [
                "--label",
                "aurora=true",
                "--label",
                f"aurora.project_id={project_id}",
                "--label",
                f"aurora.worker_id={worker_id}",
                "--name",
                f"aurora-{worker_id}",
            ]
        return ["--label", "aurora=true"]


class AutoCommandRunner(CommandRunner):
    def __init__(self, prefer_kali: bool = True, allow_local_fallback: bool = True) -> None:
        self.prefer_kali = prefer_kali
        self.allow_local_fallback = allow_local_fallback
        self.kali = KaliContainerRunner()
        self.local = LocalCommandRunner()

    def run(self, *, command: str, cwd: Path, timeout: int | None) -> CommandResult:
        if self.prefer_kali and self.kali.available():
            try:
                return self.kali.run(command=command, cwd=cwd, timeout=timeout)
            except Exception as exc:  # Container failure should not break the control-plane MVP.
                if not self.allow_local_fallback:
                    raise RuntimeError(f"kali container execution failed and local fallback is disabled: {exc}") from exc
                local = self.local.run(command=command, cwd=cwd, timeout=timeout)
                local.stderr = f"[kali-container fallback: {exc}]\n{local.stderr}"
                local.backend = "local-fallback"
                return local
        if self.prefer_kali and not self.allow_local_fallback:
            raise RuntimeError(
                f"kali worker image is not available locally: {self.kali.image}. Pull/build it before running this runtime."
            )
        return self.local.run(command=command, cwd=cwd, timeout=timeout)
