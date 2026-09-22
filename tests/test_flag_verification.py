from pathlib import Path
import hashlib

import pytest

from sqlmodel import Session, SQLModel, create_engine, select

from aurora.config import get_settings
from aurora.models import Artifact, Attempt, AuthorizationScope, ChallengeGroupItem, FlagCandidate, Project, ToolTrace, WorkerEvent
from aurora.services.artifact_store import ArtifactStore
from aurora.services.capability_gateway import CapabilityGateway
from aurora.services.command_runner import LocalCommandRunner
from aurora.services.flag_validator import FlagValidator


def _gateway(tmp_path: Path, monkeypatch) -> tuple[CapabilityGateway, object, str, str]:
    monkeypatch.setenv("AURORA_CODEX_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("AURORA_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    project_id = "proj_verify"
    worker_id = "worker_verify"
    gateway = CapabilityGateway(artifact_store=ArtifactStore(tmp_path / "artifacts"), command_runner=LocalCommandRunner())
    return gateway, engine, project_id, worker_id


@pytest.mark.parametrize("output", [
    "'flag{test}' => <title>500 Internal Server Error</title>",
    '{"detail":"File not found: flagtest"} <= /tmp/gradio/flag{test}',
])
def test_request_labels_do_not_become_observed_flags(monkeypatch, tmp_path, output) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    validator = FlagValidator(gateway.artifact_store)
    with Session(engine) as session:
        source = gateway.artifact_store.write_text(
            session, project_id=project_id, content=output + "\nflag{actual-response}",
            summary="network probe", artifact_type="terminal", origin_kind="target_observation",
            evidence_context=validator.request_evidence_context("curl http://target/flag{test}"),
        )
        candidates = validator.extract_candidate_flags(session, artifact_refs=[source.id], project_id=project_id)
        assert [candidate["value"] for candidate in candidates] == ["flag{actual-response}"]
        assert not validator.is_verified_candidate(session, value="flag{ACTUAL-response}", artifact_ref=source.id, project_id=project_id)


def test_legacy_shell_request_labels_are_excluded(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    with Session(engine) as session:
        source = gateway.artifact_store.write_text(
            session, project_id=project_id, content="flag{test} => HTTP 500",
            summary="legacy probe", artifact_type="terminal", origin_kind="target_observation",
        )
        session.add(ToolTrace(project_id=project_id, tool_name="codex.shell", request_json={"command": "curl http://target/flag{test}"}, artifact_refs=[source.id]))
        session.commit()
        assert FlagValidator().extract_candidate_flags(session, artifact_refs=[source.id], project_id=project_id) == []


def test_replay_cannot_launder_a_request_value(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    (workspace / "solve.py").write_text("import json,pathlib,sys\nmanifest=json.loads(pathlib.Path(sys.argv[1]).read_text())\nprint(pathlib.Path(manifest[0]['path']).read_text().strip())\n")
    with Session(engine) as session:
        source = gateway.artifact_store.write_text(
            session, project_id=project_id, content="flag{test}", summary="reflected request",
            artifact_type="terminal", origin_kind="target_observation",
            evidence_context=FlagValidator.request_evidence_context("curl --data 'flag{test}' http://target/"),
        )
        result = gateway.execute(session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify", request={"source_artifact_refs": [source.id], "verification_script": "solve.py"})
        assert not result.success
        assert "request input" in result.summary
        assert session.exec(select(FlagCandidate)).all() == []


def test_old_instance_replay_rejected_and_static_derivation_survives_restart(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    (workspace / "solve.py").write_text("import codecs,json,pathlib,sys\nmanifest=json.loads(pathlib.Path(sys.argv[1]).read_text())\nprint(codecs.decode(pathlib.Path(manifest[0]['path']).read_text(), 'rot_13'))\n")
    with Session(engine) as session:
        item = ChallengeGroupItem(group_id="group_instances", project_id=project_id, position=1, competition_meta={"environment_id": "instance-one"})
        session.add(item)
        session.commit()
        dynamic = gateway.artifact_store.write_text(session, project_id=project_id, content="synt{qlaNZVP}", summary="live data", origin_kind="target_observation")
        static = gateway.artifact_store.write_text(session, project_id=project_id, content="synt{fgngvp}", summary="attachment", origin_kind="challenge_input")
        verified = gateway.execute(session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify", request={"source_artifact_refs": [dynamic.id], "verification_script": "solve.py"})
        assert verified.success
        item.competition_meta = {"environment_id": "instance-two"}
        session.add(item)
        session.commit()
        assert FlagValidator().extract_candidate_flags(session, artifact_refs=verified.artifact_refs, project_id=project_id) == []
        stale = gateway.execute(session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify", request={"source_artifact_refs": [dynamic.id], "verification_script": "solve.py"})
        assert not stale.success and "stale" in stale.summary
        stable = gateway.execute(session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify", request={"source_artifact_refs": [static.id], "verification_script": "solve.py"})
        assert stable.success
        Path(static.path).write_text("changed attachment")
        assert FlagValidator().extract_candidate_flags(session, artifact_refs=stable.artifact_refs, project_id=project_id) == []


def test_late_output_keeps_original_instance_identity(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    with Session(engine) as session:
        attempt = Attempt(project_id=project_id, worker_id=worker_id, intent_id="intent_old", environment_id="instance-one")
        item = ChallengeGroupItem(group_id="group_instances", project_id=project_id, position=1, competition_meta={"environment_id": "instance-two"})
        session.add_all([attempt, item])
        session.commit()
        source = gateway.artifact_store.write_text(session, project_id=project_id, source_attempt_id=attempt.id, content="flag{late}", summary="late output", origin_kind="target_observation")
        assert source.evidence_context["environment_id"] == "instance-one"
        assert FlagValidator().extract_candidate_flags(session, artifact_refs=[source.id], project_id=project_id) == []


def test_reacquired_identical_file_belongs_to_new_instance(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    source = workspace / "encoded.out"
    source.write_text("synt{serfu}")
    (workspace / "solve.py").write_text(
        "import codecs,json,pathlib,sys\n"
        "entries=json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "entry=next(entry for entry in entries if entry['path'].endswith('_encoded.out'))\n"
        "print(codecs.decode(pathlib.Path(entry['path']).read_text(),'rot_13'))\n"
    )
    with Session(engine) as session:
        item = ChallengeGroupItem(group_id="group_instances", project_id=project_id, position=1, competition_meta={"environment_id": "old"})
        session.add(item)
        session.commit()
        old = gateway.artifact_store.write_file(session, project_id=project_id, source=source, summary="old download", artifact_type="worker-evidence", origin_kind="worker_observation", deduplicate=True)
        item.competition_meta = {"environment_id": "new"}
        session.add(item)
        session.commit()
        fresh = gateway.artifact_store.write_file(session, project_id=project_id, source=source, summary="fresh download", artifact_type="worker-evidence", origin_kind="worker_observation", deduplicate=True)
        repeated = gateway.artifact_store.write_file(session, project_id=project_id, source=source, summary="same instance", artifact_type="worker-evidence", origin_kind="worker_observation", deduplicate=True)
        assert fresh.id != old.id
        assert repeated.id == fresh.id
        assert old.evidence_context["environment_id"] == "old"
        assert fresh.evidence_context["environment_id"] == "new"
        for artifact, expected in [(old, False), (fresh, True)]:
            result = gateway.execute(session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify", request={"source_artifact_refs": [artifact.id], "verification_script": "solve.py"})
            assert result.success is expected


def test_stale_evidence_feedback_reports_current_evidence_and_repeats(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    (workspace / "solve.py").write_text("print('flag{unused}')\n", encoding="utf-8")
    with Session(engine) as session:
        item = ChallengeGroupItem(group_id="group_instances", project_id=project_id, position=1, competition_meta={"environment_id": "instance-one"})
        session.add(item)
        session.commit()
        stale = gateway.artifact_store.write_text(
            session, project_id=project_id, content="flag{stale}", summary="stale data", origin_kind="target_observation",
        )
        item.competition_meta = {"environment_id": "instance-two"}
        session.add(item)
        session.commit()
        fresh = gateway.artifact_store.write_text(
            session, project_id=project_id, content="flag{fresh}", summary="fresh data", origin_kind="target_observation",
        )

        request = {"source_artifact_refs": [stale.id], "verification_script": "solve.py"}
        first = gateway.execute(session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify", request=request)
        assert first.success is False
        assert "Current valid evidence" in first.summary
        assert fresh.id in first.summary
        assert stale.id not in first.summary.split("Current valid evidence")[1]

        second = gateway.execute(session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify", request=request)
        assert second.success is False
        assert "do not repeat it" in second.summary

        events = session.exec(
            select(WorkerEvent)
            .where(WorkerEvent.project_id == project_id, WorkerEvent.event_type == "flag.verify.stale_evidence")
            .order_by(WorkerEvent.created_at)
        ).all()
        assert [event.payload_json["prior_stale_attempts"] for event in events] == [0, 1]
        assert all(event.payload_json["current_evidence"] == [fresh.id] for event in events)


def test_file_deduplication_preserves_origin_and_rejects_corrupt_storage(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    store = ArtifactStore(tmp_path / "artifacts")
    source = tmp_path / "evidence.bin"
    source.write_bytes(b"original bytes")
    with Session(engine) as session:
        untrusted = store.write_file(session, project_id="project", source=source, summary="untrusted", origin_kind="model_output", deduplicate=True)
        observed = store.write_file(session, project_id="project", source=source, summary="observed", origin_kind="worker_observation", deduplicate=True)
        assert untrusted.id != observed.id
        Path(observed.path).write_bytes(b"corrupt bytes")
        repaired = store.write_file(session, project_id="project", source=source, summary="reacquired", origin_kind="worker_observation", deduplicate=True)
        assert repaired.id != observed.id
        assert Path(repaired.path).read_bytes() == source.read_bytes()


def test_flag_verify_replays_derivation_twice(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    workspace.joinpath("solve.py").write_text(
        "import codecs, json, pathlib, sys\n"
        "manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "raw = pathlib.Path(manifest[0]['path']).read_text().strip()\n"
        "print(codecs.decode(raw, 'rot_13'))\n",
        encoding="utf-8",
    )

    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        session.commit()
        source = ArtifactStore(tmp_path / "artifacts").write_text(
            session,
            project_id=project_id,
            content="synt{qrevirq}",
            summary="encoded challenge input",
            artifact_type="imported_attachment",
            origin_kind="challenge_input",
        )
        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            tool_name="flag.verify",
            request={"source_artifact_refs": [source.id], "verification_script": "solve.py"},
        )

        assert result.success is True
        artifact = session.get(Artifact, result.artifact_refs[0])
        assert artifact is not None and artifact.origin_kind == "verified_derivation"
        assert FlagValidator().extract_candidate_flags(session, artifact_refs=[artifact.id], project_id=project_id)[0]["value"] == "flag{derived}"
        trace = session.exec(select(ToolTrace).where(ToolTrace.id == result.trace_id)).one()
        assert trace.exit_code == 0


@pytest.mark.parametrize(("script", "expected_hint"), [
    ("import json,sys\njson.load(open(sys.argv[1])).get('files', [])\n", "JSON array"),
    ("open('inputs/inputs/missing.bin').read()\n", "working directory"),
    ("print('debug output')\n", "stdout"),
])
def test_verification_returns_actionable_replay_feedback(monkeypatch, tmp_path, script, expected_hint) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    (workspace / "solve.py").write_text(script)
    with Session(engine) as session:
        source = gateway.artifact_store.write_text(
            session, project_id=project_id, content="encoded input", summary="challenge",
            origin_kind="challenge_input",
        )
        result = gateway.execute(
            session, project_id=project_id, worker_id=worker_id,
            tool_name="flag.verify",
            request={"source_artifact_refs": [source.id], "verification_script": "solve.py"},
        )

        assert not result.success
        assert expected_hint in result.summary
        trace = session.get(ToolTrace, result.trace_id)
        assert expected_hint in trace.summary
        assert session.exec(select(FlagCandidate)).all() == []


def test_flag_verify_rehabilitates_legacy_local_rejection(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    workspace.joinpath("solve.py").write_text(
        "import codecs, json, pathlib, sys\n"
        "manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "raw = pathlib.Path(manifest[0]['path']).read_text().strip()\n"
        "print(codecs.decode(raw, 'rot_13'))\n",
        encoding="utf-8",
    )

    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        candidate = FlagCandidate(
            project_id=project_id,
            value="flag{local_retry}",
            value_hash=hashlib.sha256(b"flag{local_retry}").hexdigest(),
            status="REJECTED",
            provenance_kind="UNVERIFIED",
            rejection_reason="candidate was not present in trusted evidence",
        )
        session.add(candidate)
        session.commit()
        source = ArtifactStore(tmp_path / "artifacts").write_text(
            session,
            project_id=project_id,
            content="synt{ybpny_ergel}",
            summary="encoded challenge input",
            artifact_type="imported_attachment",
            origin_kind="challenge_input",
        )

        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            tool_name="flag.verify",
            request={"source_artifact_refs": [source.id], "verification_script": "solve.py"},
        )

        session.refresh(candidate)
        assert result.success is True
        assert candidate.status == "LOCAL_VERIFIED"
        assert candidate.rejection_reason is None


def test_flag_verify_preserves_platform_rejection(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    workspace.joinpath("solve.py").write_text(
        "import codecs, json, pathlib, sys\n"
        "manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "raw = pathlib.Path(manifest[0]['path']).read_text().strip()\n"
        "print(codecs.decode(raw, 'rot_13'))\n",
        encoding="utf-8",
    )

    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        candidate = FlagCandidate(
            project_id=project_id,
            value="flag{wrong}",
            value_hash=hashlib.sha256(b"flag{wrong}").hexdigest(),
            status="REJECTED",
            provenance_kind="DERIVED_REPLAY",
            submission_count=1,
            rejection_reason="competition platform rejected the candidate flag",
        )
        session.add(candidate)
        session.commit()
        source = ArtifactStore(tmp_path / "artifacts").write_text(
            session,
            project_id=project_id,
            content="synt{jebat}",
            summary="encoded challenge input",
            artifact_type="imported_attachment",
            origin_kind="challenge_input",
        )

        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            tool_name="flag.verify",
            request={"source_artifact_refs": [source.id], "verification_script": "solve.py"},
        )

        session.refresh(candidate)
        assert result.success is False
        assert "previously rejected" in result.summary
        assert candidate.status == "REJECTED"


def test_flag_verify_rejects_hardcoded_candidate(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    workspace.joinpath("solve.py").write_text("print('flag{hardcoded}')\n", encoding="utf-8")

    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        session.commit()
        source = ArtifactStore(tmp_path / "artifacts").write_text(
            session,
            project_id=project_id,
            content="unrelated input",
            summary="challenge input",
            artifact_type="imported_attachment",
            origin_kind="challenge_input",
        )
        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            tool_name="flag.verify",
            request={"source_artifact_refs": [source.id], "verification_script": "solve.py"},
        )

        assert result.success is False
        assert "hard-coded" in result.summary
        artifact = session.get(Artifact, result.artifact_refs[0])
        assert artifact is not None and artifact.origin_kind == "verification_failure"
        assert FlagValidator().extract_candidate_flags(session, artifact_refs=[artifact.id], project_id=project_id) == []


def test_flag_verify_accepts_container_workspace_script_path(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id / "runtime"
    workspace.mkdir(parents=True)
    workspace.joinpath("verify_flag.py").write_text("print('flag{mapped}')\n", encoding="utf-8")

    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        session.commit()
        source = ArtifactStore(tmp_path / "artifacts").write_text(
            session, project_id=project_id, content="input", summary="challenge input",
            artifact_type="imported_attachment", origin_kind="challenge_input",
        )
        result = gateway.execute(
            session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify",
            request={"source_artifact_refs": [source.id], "verification_script": "/workspace/runtime/verify_flag.py"},
        )

        assert result.success is False
        assert "hard-coded" in result.summary


def test_flag_verify_registers_worker_workspace_source_path(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    workspace.joinpath("evidence.txt").write_text("synt{jbexfcnpr}", encoding="utf-8")
    workspace.joinpath("solve.py").write_text(
        "import codecs, json, pathlib, sys\n"
        "manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "raw = pathlib.Path(manifest[0]['path']).read_text().strip()\n"
        "print(codecs.decode(raw, 'rot_13'))\n",
        encoding="utf-8",
    )

    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        session.commit()
        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_workspace_source",
            tool_name="flag.verify",
            request={
                "source_artifact_refs": ["/workspace/evidence.txt"],
                "verification_script": "/workspace/solve.py",
            },
        )
        source = session.exec(
            select(Artifact).where(
                Artifact.project_id == project_id,
                Artifact.type == "worker-evidence",
            )
        ).one()

    assert result.success is True
    assert result.metrics["candidate_id"] in result.summary
    assert source.origin_kind == "worker_observation"
    assert source.id in result.artifact_refs


def test_flag_verify_preserves_declared_worker_source_basename(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    (workspace / "flag.2.out").write_text("synt{anzrq_rivqrapr}", encoding="utf-8")
    (workspace / "solve.py").write_text(
        "import codecs, json, pathlib, sys\n"
        "entries = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "for entry in entries:\n"
        "    path = pathlib.Path(entry['path'])\n"
        "    if 'flag' in path.name and path.name.endswith('.out'):\n"
        "        print(codecs.decode(path.read_text(), 'rot_13'))\n",
        encoding="utf-8",
    )

    with Session(engine) as session:
        result = gateway.execute(
            session, project_id=project_id, worker_id=worker_id,
            tool_name="flag.verify",
            request={"source_artifact_refs": ["/workspace/flag.2.out"], "verification_script": "solve.py"},
        )

        assert result.success, result.summary
        candidate = session.get(FlagCandidate, result.metrics["candidate_id"])
        assert candidate.value == "flag{named_evidence}"
        assert result.metrics["runs"] == 2


def test_flag_verify_packages_source_with_original_extension(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    workspace.joinpath("solve.py").write_text(
        "import codecs, json, pathlib, sys\n"
        "manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "source = pathlib.Path(manifest[0]['path'])\n"
        "assert source.suffix == '.zip'\n"
        "print(codecs.decode(source.read_text().strip(), 'rot_13'))\n",
        encoding="utf-8",
    )
    source_path = tmp_path / "412.zip"
    source_path.write_text("synt{cnpxntrq}", encoding="utf-8")
    source_bytes = source_path.read_bytes()

    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        source = Artifact(
            id="artifact_zip_input",
            project_id=project_id,
            path=str(source_path),
            sha256=hashlib.sha256(source_bytes).hexdigest(),
            size=len(source_bytes),
            summary="archive challenge input",
            type="imported_attachment",
            mime_type="application/zip",
            origin_kind="challenge_input",
        )
        session.add(source)
        session.commit()

        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            tool_name="flag.verify",
            request={"source_artifact_refs": [source.id], "verification_script": "solve.py"},
        )

        assert result.success is True
        verified = session.get(Artifact, result.artifact_refs[0])
        assert verified is not None
        assert FlagValidator().extract_candidate_flags(
            session, artifact_refs=[verified.id], project_id=project_id
        )[0]["value"] == "flag{packaged}"


def test_flag_verify_reports_missing_inputs_separately(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        session.commit()

        missing_refs = gateway.execute(
            session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify",
            request={"verification_script": "solve.py"},
        )
        missing_script = gateway.execute(
            session, project_id=project_id, worker_id=worker_id, tool_name="flag.verify",
            request={"source_artifact_refs": ["artifact_input"]},
        )

    assert "non-empty source_artifact_refs" in missing_refs.summary
    assert "requires verification_script" in missing_script.summary


def test_flag_verify_accepts_inline_script_and_clamps_timeout(monkeypatch, tmp_path) -> None:
    gateway, engine, project_id, worker_id = _gateway(tmp_path, monkeypatch)
    inline_script = (
        "import codecs, json, pathlib, sys\n"
        "manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "raw = pathlib.Path(manifest[0]['path']).read_text().strip()\n"
        "print(codecs.decode(raw, 'rot_13'))\n"
    )
    with Session(engine) as session:
        session.add_all([Project(id=project_id, name="verify", goal="derive"), AuthorizationScope(project_id=project_id)])
        session.commit()
        source = ArtifactStore(tmp_path / "artifacts").write_text(
            session,
            project_id=project_id,
            content="synt{vayvar}",
            summary="encoded challenge input",
            artifact_type="imported_attachment",
            origin_kind="challenge_input",
        )

        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            tool_name="flag.verify",
            request={
                "source_artifact_refs": [source.id],
                "verification_script": inline_script,
                "timeout_seconds": 120,
            },
        )

        assert result.success is True
        assert result.metrics["effective_timeout_seconds"] == 60
        assert result.metrics["inline_script"] is True
        candidate = session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project_id)).one()
        assert candidate.value == "flag{inline}"
