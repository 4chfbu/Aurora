from __future__ import annotations

import shutil
import subprocess
import os
import json
import selectors
import signal
import time
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path

from aurora.config import get_settings
from aurora.services.tool_profiles import manifest_sha256
from aurora.services.network_proxy import network_proxy_registry
from aurora.services.openvpn_gateway import OpenVPNError, openvpn_gateway_registry


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
    finalization_reason: str | None = None


class CommandRunner:
    def run(self, *, command: str, cwd: Path, timeout: int | None) -> CommandResult:
        raise NotImplementedError

    def run_streaming(
        self,
        *,
        command: str,
        cwd: Path,
        timeout: int | None,
        on_output,
        soft_timeout: int | None = None,
        finalize_grace: int = 10,
    ) -> CommandResult:
        result = self.run(command=command, cwd=cwd, timeout=timeout)
        for stream, content in (("stdout", result.stdout), ("stderr", result.stderr)):
            for line in content.splitlines():
                on_output(stream, line)
        return result


class LocalCommandRunner(CommandRunner):
    def run(self, *, command: str, cwd: Path, timeout: int | None) -> CommandResult:
        try:
            environment = os.environ.copy()
            environment.update(network_proxy_registry.get().environment())
            completed = subprocess.run(
                command,
                shell=True,
                cwd=str(cwd),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=environment,
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
    def __init__(
        self,
        image: str | None = None,
        expected_profile: str | None = None,
        *,
        network: str | None = None,
        workspace_read_only: bool = False,
        environment_overrides: dict[str, str] | None = None,
    ) -> None:
        settings = get_settings()
        self.settings = settings
        self.image = image or settings.default_worker_image
        self.network = network or settings.default_container_network
        self.workspace_read_only = workspace_read_only
        self.environment_overrides = dict(environment_overrides or {})
        self.engine = shutil.which("podman") or shutil.which("docker")
        self.expected_profile = expected_profile
        self.availability_error: str | None = None

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
            **network_proxy_registry.get().container_environment(),
        }
        values.update(self.environment_overrides)
        inherited_keys = [
            "AURORA_SUBAGENTS_MAX_PER_WORKER",
            "AURORA_SUBAGENTS_MAX_CONCURRENT",
            "AURORA_SUBAGENT_CODEX_COMMAND",
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
            self.availability_error = "docker/podman not available"
            return False
        try:
            openvpn_gateway_registry.worker_network()
        except OpenVPNError as exc:
            self.availability_error = str(exc)
            return False
        inspected = subprocess.run(
            [self.engine, "image", "inspect", self.image],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if inspected.returncode != 0:
            detail = (inspected.stderr or inspected.stdout or "").strip()
            if "permission denied" in detail.lower():
                self.availability_error = f"cannot access the container engine while checking {self.image}: {detail}"
            else:
                self.availability_error = f"worker image is not available locally: {self.image}{f': {detail}' if detail else ''}"
            return False
        if self.expected_profile:
            labels = subprocess.run(
                [self.engine, "image", "inspect", "--format", "{{json .Config.Labels}}", self.image],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            try:
                parsed = json.loads(labels.stdout or "{}") if labels.returncode == 0 else {}
            except json.JSONDecodeError:
                parsed = {}
            actual_profile = parsed.get("io.aurora.worker.profile")
            actual_manifest = parsed.get("io.aurora.tool-manifest-sha256")
            if actual_profile != self.expected_profile or actual_manifest != manifest_sha256():
                self.availability_error = (
                    f"worker image metadata mismatch: image={self.image} expected_profile={self.expected_profile} "
                    f"actual_profile={actual_profile or 'missing'}"
                )
                return False
        if not self._proxy_available():
            return False
        self.availability_error = None
        return True

    def _proxy_available(self) -> bool:
        """Fail preflight before starting a Worker when its LLM proxy is down."""
        settings = get_settings()
        proxy_host = urlparse(settings.codex_proxy_base_url).hostname
        if proxy_host != "aurora-cc-switch":
            return True

        inspected = subprocess.run(
            [self.engine, "inspect", "--format", "{{json .State}}", "aurora-cc-switch"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if inspected.returncode != 0:
            detail = (inspected.stderr or inspected.stdout or "").strip()
            self.availability_error = f"CC Switch proxy container is unavailable{f': {detail}' if detail else ''}"
            return False
        try:
            state = json.loads(inspected.stdout or "{}")
        except json.JSONDecodeError:
            state = {}
        status = str(state.get("Status") or "unknown")
        health = str((state.get("Health") or {}).get("Status") or "none")
        if status != "running" or health not in {"healthy", "none"}:
            self.availability_error = f"CC Switch proxy is not ready: container_status={status}, health={health}"
            return False
        return True

    def run(self, *, command: str, cwd: Path, timeout: int | None) -> CommandResult:
        if self.engine is None:
            raise RuntimeError("docker/podman not available")
        executed, relative_cwd, container_cwd = self._build_command(command, cwd)
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

    def run_streaming(
        self,
        *,
        command: str,
        cwd: Path,
        timeout: int | None,
        on_output,
        soft_timeout: int | None = None,
        finalize_grace: int = 10,
    ) -> CommandResult:
        if self.engine is None:
            raise RuntimeError("docker/podman not available")
        executed, relative_cwd, container_cwd = self._build_command(command, cwd)
        process = subprocess.Popen(
            executed,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        captured = {"stdout": [], "stderr": []}
        started = time.monotonic()
        finalization_reason: str | None = None
        interrupted_at: float | None = None

        def request_finalization(reason: str, now: float) -> None:
            nonlocal finalization_reason, interrupted_at
            if finalization_reason is not None:
                return
            finalization_reason = reason
            interrupted_at = now
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                on_output("control", reason)
            except Exception:
                # Process control is authoritative even if checkpoint/event
                # persistence is temporarily unavailable.
                pass

        while selector.get_map():
            now = time.monotonic()
            if soft_timeout is not None and now - started >= soft_timeout:
                request_finalization("soft_timeout", now)
            if timeout is not None and now - started >= timeout:
                request_finalization("hard_timeout", now)
            if finalization_reason and interrupted_at is not None and now - interrupted_at >= max(1, finalize_grace) and process.poll() is None:
                self._stop_container(relative_cwd)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for key, _ in selector.select(timeout=0.2):
                line = key.fileobj.readline()
                if line:
                    captured[key.data].append(line)
                    callback_reason = on_output(key.data, line.rstrip("\n"))
                    if isinstance(callback_reason, str) and callback_reason:
                        request_finalization(callback_reason, time.monotonic())
                else:
                    selector.unregister(key.fileobj)
            if process.poll() is not None and not selector.get_map():
                break
        return_code = process.wait()
        stderr = "".join(captured["stderr"])
        if finalization_reason:
            stderr = (
                f"command finalization requested ({finalization_reason}); Codex received SIGINT "
                f"and a {max(1, finalize_grace)}s finalization grace period\n" + stderr
            )
        return CommandResult(
            command=command,
            executed_command=" ".join(executed),
            cwd=str(container_cwd),
            stdout="".join(captured["stdout"]),
            stderr=stderr,
            exit_code=124 if finalization_reason else return_code,
            backend="kali-container",
            failure_kind=(
                "command_timed_out"
                if finalization_reason in {"soft_timeout", "hard_timeout"}
                else finalization_reason
                if finalization_reason
                else "resource_terminated"
                if return_code == 137
                else None
            ),
            finalization_reason=finalization_reason,
        )

    def _build_command(self, command: str, cwd: Path) -> tuple[list[str], Path, Path]:
        # A solver only receives its own worker directory. Mounting the
        # repository root exposed unrelated challenge files to the model.
        workspace = cwd.resolve()
        relative_cwd = workspace.relative_to(Path.cwd().resolve())
        container_cwd = Path("/workspace")
        workspace_stat = workspace.stat()
        vpn_network = openvpn_gateway_registry.worker_network()
        selected_network = vpn_network or self.network
        network_args = ["--network", selected_network]
        if vpn_network is None:
            network_args = ["--add-host", "host.docker.internal:host-gateway", *network_args]
        executed = [
            self.engine,
            "run",
            "--rm",
            # nmap carries file capabilities (cap_net_raw/cap_net_admin); a
            # non-root exec only succeeds when those caps are in the
            # container bounding set. Grant them so network.scan works.
            "--cap-add=NET_RAW",
            "--cap-add=NET_ADMIN",
            *network_args,
            "--cpus",
            str(self.settings.worker_container_cpus),
            "--memory",
            self.settings.worker_container_memory,
            "--user",
            f"{workspace_stat.st_uid}:{workspace_stat.st_gid}",
            *self._label_args(relative_cwd),
            *self._env_args(),
            "-e",
            "HOME=/workspace/runtime/home",
            "-v",
            f"{workspace}:/workspace:{'ro' if self.workspace_read_only else 'rw'}",
            "-w",
            str(container_cwd),
            self.image,
            "bash",
            "-lc",
            command,
        ]
        return executed, relative_cwd, container_cwd

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
    def __init__(
        self,
        prefer_kali: bool = True,
        allow_local_fallback: bool = True,
        image: str | None = None,
        expected_profile: str | None = None,
        *,
        network: str | None = None,
        workspace_read_only: bool = False,
        environment_overrides: dict[str, str] | None = None,
    ) -> None:
        self.prefer_kali = prefer_kali
        self.allow_local_fallback = allow_local_fallback
        self.kali = KaliContainerRunner(
            image=image,
            expected_profile=expected_profile,
            network=network,
            workspace_read_only=workspace_read_only,
            environment_overrides=environment_overrides,
        )
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
            reason = self.kali.availability_error or f"worker image is unavailable: {self.kali.image}"
            raise RuntimeError(f"{reason}. Build the configured core/heavy worker images before running this runtime.")
        return self.local.run(command=command, cwd=cwd, timeout=timeout)

    def run_streaming(
        self,
        *,
        command: str,
        cwd: Path,
        timeout: int | None,
        on_output,
        soft_timeout: int | None = None,
        finalize_grace: int = 10,
    ) -> CommandResult:
        if self.prefer_kali and self.kali.available():
            try:
                return self.kali.run_streaming(
                    command=command,
                    cwd=cwd,
                    timeout=timeout,
                    on_output=on_output,
                    soft_timeout=soft_timeout,
                    finalize_grace=finalize_grace,
                )
            except Exception as exc:
                if not self.allow_local_fallback:
                    raise RuntimeError(f"kali container execution failed and local fallback is disabled: {exc}") from exc
        return super().run_streaming(
            command=command,
            cwd=cwd,
            timeout=timeout,
            on_output=on_output,
            soft_timeout=soft_timeout,
            finalize_grace=finalize_grace,
        )
