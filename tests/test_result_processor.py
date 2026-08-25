import hashlib

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from aurora.models import Artifact, Attempt, Finding, FlagCandidate, Intent, LLMTrace, Project, Worker
from aurora.services.artifact_store import ArtifactStore
from aurora.services.flag_validator import FlagValidator
from aurora.services.result_processor import ResultProcessor


@pytest.mark.parametrize(
    "value",
    [
        "flag{*****}",
        "flag{abc***}",
        "flag{...}",
        "flag{a\tb}",
        "flag{a\u0001b}",
        "flag{a\ufffdb}",
        "flag{a\u200bb}",
        "flag{a\ufdd0b}",
    ],
)
def test_flag_validator_rejects_masked_and_unreadable_payloads(value: str) -> None:
    assert not FlagValidator.is_valid_flag_value(value)


@pytest.mark.parametrize("value", ["flag{readable-value_42?}", "qwxf{\u4e2d\u6587}"])
def test_flag_validator_accepts_printable_payloads(value: str) -> None:
    assert FlagValidator.is_valid_flag_value(value)


@pytest.mark.parametrize(
    "value",
    [
        "body{color:#000;background:#fff;margin:0}",
        "h1{border-right:1px solid rgba(0,0,0,.3)}",
        "const{value:1}",
        "let{url:t,body:a}",
        "return{valid_payload}",
        "function{valid_payload}",
        "window{valid_payload}",
    ],
)
def test_flag_validator_rejects_javascript_style_prefixes(value: str) -> None:
    assert not FlagValidator.is_valid_flag_value(value)


def test_result_processor_normalizes_model_confidence_labels() -> None:
    processor = ResultProcessor()

    assert processor._confidence("high") == 0.8
    assert processor._confidence("VERY HIGH") == 0.95
    assert processor._confidence("unknown") == 0.5
    assert processor._confidence(4) == 1.0
    assert processor._confidence(float("nan")) == 0.5


def test_result_processor_normalizes_priority_and_ignores_malformed_entries() -> None:
    processor = ResultProcessor()

    assert processor._priority("high") == 0.8
    assert processor._priority("unexpected") == 0.5
    assert processor._objects([{"statement": "valid"}, "invalid", None]) == [{"statement": "valid"}]
    assert processor._objects("not-a-list") == []


def _attempt(session: Session, project: Project) -> tuple[Attempt, LLMTrace]:
    intent = Intent(project_id=project.id, objective="recover flag", status="RUNNING")
    worker = Worker(project_id=project.id, intent_id=intent.id, status="RUNNING")
    attempt = Attempt(project_id=project.id, intent_id=intent.id, worker_id=worker.id)
    trace = LLMTrace(project_id=project.id, worker_id=worker.id, intent_id=intent.id, context_snapshot_id="ctx_test", prompt_hash="test")
    session.add_all([intent, worker, attempt, trace])
    session.commit()
    return attempt, trace


def test_model_only_flag_is_rejected_without_completing_project(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="hallucination", goal="find real flag")
        session.add(project)
        session.commit()
        attempt, trace = _attempt(session, project)

        ResultProcessor().apply(
            session,
            attempt=attempt,
            llm_trace=trace,
            output={"status": "success", "candidate_flags": ["flag{invented_but_plausible}"]},
        )

        session.refresh(project)
        candidate = session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project.id)).one()
        assert project.status == "ACTIVE"
        assert candidate.status == "REJECTED"
        assert session.exec(select(Finding).where(Finding.project_id == project.id)).all() == []


def test_trusted_observed_flag_becomes_ready_but_not_completed(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="observed", goal="find real flag")
        session.add(project)
        session.commit()
        attempt, trace = _attempt(session, project)
        artifact = ArtifactStore(tmp_path).write_text(
            session,
            project_id=project.id,
            source_attempt_id=attempt.id,
            content="HTTP/1.1 200 OK\n\nflag{from_target}",
            summary="authorized target response",
            artifact_type="terminal",
            origin_kind="target_observation",
        )

        ResultProcessor().apply(
            session,
            attempt=attempt,
            llm_trace=trace,
            output={
                "status": "success",
                "artifact_refs": [artifact.id],
                "candidate_flags": [{"value": "flag{from_target}", "artifact_ref": artifact.id}],
            },
        )

        session.refresh(project)
        candidate = session.exec(select(FlagCandidate).where(FlagCandidate.project_id == project.id)).one()
        assert project.status == "FLAG_READY"
        assert candidate.status == "LOCAL_VERIFIED"
        assert candidate.provenance_kind == "OBSERVED"


def test_platform_rejected_candidate_is_not_revived_by_post_tool_artifact_scan(tmp_path) -> None:
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        project = Project(name="platform-rejected", goal="fix the submitted format")
        session.add(project)
        session.commit()
        attempt, trace = _attempt(session, project)
        artifact = ArtifactStore(tmp_path).write_text(
            session,
            project_id=project.id,
            source_attempt_id=attempt.id,
            content='{"status":"verified","value":"flag{wrong_wrapper}"}',
            summary="verified derivation",
            artifact_type="flag-verification",
            origin_kind="verified_derivation",
        )
        candidate = FlagCandidate(
            project_id=project.id,
            value="flag{wrong_wrapper}",
            value_hash=hashlib.sha256(b"flag{wrong_wrapper}").hexdigest(),
            status="REJECTED",
            provenance_kind="DERIVED_REPLAY",
            artifact_refs=[artifact.id],
            verification_artifact_ref=artifact.id,
            submission_count=1,
            rejection_reason="competition platform rejected the candidate flag",
        )
        session.add(candidate)
        session.commit()

        ResultProcessor().apply(
            session,
            attempt=attempt,
            llm_trace=trace,
            output={
                "status": "success",
                "artifact_refs": [artifact.id],
                "candidate_flags": [{"value": candidate.value, "artifact_ref": artifact.id}],
            },
        )

        session.refresh(candidate)
        session.refresh(project)
        assert candidate.status == "REJECTED"
        assert project.status == "ACTIVE"
        assert session.exec(select(Finding).where(Finding.project_id == project.id)).all() == []
