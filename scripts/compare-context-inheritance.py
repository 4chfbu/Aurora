#!/usr/bin/env python3
"""Compare context continuity with an unchanged source snapshot, without a model."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def probe(source: Path) -> dict:
    sys.path.insert(0, str(source))
    # Both source trees read the same local configuration; the overrides below
    # isolate storage and disable credential-bearing tools for the fixtures.
    os.chdir(Path(__file__).resolve().parents[1])
    with tempfile.TemporaryDirectory(prefix="aurora-context-comparison-") as temporary:
        root = Path(temporary)
        os.environ.update({
            "AURORA_DB_URL": f"sqlite:///{root / 'unused-global.db'}",
            "AURORA_ARTIFACT_DIR": str(root / "artifacts"),
            "AURORA_CODEX_WORKSPACE_DIR": str(root / "workers"),
            "AURORA_LLM_API_KEY": "", "OPENAI_API_KEY": "",
            "AURORA_FOFA_EMAIL": "", "AURORA_FOFA_KEY": "",
            "AURORA_CODEX_MODEL_CONTEXT_WINDOW": "1000000",
            "AURORA_CODEX_AUTO_COMPACT_TOKEN_LIMIT": "800000",
        })
        from sqlmodel import SQLModel, Session, create_engine
        from aurora.config import get_settings
        from aurora.models import Attempt, AttemptCheckpoint, Fact, Intent, Project, Worker
        from aurora.services.artifact_store import ArtifactStore
        from aurora.services.context_builder import ContextBuilder
        from aurora.services.prompt_renderer import PromptRenderer
        from aurora.services.worker_control import WorkerControlService
        from aurora.services.worker_runtime import CodexHarnessRuntime

        database = create_engine("sqlite://")
        SQLModel.metadata.create_all(database)
        settings = get_settings()
        settings.artifact_dir = root / "artifacts"
        settings.codex_workspace_dir = root / "workers"
        settings.debug.max_context_snapshot_bytes = 16_000
        result = {}
        with Session(database) as session:
            project = Project(name="bounded memory", goal="关键题目信息" * 20_000)
            intent = Intent(project_id=project.id, objective="use every required fact")
            facts = [Fact(project_id=project.id, statement=f"dependency {index}: " + "证据" * 100) for index in range(12)]
            intent.dependency_fact_ids = [fact.id for fact in facts]
            session.add_all([project, intent, *facts])
            session.commit()
            snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=intent.id)
            prompt = PromptRenderer().render_codex_task(worker=Worker(project_id=project.id, intent_id=intent.id), snapshot=snapshot)
            actual_bytes = len(json.dumps(snapshot.sections_json, ensure_ascii=False).encode())
            retained = {item["id"] for item in snapshot.sections_json["facts"]}
            result["bounded_context"] = {
                "limit_bytes": 16_000, "actual_bytes": actual_bytes, "within_limit": actual_bytes <= 16_000,
                "required_facts": 12, "retained_required_facts": len(retained & set(intent.dependency_fact_ids)),
                "full_memory_archived": bool(snapshot.sections_json.get("context_memory")),
                "rendered_prompt_bytes": len(prompt.encode()),
            }

            settings.debug.max_context_snapshot_bytes = 512_000
            project = Project(name="lineage", goal="continue the original branch")
            prior = Intent(project_id=project.id, objective="original branch")
            parent = Attempt(project_id=project.id, intent_id=prior.id, worker_id="parent", status="PARTIAL")
            current = Intent(project_id=project.id, objective="finish the branch", parent_intent_id=prior.id)
            worker = Worker(project_id=project.id, intent_id=current.id)
            attempt = Attempt(project_id=project.id, intent_id=current.id, worker_id=worker.id, parent_attempt_id=parent.id)
            fact = Fact(project_id=project.id, statement="decisive branch fact", source_attempt_id=parent.id)
            checkpoint = AttemptCheckpoint(project_id=project.id, intent_id=prior.id, attempt_id=parent.id, summary="branch handoff", fact_refs=[fact.id], next_steps=["validate the key"])
            session.add_all([project, prior, parent, current, worker, attempt, fact, checkpoint])
            session.commit()
            for index in range(60):
                session.add(Fact(project_id=project.id, statement=f"peer observation {index}"))
                session.add(AttemptCheckpoint(project_id=project.id, intent_id="peer", attempt_id=f"peer-{index}", summary="peer route"))
            session.commit()
            snapshot = ContextBuilder().build(session, project_id=project.id, intent_id=current.id, worker_id=worker.id)
            board = WorkerControlService().query(session, worker=worker, attempt=attempt)
            result["branch_handoff"] = {
                "parent_checkpoint_first": snapshot.sections_json["recent_checkpoints"][0]["id"] == checkpoint.id,
                "parent_fact_present": fact.id in {item["id"] for item in snapshot.sections_json["facts"]},
                "live_board_parent_first": board["checkpoints"][0]["id"] == checkpoint.id,
            }

            runtime = CodexHarnessRuntime(artifact_store=ArtifactStore(root / "artifacts"))
            project = Project(name="durable restore", goal="restore without old workspace")
            parent = Attempt(project_id=project.id, intent_id="original", worker_id="parent", status="PARTIAL", codex_thread_id="saved-thread")
            worker = Worker(project_id=project.id, intent_id="next")
            attempt = Attempt(project_id=project.id, intent_id=worker.intent_id, worker_id=worker.id, parent_attempt_id=parent.id)
            session.add_all([project, parent, worker, attempt])
            session.commit()
            workspace = settings.codex_workspace_dir / project.id / parent.worker_id
            (workspace / "runtime/codex-home/sessions").mkdir(parents=True)
            (workspace / "runtime/codex-home/sessions/thread.jsonl").write_text("retained conversation\n")
            (workspace / "work").mkdir()
            (workspace / "work/solve.py").write_text("print('saved algorithm')\n")
            (workspace / "inputs").mkdir()
            evidence = runtime.artifact_store.write_text(session, project_id=project.id, content="old input", summary="input", origin_kind="worker_observation")
            (workspace / "inputs/original.txt").write_text("old input")
            (workspace / "inputs/manifest.json").write_text(json.dumps([{"artifact_id": evidence.id, "path": "inputs/original.txt", "sha256": evidence.sha256}]))
            manifest = runtime._persist_resume_manifest(session, attempt=parent, workspace=workspace)
            manifest_version = json.loads(Path(manifest.path).read_text())["version"]
            shutil.rmtree(workspace)
            target = settings.codex_workspace_dir / project.id / worker.id
            thread = runtime._prepare_attempt(session, worker=worker, attempt=attempt, control_token="comparison", workspace=target)
            result["durable_restore"] = {
                "manifest_version": manifest_version, "thread_restored": thread == "saved-thread",
                "work_restored": (target / "work/solve.py").is_file(),
                "old_input_restored": (target / "inputs/original.txt").is_file(),
            }
        database.dispose()
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--probe-source", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe_source:
        print(json.dumps(probe(args.probe_source.resolve()), ensure_ascii=False))
        return
    if not args.baseline:
        parser.error("--baseline is required")
    comparison = {"method": "Identical deterministic fixtures; isolated SQLite and local files; no model, network or platform calls."}
    for name, path in (("before", args.baseline), ("after", args.candidate)):
        completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe-source", str(path.resolve())], cwd=path, check=True, capture_output=True, text=True)
        comparison[name] = json.loads(completed.stdout)
    rendered = json.dumps(comparison, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
