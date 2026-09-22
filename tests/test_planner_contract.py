import json

import pytest
from sqlmodel import Session

from aurora.config import get_settings
from aurora.db import engine
from aurora.models import Fact, Intent, Project, ProjectRuntimePolicy
from aurora.services.project_reasoner import ProjectReasoner


@pytest.mark.parametrize("priority,expected", [(" HIGH ", 3.0), ("medium", 2.0), ("low", 1.0), ("2.75", 2.75), (4.5, 4.5)])
def test_planner_keeps_model_branch_with_compatible_priority(monkeypatch, priority, expected):
    monkeypatch.setenv("AURORA_LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    prompts = []

    def respond(**kwargs):
        prompts.append(json.loads(kwargs["messages"][0]["content"]))
        return {"choices": [{"message": {"content": json.dumps({"intents": [{
            "objective": "Validate the isolated parser boundary using the recorded input",
            "capabilities": ["blackboard.query"], "priority": priority, "risk_level": "low",
        }]})}}]}

    monkeypatch.setattr("aurora.services.project_reasoner.chat_completion", respond)
    with Session(engine) as session:
        project = Project(name="model priority", goal="continue a specific experiment")
        policy = ProjectRuntimePolicy(project_id=project.id, max_parallel_explorers=1)
        fact = Fact(project_id=project.id, statement="The parser boundary has been isolated")
        session.add_all([project, policy, fact])
        session.commit()
        reasoner = ProjectReasoner()
        created = reasoner._reason(session, project_id=project.id, policy=policy, facts=[fact], phase=2)
        intent = session.get(Intent, created[0])
        assert intent.objective == "Validate the isolated parser boundary using the recorded input"
        assert intent.priority == expected
        assert reasoner._last_reason_details["model_created"] == 1
        assert reasoner._last_reason_details["fallback_created"] == 0
        schema = prompts[0]["intent_schema"]["properties"]["priority"]
        assert schema["type"] == "number" and schema["minimum"] == 0 and schema["maximum"] == 100


@pytest.mark.parametrize("priority", [True, "urgent", "NaN", 101, -1, None])
def test_invalid_priority_is_reported_without_discarding_valid_sibling(monkeypatch, priority):
    monkeypatch.setenv("AURORA_LLM_API_KEY", "test-key")
    get_settings.cache_clear()
    response = {"intents": [
        {"objective": "invalid branch", "priority": priority},
        {"objective": "keep this valid specific branch", "priority": 2},
    ]}
    monkeypatch.setattr("aurora.services.project_reasoner.chat_completion", lambda **kwargs: {
        "choices": [{"message": {"content": json.dumps(response)}}],
    })
    with Session(engine) as session:
        project = Project(name="invalid priority", goal="inspect input", challenge_type="web")
        policy = ProjectRuntimePolicy(project_id=project.id, max_parallel_explorers=2)
        session.add_all([project, policy])
        session.commit()
        reasoner = ProjectReasoner()
        created = reasoner._reason(session, project_id=project.id, policy=policy, facts=[], phase=2)
        assert "keep this valid specific branch" in {session.get(Intent, ref).objective for ref in created}
        assert reasoner._last_reason_details["model_created"] == 1
        rejection = reasoner._last_reason_details["model_candidate_rejections"][0]
        assert rejection["fields"] == ["priority"]
