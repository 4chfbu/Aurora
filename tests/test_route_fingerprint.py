from aurora.services.blackboard_repository import route_fingerprint


def test_route_fingerprint_ignores_transient_limits_and_whitespace() -> None:
    first = {"command": "curl   -fsS  http://target/", "timeout_seconds": 5, "max_output_bytes": 1000}
    second = {"command": "curl -fsS http://target/", "timeout_seconds": 30, "max_output_bytes": 9000}

    assert route_fingerprint(first) == route_fingerprint(second)
