from aurora.services.solver_playbooks import select_playbook


def test_unknown_challenge_is_classified_from_evidence() -> None:
    playbook = select_playbook("unknown", "题目提供 login API，需要测试 SSRF")
    assert playbook.challenge_type == "web"
    assert playbook.confidence == 0.65
    assert "http.request" in playbook.capabilities


def test_explicit_type_wins_over_conflicting_text() -> None:
    playbook = select_playbook("reverse", "web api and login")
    assert playbook.challenge_type == "reverse"
    assert playbook.confidence == 1.0


def test_unknown_playbook_has_bounded_first_steps() -> None:
    playbook = select_playbook(None, "unclassified attachment")
    assert playbook.challenge_type == "unknown"
    assert len(playbook.first_steps) == 3
    assert any("新增事实" in condition for condition in playbook.stop_conditions)
