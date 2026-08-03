import os
from pathlib import Path

os.environ["AURORA_DB_URL"] = "sqlite:////tmp/aurora_test.db"
os.environ["AURORA_ARTIFACT_DIR"] = "/tmp/aurora_test_artifacts"

db_path = Path("/tmp/aurora_test.db")
if db_path.exists():
    db_path.unlink()

from fastapi.testclient import TestClient  # noqa: E402

from aurora.api import create_app  # noqa: E402


def test_autorun_start_completes_candidate_flag() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "autorun-flag", "goal": "自动执行并拿到 flag", "allowed_hosts": ["127.0.0.1"]},
        ).json()
        project_id = project["id"]
        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "输出 flag{autorun_ok}",
                "capability_tags": ["sandbox.exec"],
                "priority": 10,
                "risk_level": "low",
                "tool_request": {"command": "printf 'flag{autorun_ok}'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        assert intent.status_code == 200
        result = client.post(
            f"/api/projects/{project_id}/autorun/start",
            json={"max_iterations": 5, "max_minutes": 5, "no_progress_limit": 2, "stop_on_observer_escalate": True},
        )
        assert result.status_code == 200
        body = result.json()
        assert body["autorun"]["status"] == "completed"
        assert body["summary"]["project"]["status"] == "COMPLETED"
        assert body["summary"]["findings"][0]["title"] == "Candidate flag: flag{autorun_ok}"


def test_autorun_stops_on_observer_escalate() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "autorun-escalate", "goal": "触发观察器停止", "allowed_hosts": ["127.0.0.1"]},
        ).json()
        project_id = project["id"]
        denied = client.post(
            f"/api/projects/{project_id}/tools/http.request/execute",
            json={"request": {"url": "http://169.254.169.254/latest/meta-data/"}},
        )
        assert denied.status_code == 200
        result = client.post(f"/api/projects/{project_id}/autorun/start", json={"max_iterations": 5})
        assert result.status_code == 200
        body = result.json()
        assert body["autorun"]["status"] == "blocked"
        assert body["autorun"]["stop_reason"] == "observer_escalate"


def test_autorun_background_start_reports_status() -> None:
    client = TestClient(create_app())
    with client:
        project = client.post(
            "/api/projects",
            json={"name": "autorun-bg", "goal": "后台自动执行", "allowed_hosts": ["127.0.0.1"]},
        ).json()
        project_id = project["id"]
        intent = client.post(
            f"/api/projects/{project_id}/intents",
            json={
                "objective": "输出 flag{autorun_bg}",
                "capability_tags": ["sandbox.exec"],
                "priority": 10,
                "risk_level": "low",
                "tool_request": {"command": "printf 'flag{autorun_bg}'", "cwd": ".", "timeout_seconds": 5},
            },
        )
        assert intent.status_code == 200
        started = client.post(
            f"/api/projects/{project_id}/autorun/start",
            json={"max_iterations": 5, "max_minutes": 5, "no_progress_limit": 2, "background": True},
        )
        assert started.status_code == 200
        assert started.json()["autorun"]["project_id"] == project_id

        status = client.get(f"/api/projects/{project_id}/autorun/status")
        assert status.status_code == 200
        assert status.json()["background"]["project_id"] == project_id
