import json
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from aurora.api import create_app
from aurora.config import get_settings
from aurora.db import engine
from aurora.models import ContextSnapshot, Fact, Intent, LLMTrace, Project, ToolTrace, WorkerEvent, now_utc
from aurora.services.artifact_store import ArtifactStore
from aurora.services.event_stream import event_page
from aurora.services.tool_profiles import effective_challenge_type, profile_for_challenge
from aurora.services.worker_runtime import CodexHarnessRuntime


def test_event_pages_are_bounded_and_use_tie_breaking_cursor():
    with Session(engine) as session:
        timestamp = now_utc()
        session.add_all([WorkerEvent(id=f"event_{index:04}", project_id="project", event_type="test", created_at=timestamp) for index in range(250)])
        session.add(WorkerEvent(id="foreign", project_id="other", event_type="test", created_at=timestamp + timedelta(seconds=1)))
        session.commit()
        initial = event_page(session, "project", None)
        assert len(initial) == 100 and initial[0].id == "event_0150"
        page = event_page(session, "project", (timestamp, "event_0049"))
        assert len(page) == 100 and page[0].id == "event_0050" and page[-1].id == "event_0149"
        assert event_page(session, "project", (timestamp, "event_0249")) == []
        session.add(WorkerEvent(id="event_0250", project_id="project", event_type="test", created_at=timestamp))
        session.commit()
        assert [event.id for event in event_page(session, "project", (timestamp, "event_0249"))] == ["event_0250"]


def test_debug_lists_support_bounded_pages_and_summaries():
    with TestClient(create_app()) as client, Session(engine) as session:
        project = Project(name="pages", goal="bounded debug payload")
        session.add(project)
        timestamp = now_utc()
        for index in range(4):
            created_at = timestamp + timedelta(seconds=index)
            session.add(ContextSnapshot(project_id=project.id, intent_id="intent", sections_json={"large": "x" * 10000}, created_at=created_at))
            session.add(LLMTrace(project_id=project.id, worker_id="worker", intent_id="intent", context_snapshot_id="snapshot", prompt_hash="hash", structured_output={"large": "x" * 10000}, created_at=created_at))
            session.add(ToolTrace(project_id=project.id, tool_name="test", created_at=created_at))
        session.commit()
        for endpoint, omitted in (("context-snapshots", "sections_json"), ("llm-traces", "structured_output")):
            path = f"/api/projects/{project.id}/debug/{endpoint}"
            first = client.get(f"{path}?summary=true&limit=2").json()
            second = client.get(f"{path}?summary=true&limit=2&offset=2").json()
            assert len(first) == len(second) == 2
            assert {row["id"] for row in first}.isdisjoint(row["id"] for row in second)
            assert omitted not in first[0]
            assert omitted in client.get(f"{path}?limit=1").json()[0]
        assert len(client.get(f"/api/projects/{project.id}/debug/tool-traces?limit=2").json()) == 2


def test_unknown_routing_requires_unambiguous_web_evidence():
    assert effective_challenge_type("unknown", "Inspect this Flask website") == "web"
    assert profile_for_challenge(effective_challenge_type(None, "网站 SQL 注入")) == "core"
    for goal in ("Download https://example.com/firmware.bin", "Web interface for a binary pwn heap exploit", "Connect http://target.example", "Analyze the firmware"):
        assert profile_for_challenge(effective_challenge_type("unknown", goal)) == "heavy"
    assert effective_challenge_type("pwn", "PHP website") == "pwn"


def test_worker_inputs_preserve_dependencies_and_bound_optional_files(monkeypatch, tmp_path):
    monkeypatch.setenv("AURORA_WORKER_INPUT_MAX_FILES", "1")
    monkeypatch.setenv("AURORA_WORKER_INPUT_MAX_BYTES", "2")
    get_settings.cache_clear()
    with Session(engine) as session:
        project = Project(name="bounded inputs", goal="retain mandatory evidence")
        intent = Intent(project_id=project.id, objective="read dependency")
        session.add_all([project, intent])
        session.commit()
        store = ArtifactStore(tmp_path / "artifacts")
        source = tmp_path / "firmware.bin"
        source.write_bytes(b"mandatory original challenge bytes")
        challenge = store.write_file(session, project_id=project.id, source=source, summary="attachment", origin_kind="challenge_input")
        dependency = store.write_text(session, project_id=project.id, content="dependency", summary="needed", origin_kind="worker_observation")
        handoff = store.write_text(session, project_id=project.id, content="handoff", summary="handoff", origin_kind="worker_observation")
        store.write_text(session, project_id=project.id, content="old optional", summary="old", origin_kind="worker_observation")
        optional = store.write_text(session, project_id=project.id, content="ok", summary="recent", origin_kind="worker_observation")
        foreign = store.write_text(session, project_id="other", content="foreign", summary="foreign", origin_kind="challenge_input")
        fact = Fact(project_id=project.id, statement="dependency", evidence_refs=[dependency.id])
        intent.dependency_fact_ids = [fact.id]
        session.add_all([fact, intent])
        session.commit()
        snapshot = ContextSnapshot(project_id=project.id, intent_id=intent.id, sections_json={"handoff_artifacts": [{"id": handoff.id}, {"id": foreign.id}]})
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        CodexHarnessRuntime._materialize_project_inputs(session, snapshot, workspace)
        manifest = json.loads((workspace / "inputs/manifest.json").read_text())
        assert {entry["artifact_id"] for entry in manifest} == {challenge.id, dependency.id, handoff.id, optional.id}
        assert any(entry["path"].endswith("firmware.bin") for entry in manifest)
