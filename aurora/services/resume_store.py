"""Verify an immutable resume bundle before publishing any restored files."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any

from sqlmodel import Session

from aurora.models import Artifact, Attempt
from aurora.services.flag_validator import FlagValidator


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _relative(value: Any, *, prefix: str | None = None) -> Path:
    path = Path(str(value or ""))
    if not path.parts or path.is_absolute() or ".." in path.parts or (prefix and path.parts[0] != prefix):
        raise ValueError("unsafe resume path")
    if prefix == "inputs" and (len(path.parts) < 2 or path == Path("inputs/manifest.json")):
        raise ValueError("invalid input path")
    return path


def _artifact_source(session: Session, project_id: str, entry: dict) -> tuple[Artifact, Path]:
    artifact = session.get(Artifact, entry.get("artifact_id")) if isinstance(entry.get("artifact_id"), str) else None
    if artifact is None or artifact.project_id != project_id or artifact.sha256 != entry.get("sha256"):
        raise ValueError("artifact missing, outside project, or hash metadata mismatch")
    return artifact, Path(artifact.path)


def _copy_verified(source: Path, target: Path, digest: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if _digest(target) != digest:
        raise ValueError("restored file hash mismatch")


def _publish(swaps: list[tuple[Path, Path]], staging: Path) -> None:
    """Rollback publication failures, retaining the new worker's existing files."""
    installed: list[tuple[Path, Path | None]] = []
    try:
        for index, (source, target) in enumerate(swaps):
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = staging / f"backup-{index}" if target.exists() else None
            if backup:
                target.replace(backup)
            installed.append((target, backup))
            source.replace(target)
    except OSError:
        for target, backup in reversed(installed):
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            if backup:
                backup.replace(target)
        raise


def restore_resume_manifest(
    session: Session, *, parent: Attempt, workspace: Path, legacy_home: Path, native_session: bool = True,
) -> tuple[bool, dict[str, Any]]:
    workspace.mkdir(parents=True, exist_ok=True)
    artifact = session.get(Artifact, parent.resume_manifest_artifact_id) if parent.resume_manifest_artifact_id else None
    if artifact is None or artifact.project_id != parent.project_id:
        return False, {"reason": "resume_manifest_missing", "parent_attempt_id": parent.id}
    try:
        raw = Path(artifact.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != artifact.sha256:
            raise ValueError("manifest artifact hash mismatch")
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or manifest.get("version", 1) not in {1, 2}:
            raise ValueError("unsupported resume manifest")
    except (OSError, ValueError) as exc:
        return False, {"reason": "resume_manifest_invalid", "detail": str(exc)[:500], "parent_attempt_id": parent.id}
    if (manifest.get("project_id") != parent.project_id or manifest.get("attempt_id") != parent.id
            or manifest.get("codex_thread_id") != parent.codex_thread_id):
        return False, {"reason": "resume_manifest_scope_mismatch", "parent_attempt_id": parent.id}
    diagnostic = {"parent_attempt_id": parent.id, "manifest_artifact_id": artifact.id}
    errors: list[dict[str, str]] = []
    partial_work = not native_session and manifest.get("version") == 2 and manifest.get("partial_work_state_available") is True
    completeness_keys = ["inputs_complete"] if partial_work else ["inputs_complete", "work_state_complete"]
    if native_session:
        completeness_keys.append("codex_state_complete")
    for key in completeness_keys:
        if not manifest.get(key):
            errors.append({"path": key, "reason": "incomplete_manifest"})
    if errors:
        return False, {**diagnostic, "reason": "resume_integrity_failed", "errors": errors}
    try:
        with tempfile.TemporaryDirectory(prefix=".resume-", dir=workspace) as temporary:
            staging = Path(temporary)
            (staging / "work").mkdir()
            (staging / "home").mkdir()
            manifest_path = workspace / "inputs" / "manifest.json"
            current_inputs = json.loads(manifest_path.read_text()) if manifest_path.is_file() else []
            if not isinstance(current_inputs, list):
                raise ValueError("invalid current input manifest")
            inputs_by_path = {entry["path"]: entry for entry in current_inputs if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
            swaps: list[tuple[Path, Path]] = []
            sections = ["inputs", "work_files"] + (["codex_state"] if native_session else [])
            for section in sections:
                entries = manifest.get(section)
                if not isinstance(entries, list) or (section == "codex_state" and not entries):
                    raise ValueError(f"invalid {section} manifest")
                paths: set[Path] = set()
                for entry in entries:
                    if not isinstance(entry, dict):
                        raise ValueError(f"invalid {section} entry")
                    relative = _relative(entry.get("path"), prefix="inputs" if section == "inputs" else None)
                    if relative in paths:
                        raise ValueError(f"duplicate {section} path")
                    paths.add(relative)
                    final = workspace / relative if section == "inputs" else workspace / ("work" if section == "work_files" else "runtime/codex-home") / relative
                    if not final.resolve().is_relative_to(workspace.resolve()):
                        raise ValueError("resume destination escapes workspace")
                    if section == "codex_state" and manifest.get("version", 1) == 1:
                        source = legacy_home / relative
                        if not source.resolve().is_relative_to(legacy_home.resolve()):
                            raise ValueError("legacy session path escapes source")
                    else:
                        source_artifact, source = _artifact_source(session, parent.project_id, entry)
                    if section == "inputs":
                        # Rehydrate old inputs even when they no longer fit the
                        # optional recent-input window of the new worker.
                        if final.is_file() and _digest(final) == entry["sha256"]:
                            if _digest(source) != entry["sha256"]:
                                raise ValueError("input artifact hash mismatch")
                        else:
                            staged_file = staging / relative
                            _copy_verified(source, staged_file, entry["sha256"])
                            swaps.append((staged_file, final))
                        inputs_by_path[str(relative)] = {
                            **entry,
                            "current_evidence": FlagValidator().is_current_evidence(session, source_artifact),
                            "environment_id": (source_artifact.evidence_context or {}).get("environment_id"),
                        }
                    else:
                        staged_file = staging / ("work" if section == "work_files" else "home") / relative
                        _copy_verified(source, staged_file, entry["sha256"])
            staged_manifest = staging / "input-manifest.json"
            staged_manifest.write_text(json.dumps(list(inputs_by_path.values()), ensure_ascii=False, indent=2), encoding="utf-8")
            swaps.append((staging / "work", workspace / "work"))
            if native_session:
                swaps.append((staging / "home", workspace / "runtime" / "codex-home"))
            swaps.append((staged_manifest, manifest_path))
            for _, target in swaps:
                if not target.resolve().is_relative_to(workspace.resolve()):
                    raise ValueError("resume destination escapes workspace")
            _publish(swaps, staging)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return False, {**diagnostic, "reason": "resume_integrity_failed", "errors": [{"path": "resume_bundle", "reason": str(exc)[:500]}]}
    return True, {**diagnostic, "reason": "resume_integrity_verified" if native_session else "work_state_restored",
                  "manifest_version": manifest.get("version", 1), "partial_work_state": partial_work,
                  "work_files_omitted": manifest.get("work_files_omitted", 0)}
