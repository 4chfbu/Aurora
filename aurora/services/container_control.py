from __future__ import annotations

import shutil
import subprocess
from typing import Any


def _engine() -> str | None:
    return shutil.which("podman") or shutil.which("docker")


def stop_project_containers(project_id: str) -> dict[str, object]:
    engine = _engine()
    if engine is None:
        return {"stopped": [], "error": "docker/podman not available"}

    listed = subprocess.run(
        [engine, "ps", "-q", "--filter", f"label=aurora.project_id={project_id}"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    stopped: list[str] = []
    errors: list[str] = []
    for container_id in container_ids:
        # Stop is a user-visible emergency control: do not wait for Docker's
        # default grace period while a solver command is still running.
        result = subprocess.run([engine, "kill", container_id], text=True, capture_output=True, check=False, timeout=10)
        if result.returncode == 0:
            stopped.append(container_id)
        else:
            errors.append(result.stderr or result.stdout or f"failed to stop {container_id}")
    return {"stopped": stopped, "errors": errors}


def stop_worker_containers(worker_id: str) -> dict[str, object]:
    """Stop only the container owned by one Worker when a lease expires."""
    engine = _engine()
    if engine is None:
        return {"stopped": [], "error": "docker/podman not available"}
    listed = subprocess.run(
        [engine, "ps", "-q", "--filter", f"label=aurora.worker_id={worker_id}"],
        text=True, capture_output=True, check=False, timeout=10,
    )
    stopped: list[str] = []
    errors: list[str] = []
    for container_id in (line.strip() for line in listed.stdout.splitlines() if line.strip()):
        result = subprocess.run([engine, "kill", container_id], text=True, capture_output=True, check=False, timeout=10)
        if result.returncode == 0:
            stopped.append(container_id)
        else:
            errors.append(result.stderr or result.stdout or f"failed to stop {container_id}")
    return {"stopped": stopped, "errors": errors}


def remove_project_containers(project_id: str) -> dict[str, object]:
    """Force-remove both running and exited containers owned by one project."""
    engine = _engine()
    if engine is None:
        return {"removed": [], "errors": ["docker/podman not available"]}
    listed = subprocess.run(
        [engine, "ps", "-aq", "--filter", f"label=aurora.project_id={project_id}"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    removed: list[str] = []
    errors: list[str] = []
    for container_id in (line.strip() for line in listed.stdout.splitlines() if line.strip()):
        result = subprocess.run([engine, "rm", "-f", container_id], text=True, capture_output=True, check=False, timeout=15)
        if result.returncode == 0:
            removed.append(container_id)
        else:
            errors.append(result.stderr or result.stdout or f"failed to remove {container_id}")
    return {"removed": removed, "errors": errors}


def get_project_container_logs(project_id: str, *, tail: int = 200) -> dict[str, Any]:
    engine = _engine()
    if engine is None:
        return {"containers": [], "error": "docker/podman not available"}

    listed = subprocess.run(
        [engine, "ps", "-q", "--filter", f"label=aurora.project_id={project_id}"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    container_ids = [line.strip() for line in listed.stdout.splitlines() if line.strip()]
    containers: list[dict[str, str]] = []
    for container_id in container_ids:
        inspected = subprocess.run(
            [engine, "inspect", "--format", "{{.Name}} {{.Image}} {{.State.Status}}", container_id],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        logs = subprocess.run(
            [engine, "logs", "--tail", str(max(1, min(tail, 1000))), container_id],
            text=True,
            capture_output=True,
            check=False,
            timeout=20,
        )
        containers.append(
            {
                "id": container_id,
                "info": inspected.stdout.strip(),
                "stdout": logs.stdout,
                "stderr": logs.stderr,
            }
        )
    return {"containers": containers}
