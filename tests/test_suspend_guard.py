from aurora.services.suspend_guard import SuspendGapDetector


def test_suspend_gap_detector_ignores_normal_clock_progress() -> None:
    wall = iter([100.0, 101.0, 102.0])
    monotonic = iter([10.0, 11.0, 12.0])
    detector = SuspendGapDetector(
        threshold_seconds=30,
        wall_clock=lambda: next(wall),
        monotonic_clock=lambda: next(monotonic),
    )

    assert detector.poll() is None
    assert detector.poll() is None


def test_suspend_gap_detector_reports_wall_clock_jump() -> None:
    wall = iter([100.0, 101.0, 221.0])
    monotonic = iter([10.0, 11.0, 12.0])
    detector = SuspendGapDetector(
        threshold_seconds=30,
        wall_clock=lambda: next(wall),
        monotonic_clock=lambda: next(monotonic),
    )

    assert detector.poll() is None
    assert detector.poll() == 119.0
