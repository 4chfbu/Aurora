from __future__ import annotations


TSECBENCH_PHASE_MINUTES = (12, 25, 40)
TSECBENCH_MAX_MINUTES_PER_CHALLENGE = sum(TSECBENCH_PHASE_MINUTES)

# Reserve the last minute of each phase for a structured checkpoint and
# continuation handoff. The hard deadline remains the authoritative wall-clock
# phase limit.
TSECBENCH_PHASE_BUDGETS: dict[int, tuple[int, int, int, int]] = {
    1: (11 * 60, 12 * 60, 0, 3),
    2: (24 * 60, 25 * 60, 0, 3),
    3: (39 * 60, 40 * 60, 0, 4),
}


def tsecbench_phase_minutes(phase: int) -> int:
    if 1 <= phase <= len(TSECBENCH_PHASE_MINUTES):
        return TSECBENCH_PHASE_MINUTES[phase - 1]
    return TSECBENCH_PHASE_MINUTES[-1]


def tsecbench_phase_budget(phase: int) -> tuple[int, int, int, int]:
    return TSECBENCH_PHASE_BUDGETS.get(phase, TSECBENCH_PHASE_BUDGETS[3])
