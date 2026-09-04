from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from aurora.config import get_settings
from aurora.models import (
    Attempt,
    AttemptCheckpoint,
    ChallengeGroup,
    ChallengeGroupItem,
    EvaluationItemResult,
    EvaluationRun,
    EvaluationSuite,
    FlagCandidate,
    Project,
    ToolTrace,
    WorkerEvent,
    now_utc,
)
from aurora.services.demo import create_project_with_bootstrap
from aurora.services.agent_runtime import agent_runtime_settings
from aurora.services.flag_rejection import is_authoritative_flag_rejection
from aurora.services.tsecbench import TSecBenchClient
from aurora.services.tool_profiles import manifest_sha256
from aurora.services.tsecbench_phase_policy import (
    TSECBENCH_MAX_MINUTES_PER_CHALLENGE,
    TSECBENCH_PHASE_MINUTES,
)


@dataclass(frozen=True)
class EvaluationComparison:
    baseline_run_id: str
    candidate_run_id: str
    baseline_success_rate: float
    candidate_success_rate: float
    improvement_points: float
    promotion_gate_points: float
    repeated_request_reduction: float
    wrong_submission_rate_not_worse: bool
    sample_size: int
    promotion_eligible: bool
    promoted: bool


class EvaluationService:
    def create_suite(
        self,
        session: Session,
        *,
        name: str,
        challenge_codes: list[str] | None = None,
        include_completed: bool = False,
        client: TSecBenchClient | None = None,
    ) -> EvaluationSuite:
        requested = {value.strip() for value in (challenge_codes or []) if value.strip()}
        challenges = (client or TSecBenchClient()).list_challenges()
        items: list[dict[str, Any]] = []
        for challenge in challenges:
            if requested and challenge.unique_code not in requested:
                continue
            if (challenge.is_completed or challenge.correct_flag_count > 0) and not include_completed:
                continue
            raw_attachments = challenge.raw.get("attachments", challenge.raw.get("files", [])) if isinstance(challenge.raw, dict) else []
            if not isinstance(raw_attachments, list):
                raw_attachments = [raw_attachments]
            attachments = []
            for attachment in raw_attachments:
                if isinstance(attachment, str):
                    attachments.append({"url": attachment})
                elif isinstance(attachment, dict):
                    attachments.append({
                        key: attachment[key]
                        for key in ("name", "filename", "url", "sha256", "size")
                        if attachment.get(key) is not None
                    })
            snapshot = {
                "unique_code": challenge.unique_code,
                "title": challenge.title,
                "description": challenge.description,
                "challenge_type": challenge.challenge_type,
                "difficulty": challenge.difficulty,
                "level": challenge.level,
                "points": challenge.points,
                "flag_count": challenge.flag_count,
                "baseline_correct_flag_count": challenge.correct_flag_count,
                "baseline_completed": challenge.is_completed,
                "attachments": attachments,
                "platform": "tsecbench",
            }
            snapshot["version_hash"] = self._hash(snapshot)
            items.append(snapshot)
        if requested - {item["unique_code"] for item in items}:
            missing = sorted(requested - {item["unique_code"] for item in items})
            raise ValueError(f"challenge codes not available for a clean evaluation: {', '.join(missing)}")
        if not items:
            raise ValueError("evaluation suite has no eligible challenges")
        payload = sorted(items, key=lambda item: item["unique_code"])
        suite = EvaluationSuite(
            name=name.strip()[:240] or "Aurora evaluation",
            platform="tsecbench",
            items_json=payload,
            content_hash=self._hash(payload),
        )
        session.add(suite)
        session.commit()
        session.refresh(suite)
        return suite

    def create_run(
        self,
        session: Session,
        *,
        suite_id: str,
        label: str,
        variant: str = "candidate",
        client: TSecBenchClient | None = None,
    ) -> EvaluationRun:
        suite = session.get(EvaluationSuite, suite_id)
        if suite is None:
            raise ValueError("evaluation suite not found")
        if variant not in {"baseline", "candidate"}:
            raise ValueError("evaluation variant must be baseline or candidate")
        prior_runs = session.exec(
            select(EvaluationRun).where(EvaluationRun.suite_id == suite.id)
        ).all()
        for prior in prior_runs:
            prior_group = session.get(ChallengeGroup, prior.group_id) if prior.group_id else None
            if prior_group is None or prior_group.status not in {"COMPLETED", "FAILED", "STOPPED"}:
                raise ValueError("an earlier evaluation run for this suite is still active")
        attachment_items = [
            str(item.get("unique_code") or "unknown")
            for item in suite.items_json
            if item.get("attachments")
        ]
        if attachment_items:
            raise ValueError(
                "evaluation attachment materialization is not implemented; "
                f"cannot run: {', '.join(attachment_items)}"
            )
        live_challenges = {
            challenge.unique_code: challenge
            for challenge in (client or TSecBenchClient()).list_challenges()
        }
        dirty_items: list[str] = []
        missing_items: list[str] = []
        for item in suite.items_json:
            code = str(item.get("unique_code") or "")
            live = live_challenges.get(code)
            if live is None:
                missing_items.append(code or "unknown")
            elif live.is_completed or live.correct_flag_count > 0:
                dirty_items.append(code)
        if missing_items:
            raise ValueError(f"evaluation challenges are no longer available: {', '.join(missing_items)}")
        if dirty_items:
            raise ValueError(
                "evaluation requires a clean platform session; challenges already contain accepted progress: "
                f"{', '.join(dirty_items)}"
            )
        settings = get_settings()
        agent_runtime = agent_runtime_settings(session)
        config = {
            "models": {
                role: {
                    "id": settings.model_for_role(role),
                    "context_window": settings.codex_metadata_for_role(role)[0],
                    "auto_compact_token_limit": settings.codex_metadata_for_role(role)[1],
                }
                for role in ("triage", "solver", "reviewer")
            },
            "prompt_contract": "strict-v1",
            "tool_manifest_sha256": manifest_sha256(),
            "suite_content_hash": suite.content_hash,
            "max_minutes_per_challenge": TSECBENCH_MAX_MINUTES_PER_CHALLENGE,
            "phase_minutes": list(TSECBENCH_PHASE_MINUTES),
            "max_route_repeats": 2,
        }
        run = EvaluationRun(
            suite_id=suite.id,
            label=label.strip()[:240] or variant,
            variant=variant,
            config_json=config,
        )
        group = ChallengeGroup(
            name=f"Evaluation: {suite.name} [{run.label}]"[:240],
            limits={"max_iterations": 0, "max_minutes": 60, "no_progress_limit": 3, "stop_on_observer_escalate": True},
            max_concurrent=max(1, int(settings.tsecbench_max_concurrent or 1)),
        )
        session.add(run)
        session.add(group)
        session.commit()
        session.refresh(run)
        session.refresh(group)
        run.group_id = group.id
        session.add(run)
        for position, item in enumerate(suite.items_json, start=1):
            code = str(item["unique_code"])
            project = create_project_with_bootstrap(
                session,
                name=str(item.get("title") or code)[:240],
                goal=str(item.get("description") or f"Solve platform challenge {code}"),
                challenge_type=str(item.get("challenge_type") or "unknown"),
                allowed_hosts=[],
                hint="Use only the authorized platform instance and imported challenge evidence.",
                multi_agent_exploration_enabled=agent_runtime.multi_agent_exploration_enabled,
                max_parallel_explorers=agent_runtime.default_max_project_workers,
            )
            meta = {**item, "platform": suite.platform, "provenance": "evaluation_snapshot"}
            group_item = ChallengeGroupItem(
                group_id=group.id,
                project_id=project.id,
                position=position,
                competition_meta=meta,
            )
            result = EvaluationItemResult(run_id=run.id, challenge_key=code, project_id=project.id)
            session.add(group_item)
            session.add(result)
        session.commit()
        return run

    def refresh(self, session: Session, *, run_id: str) -> dict[str, Any]:
        run = session.get(EvaluationRun, run_id)
        if run is None:
            raise ValueError("evaluation run not found")
        group = session.get(ChallengeGroup, run.group_id) if run.group_id else None
        group_items = session.exec(select(ChallengeGroupItem).where(ChallengeGroupItem.group_id == run.group_id)).all() if run.group_id else []
        by_project = {item.project_id: item for item in group_items}
        results = session.exec(select(EvaluationItemResult).where(EvaluationItemResult.run_id == run.id)).all()
        for result in results:
            project = session.get(Project, result.project_id) if result.project_id else None
            item = by_project.get(result.project_id or "")
            attempts = session.exec(select(Attempt).where(Attempt.project_id == result.project_id)).all() if result.project_id else []
            traces = session.exec(select(ToolTrace).where(ToolTrace.project_id == result.project_id)).all() if result.project_id else []
            events = session.exec(select(WorkerEvent).where(WorkerEvent.project_id == result.project_id)).all() if result.project_id else []
            candidates = session.exec(select(FlagCandidate).where(FlagCandidate.project_id == result.project_id)).all() if result.project_id else []
            checkpoints = session.exec(select(AttemptCheckpoint).where(AttemptCheckpoint.project_id == result.project_id)).all() if result.project_id else []
            submission_status = item.submission_status if item else "NOT_SUBMITTED"
            completed = bool(
                project
                and project.status == "COMPLETED"
                and submission_status in {"SUBMITTED", "MANUALLY_ACCEPTED", "ACCEPTED", "COMPLETED"}
            )
            platform_correct = True if completed else False if submission_status == "REJECTED" else None
            environment_error = bool(item and item.fused_status in {"WAITING_INPUT", "CRASHED"} and not attempts)
            started = min((attempt.started_at for attempt in attempts), default=None)
            finished = max((attempt.finished_at for attempt in attempts if attempt.finished_at), default=None)
            result.status = "COMPLETED" if completed else (item.fused_status if item else project.status if project else "MISSING")
            result.platform_correct = platform_correct
            result.platform_completed = completed
            result.environment_error = environment_error
            result.elapsed_seconds = max(0.0, (finished - started).total_seconds()) if started and finished else None
            result.metrics_json = {
                "attempts": len(attempts),
                "shell_actions": sum(trace.tool_name == "codex.shell" for trace in traces),
                "repeated_requests": self._repeated_requests(traces),
                "flag_verify_calls": sum(trace.tool_name == "flag.verify" for trace in traces),
                "wrong_candidates": sum(is_authoritative_flag_rejection(candidate) for candidate in candidates),
                "wrong_submissions": int(submission_status in {"REJECTED", "MANUALLY_REJECTED"}),
                "derived_candidates": sum(
                    candidate.provenance_kind.upper() in {"DERIVED_REPLAY", "VERIFIED_REPLAY"}
                    for candidate in candidates
                ),
                "verified_derived_candidates": sum(
                    candidate.provenance_kind.upper() in {"DERIVED_REPLAY", "VERIFIED_REPLAY"}
                    and bool(candidate.verification_artifact_ref)
                    for candidate in candidates
                ),
                "terminal_attempts": sum(attempt.status in {"SUCCESS", "PARTIAL", "FAILED", "TIMEOUT"} for attempt in attempts),
                "checkpointed_terminal_attempts": sum(
                    attempt.status in {"SUCCESS", "PARTIAL", "FAILED", "TIMEOUT"}
                    and any(checkpoint.attempt_id == attempt.id for checkpoint in checkpoints)
                    for attempt in attempts
                ),
                "checkpoints": sum(event.event_type in {"checkpoint.saved", "attempt.checkpoint_created"} for event in events),
                "resume_scheduled": sum(event.event_type == "codex.resume_scheduled" for event in events),
                "resume_rejected": sum(event.event_type == "codex.resume_rejected" for event in events),
            }
            result.updated_at = now_utc()
            session.add(result)
        if group and group.status in {"RUNNING", "READY"}:
            run.status = group.status
            run.started_at = run.started_at or group.created_at
        elif group:
            run.status = group.status
            run.started_at = run.started_at or group.created_at
            run.finished_at = group.finished_at if group.status == "COMPLETED" else None
        session.add(run)
        session.commit()
        return self.report(session, run_id=run.id)

    def report(self, session: Session, *, run_id: str) -> dict[str, Any]:
        run = session.get(EvaluationRun, run_id)
        if run is None:
            raise ValueError("evaluation run not found")
        suite = session.get(EvaluationSuite, run.suite_id)
        results = session.exec(select(EvaluationItemResult).where(EvaluationItemResult.run_id == run.id)).all()
        eligible = [result for result in results if not result.environment_error]
        completed = sum(result.platform_completed for result in eligible)
        wrong = sum((result.metrics_json or {}).get("wrong_submissions", 0) for result in eligible)
        derived = sum((result.metrics_json or {}).get("derived_candidates", 0) for result in eligible)
        verified_derived = sum((result.metrics_json or {}).get("verified_derived_candidates", 0) for result in eligible)
        terminal_attempts = sum((result.metrics_json or {}).get("terminal_attempts", 0) for result in eligible)
        checkpointed = sum((result.metrics_json or {}).get("checkpointed_terminal_attempts", 0) for result in eligible)
        snapshots = {str(item["unique_code"]): item for item in (suite.items_json if suite else [])}
        strata: dict[str, dict[str, dict[str, int | float]]] = {"challenge_type": {}, "difficulty": {}}
        for dimension in strata:
            grouped: dict[str, list[EvaluationItemResult]] = {}
            for result in eligible:
                value = str(snapshots.get(result.challenge_key, {}).get(dimension) or "unknown")
                grouped.setdefault(value, []).append(result)
            strata[dimension] = {
                value: {
                    "eligible": len(items),
                    "completed": sum(item.platform_completed for item in items),
                    "success_rate": sum(item.platform_completed for item in items) / len(items),
                }
                for value, items in grouped.items()
            }
        return {
            "run": run,
            "suite": suite,
            "results": results,
            "metrics": {
                "total": len(results),
                "eligible": len(eligible),
                "environment_errors": len(results) - len(eligible),
                "completed": completed,
                "success_rate": completed / len(eligible) if eligible else 0.0,
                "success_rate_ci95": self._wilson(completed, len(eligible)),
                "wrong_submissions": wrong,
                "rejected_candidates": sum((result.metrics_json or {}).get("wrong_candidates", 0) for result in eligible),
                "wrong_submission_rate": wrong / len(eligible) if eligible else 0.0,
                "repeated_requests": sum((result.metrics_json or {}).get("repeated_requests", 0) for result in eligible),
                "flag_verify_calls": sum((result.metrics_json or {}).get("flag_verify_calls", 0) for result in eligible),
                "derived_verification_coverage": verified_derived / derived if derived else 1.0,
                "terminal_checkpoint_coverage": checkpointed / terminal_attempts if terminal_attempts else 1.0,
            },
            "strata": strata,
        }

    def compare(self, session: Session, *, baseline_run_id: str, candidate_run_id: str) -> EvaluationComparison:
        baseline = self.report(session, run_id=baseline_run_id)
        candidate = self.report(session, run_id=candidate_run_id)
        if baseline["run"].suite_id != candidate["run"].suite_id:
            raise ValueError("evaluation runs must use the same frozen suite")
        baseline_rate = float(baseline["metrics"]["success_rate"])
        candidate_rate = float(candidate["metrics"]["success_rate"])
        improvement = (candidate_rate - baseline_rate) * 100
        wrong_not_worse = candidate["metrics"]["wrong_submission_rate"] <= baseline["metrics"]["wrong_submission_rate"]
        baseline_repeats = int(baseline["metrics"]["repeated_requests"])
        candidate_repeats = int(candidate["metrics"]["repeated_requests"])
        repeat_reduction = (
            (baseline_repeats - candidate_repeats) / baseline_repeats
            if baseline_repeats
            else (0.0 if candidate_repeats == 0 else -1.0)
        )
        repeat_gate = candidate_repeats <= baseline_repeats if baseline_repeats == 0 else repeat_reduction >= 0.5
        quality_gates = (
            candidate["metrics"]["derived_verification_coverage"] == 1.0
            and candidate["metrics"]["terminal_checkpoint_coverage"] == 1.0
        )
        sample_size = min(int(baseline["metrics"]["eligible"]), int(candidate["metrics"]["eligible"]))
        promotion_eligible = sample_size >= 30
        return EvaluationComparison(
            baseline_run_id=baseline_run_id,
            candidate_run_id=candidate_run_id,
            baseline_success_rate=baseline_rate,
            candidate_success_rate=candidate_rate,
            improvement_points=improvement,
            promotion_gate_points=15.0,
            repeated_request_reduction=repeat_reduction,
            wrong_submission_rate_not_worse=wrong_not_worse,
            sample_size=sample_size,
            promotion_eligible=promotion_eligible,
            promoted=promotion_eligible and improvement >= 15.0 and wrong_not_worse and repeat_gate and quality_gates,
        )

    @staticmethod
    def _hash(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()

    @staticmethod
    def _repeated_requests(traces: list[ToolTrace]) -> int:
        counts: dict[str, int] = {}
        for trace in traces:
            key = f"{trace.tool_name}:{json.dumps(trace.request_json, sort_keys=True, default=str)}"
            counts[key] = counts.get(key, 0) + 1
        return sum(max(0, count - 1) for count in counts.values())

    @staticmethod
    def _wilson(successes: int, total: int) -> list[float]:
        if total <= 0:
            return [0.0, 0.0]
        z = 1.959963984540054
        proportion = successes / total
        denominator = 1 + z * z / total
        centre = (proportion + z * z / (2 * total)) / denominator
        margin = z * ((proportion * (1 - proportion) / total + z * z / (4 * total * total)) ** 0.5) / denominator
        return [max(0.0, centre - margin), min(1.0, centre + margin)]
