from datetime import timedelta
from pathlib import Path
import time
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from aurora.models import ChallengeGroupItem, now_utc
from aurora.services.challenge_group_runner import ChallengeGroupRunner
from aurora.services.deadlines import as_utc
from aurora.services.llm_http import LLMRequestError, chat_completion
from aurora.services.worker_runtime import CodexHarnessRuntime
from aurora.services.command_runner import KaliContainerRunner
from aurora.models import Worker
from aurora.db import engine
from sqlmodel import Session


def test_phase_window_is_clamped_to_global_deadline():
    deadline = now_utc() + timedelta(seconds=40)
    item = ChallengeGroupItem(group_id="group", project_id="project", position=0)
    ChallengeGroupRunner._ensure_phase_window(item, deadline_at=deadline.replace(tzinfo=None))
    assert as_utc(item.phase_deadline_at) == deadline


def test_worker_never_starts_after_deadline(tmp_path):
    class UnusedRunner:
        def run(self, **kwargs):
            raise AssertionError("expired worker launched")
    result = CodexHarnessRuntime()._run_command("codex", Path(tmp_path), deadline_at=now_utc() - timedelta(seconds=1), runner=UnusedRunner())
    assert result.exit_code == 124


def test_llm_retries_share_an_absolute_deadline(monkeypatch):
    clock = now_utc()
    monkeypatch.setattr("aurora.services.deadlines.now_utc", lambda: clock)
    timeouts = []
    def urlopen(request, timeout):
        nonlocal clock
        timeouts.append(timeout)
        clock += timedelta(seconds=timeout)
        raise URLError("timeout")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr("aurora.services.llm_http.time.sleep", lambda delay: None)
    with pytest.raises(LLMRequestError, match="deadline"):
        chat_completion(model="test", messages=[], timeout=30, deadline_at=clock + timedelta(seconds=3))
    assert timeouts == [3]


def test_streaming_hard_deadline_interrupts_partial_lines_without_extra_grace(monkeypatch, tmp_path):
    runner = KaliContainerRunner()
    runner.engine = "test"
    command = ["bash", "-c", "trap '' INT; printf partial; sleep 30"]
    monkeypatch.setattr(runner, "_build_command", lambda *_: (command, Path("worker"), Path("/workspace")))
    monkeypatch.setattr(runner, "_stop_container", lambda *_: None)
    started = time.monotonic()
    result = runner.run_streaming(command="partial line", cwd=tmp_path, timeout=1, finalize_grace=20, on_output=lambda *_: None)
    assert time.monotonic() - started < 4
    assert result.stdout == "partial"
    assert result.exit_code == 124


def test_conclude_fallback_does_not_launch_after_phase_deadline(tmp_path):
    with Session(engine) as session:
        worker = Worker(project_id="project", intent_id="intent", budgets={"phase_deadline_at": (now_utc() - timedelta(seconds=1)).isoformat()})
        session.add(worker)
        session.commit()
        outcome = CodexHarnessRuntime()._try_conclude_fallback(
            session, worker=worker, attempt=None, snapshot=SimpleNamespace(), workspace=tmp_path,
            model="test", runner=SimpleNamespace(), primary_completed=SimpleNamespace(finalization_reason=None), primary_diagnostic={},
        )
        assert not outcome.attempted and outcome.diagnostic["skip_reason"] == "deadline_exhausted"
