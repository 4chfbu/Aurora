import json

from sqlmodel import Session

from aurora.models import Attempt, ContextSnapshot, Worker
from aurora.services.subagent_collector import SubagentCollector


def test_subagent_collector_imports_manifest_as_child_records(tmp_path) -> None:
    from aurora.db import engine, init_db

    init_db()
    workspace = tmp_path / "worker"
    manifest = workspace / "subagents" / "manifest.jsonl"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "run_id": "subagent_test_record",
                "objective": "Verify an independent route.",
                "capability_tags": ["blackboard.query"],
                "exit_code": 0,
                "output": {"status": "partial", "summary": "Found one useful fact.", "decision_summary": {"selected_intent": "Verify", "reason_summary": "Evidence found.", "next_tool_plan": []}},
                "prompt": "child prompt",
                "transcript": "child transcript",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with Session(engine) as session:
        parent = Worker(id="parent_subagent_test", project_id="proj_subagent_test", intent_id="intent_subagent_test")
        attempt = Attempt(id="attempt_subagent_test", project_id=parent.project_id, intent_id=parent.intent_id, worker_id=parent.id)
        snapshot = ContextSnapshot(id="ctx_subagent_test", project_id=parent.project_id, intent_id=parent.intent_id, worker_id=parent.id, sections_json={"project_goal": "test"}, visible_tools_json=[{"name": "blackboard.query"}, {"name": "subagent.spawn"}], output_schema_json={})
        session.add(parent)
        session.add(attempt)
        session.add(snapshot)
        session.commit()
        reports = SubagentCollector().collect(session, parent_worker=parent, parent_attempt=attempt, parent_snapshot=snapshot, workspace=workspace)
        assert reports[0]["worker_id"] == "subagent_test_record"
        assert reports[0]["status"] == "partial"
        child = session.get(Worker, "subagent_test_record")
        assert child is not None
        assert child.parent_worker_id == parent.id
        assert child.execution_kind == "subagent"
