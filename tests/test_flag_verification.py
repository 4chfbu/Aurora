from pathlib import Path
import hashlib

from sqlmodel import Session, SQLModel, create_engine, select

from aurora.config import get_settings
from aurora.models import Artifact, AuthorizationScope, FlagCandidate, Project, ToolTrace
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
