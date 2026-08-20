from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from aurora.config import get_settings
from aurora.models import AuthorizationScope, ChallengeGroup, ChallengeGroupItem, Fact, FlagCandidate, Project, ToolTrace
from aurora.services.artifact_store import ArtifactStore
from aurora.services.capability_gateway import CapabilityGateway
from aurora.services.command_runner import LocalCommandRunner


class RecordingAdapter:
    def __init__(self, result: object) -> None:
        self.result = result
        self.values: list[str] = []

    def submit_flag(self, session, *, project_id, value):
        self.values.append(value)
        return self.result


def _setup(tmp_path: Path, monkeypatch, *, adapter_result: object = True):
    monkeypatch.setenv("AURORA_CODEX_WORKSPACE_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setenv("AURORA_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    get_settings.cache_clear()
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    adapter = RecordingAdapter(adapter_result)
    gateway = CapabilityGateway(
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        command_runner=LocalCommandRunner(),
        competition_adapter=adapter,
    )
    return gateway, adapter, engine


def _verify(gateway: CapabilityGateway, session: Session, tmp_path: Path) -> tuple[str, str]:
    project_id = "proj_submit"
    worker_id = "worker_submit"
    workspace = tmp_path / "workspaces" / project_id / worker_id
    workspace.mkdir(parents=True)
    workspace.joinpath("solve.py").write_text(
        "import codecs, json, pathlib, sys\n"
        "manifest = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
        "raw = pathlib.Path(manifest[0]['path']).read_text().strip()\n"
        "print(codecs.decode(raw, 'rot_13'))\n",
        encoding="utf-8",
    )
    session.add_all([
        Project(id=project_id, name="submit", goal="derive and submit"),
        AuthorizationScope(project_id=project_id),
        ChallengeGroup(id="group_submit", name="submit group", status="RUNNING", current_item_id="item_submit"),
        ChallengeGroupItem(
            id="item_submit",
            group_id="group_submit",
            project_id=project_id,
            position=1,
            competition_meta={"platform": "test"},
        ),
    ])
    session.commit()
    source = ArtifactStore(tmp_path / "artifacts").write_text(
        session,
        project_id=project_id,
        content="synt{fhozvggrq}",
        summary="encoded challenge input",
        artifact_type="imported_attachment",
        origin_kind="challenge_input",
    )
    result = gateway.execute(
        session,
        project_id=project_id,
        worker_id=worker_id,
        attempt_id="attempt_submit",
        tool_name="flag.verify",
        request={"source_artifact_refs": [source.id], "verification_script": "solve.py"},
    )
    assert result.success is True
    return project_id, worker_id


def test_verify_then_submit_latest_verified_candidate(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)
        candidate = session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project_id)).one()
        assert candidate.status == "LOCAL_VERIFIED"

        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"candidate_id": "latest_verified"},
        )

        session.refresh(candidate)
        project = session.get(Project, project_id)
        item = session.get(ChallengeGroupItem, "item_submit")
        group = session.get(ChallengeGroup, "group_submit")
        assert result.success is True
        assert result.metrics["status"] == "accepted"
        assert adapter.values == ["flag{submitted}"]
        assert candidate.status == "ACCEPTED"
        assert candidate.submission_count == 1
        assert project is not None and project.status == "COMPLETED"
        assert item is not None
        assert item.submission_status == "SUBMITTED"
        assert item.status == "COMPLETED"
        assert item.fused_status == "COMPLETED"
        assert item.finished_at is not None
        assert group is not None and group.status == "COMPLETED"
        assert group.current_item_id is None


def test_flag_submit_rejects_ambiguous_targets_and_duplicate_submission(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)
        raw = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            tool_name="flag.submit",
            request={"candidate_id": "latest_verified", "value": "flag{invented}"},
        )
        assert raw.success is False
        assert "requires exactly one of candidate_id or value" in raw.summary
        assert adapter.values == []

        accepted = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"candidate_id": "latest_verified"},
        )
        duplicate = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"candidate_id": accepted.metrics["candidate_id"]},
        )
        assert accepted.success is True
        assert duplicate.success is False
        assert duplicate.metrics["status"] == "duplicate"
        assert adapter.values == ["flag{submitted}"]


def test_flag_submit_accepts_raw_value_and_records_platform_decisions(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)

        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"value": "flag{direct_accept}"},
        )
        candidate = session.exec(
            select(FlagCandidate).where(
                FlagCandidate.project_id == project_id,
                FlagCandidate.value == "flag{direct_accept}",
            )
        ).one()
        assert result.success is True
        assert result.metrics["status"] == "accepted"
        assert adapter.values == ["flag{direct_accept}"]
        assert candidate.status == "ACCEPTED"
        assert candidate.submission_count == 1

        duplicate = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"value": "flag{direct_accept}"},
        )
        assert duplicate.success is False
        assert duplicate.metrics["status"] == "duplicate"
        assert adapter.values == ["flag{direct_accept}"]


def test_flag_submit_raw_value_platform_reject_records_feedback(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch, adapter_result=False)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)
        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            intent_id="intent_submit",
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"value": "flag{direct_reject}"},
        )

        candidate = session.exec(
            select(FlagCandidate).where(
                FlagCandidate.project_id == project_id,
                FlagCandidate.value == "flag{direct_reject}",
            )
        ).one()
        feedback = session.exec(
            select(Fact).where(
                Fact.project_id == project_id,
                Fact.category == "flag_validation_feedback",
                Fact.statement.contains("flag{direct_reject}"),
            )
        ).one()
        assert result.success is True
        assert result.metrics["status"] == "rejected"
        assert adapter.values == ["flag{direct_reject}"]
        assert candidate.status == "REJECTED"
        assert candidate.submission_count == 1
        assert "flag{direct_reject}" in feedback.statement


def test_flag_submit_raw_value_requires_valid_flag_shape(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)
        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"value": "not-a-flag"},
        )

        assert result.success is False
        assert result.metrics["status"] == "invalid_candidate"
        assert adapter.values == []


def test_flag_submit_preserves_running_group_when_another_item_is_active(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)
        other_project = Project(id="proj_other_running", name="other", goal="continue")
        other_item = ChallengeGroupItem(
            id="item_other_running",
            group_id="group_submit",
            project_id=other_project.id,
            position=2,
            status="RUNNING",
            fused_status="RUNNING",
        )
        group = session.get(ChallengeGroup, "group_submit")
        assert group is not None
        group.current_item_id = other_item.id
        session.add_all([other_project, other_item, group])
        session.commit()

        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"candidate_id": "latest_verified"},
        )

        session.refresh(group)
        assert result.success is True
        assert group.status == "RUNNING"
        assert group.current_item_id == other_item.id


def test_latest_verified_never_falls_back_to_an_older_attempt(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)
        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="different_attempt_after_failed_verify",
            tool_name="flag.submit",
            request={"candidate_id": "latest_verified"},
        )

        assert result.success is False
        assert result.metrics["status"] == "invalid_candidate"
        assert adapter.values == []


def test_flag_submit_rejects_candidate_from_another_project(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)
        foreign = FlagCandidate(
            project_id="proj_foreign",
            value="flag{foreign}",
            value_hash="foreign-value-hash",
            status="LOCAL_VERIFIED",
            provenance_kind="OBSERVED",
        )
        session.add(foreign)
        session.commit()

        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"candidate_id": foreign.id},
        )

        assert result.success is False
        assert result.metrics["status"] == "invalid_candidate"
        assert adapter.values == []


def test_rejected_submission_records_model_feedback(monkeypatch, tmp_path) -> None:
    gateway, adapter, engine = _setup(tmp_path, monkeypatch, adapter_result=False)
    with Session(engine) as session:
        project_id, worker_id = _verify(gateway, session, tmp_path)
        result = gateway.execute(
            session,
            project_id=project_id,
            worker_id=worker_id,
            intent_id="intent_submit",
            attempt_id="attempt_submit",
            tool_name="flag.submit",
            request={"candidate_id": "latest_verified"},
        )

        candidate = session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project_id)).one()
        feedback = session.exec(select(Fact).where(Fact.project_id == project_id, Fact.category == "flag_validation_feedback")).one()
        trace = session.exec(select(ToolTrace).where(ToolTrace.id == result.trace_id)).one()
        assert result.success is True
        assert result.metrics["status"] == "rejected"
        assert adapter.values == ["flag{submitted}"]
        assert candidate.status == "REJECTED"
        assert candidate.submission_count == 1
        assert "flag{submitted}" in feedback.statement
        assert trace.exit_code == 0
