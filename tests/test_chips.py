"""Standard-library-only checks for app/chips.py.

Run with either:
    python3 tests/test_chips.py
    pytest tests/test_chips.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.chips import (  # noqa: E402
    ChipReplacementEvent,
    active_chip_at,
    active_period_at,
    build_periods,
    in_race_window,
    replacement_conflicts,
)

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(minutes=15)
T2 = T0 + timedelta(minutes=30)
CUTOFF = T0 + timedelta(hours=1)


def ev(seq, old, new, at):
    return ChipReplacementEvent(
        sequence=seq, old_chip_code=old, new_chip_code=new, effective_at=at
    )


def check(name, got, want):
    if got != want:
        raise AssertionError(f"{name}:\n  got={got!r}\n want={want!r}")


def test_single_period_active_everywhere_from_start():
    periods = build_periods(
        initial_chip="C0", race_start=T0, replacements=[]
    )
    check("len", len(periods), 1)
    check("chip at start", active_chip_at(periods, T0), "C0")
    check("chip after cutoff", active_chip_at(periods, CUTOFF), "C0")
    # Before start resolves to the initial window (caller rejects separately).
    check("chip before start", active_chip_at(periods, T0 - timedelta(hours=2)),
          "C0")
    check("window open-ended", periods[0].valid_to, None)


def test_half_open_boundary_belongs_to_new_chip():
    periods = build_periods(
        initial_chip="C0",
        race_start=T0,
        replacements=[ev(1, "C0", "C1", T1)],
    )
    check("before boundary", active_chip_at(periods, T1 - timedelta(seconds=1)),
          "C0")
    check("exact boundary -> new", active_chip_at(periods, T1), "C1")
    check("after boundary", active_chip_at(periods, T1 + timedelta(seconds=1)),
          "C1")

    p0, p1 = periods
    check("p0 from", p0.valid_from, T0)
    check("p0 to (exclusive)", p0.valid_to, T1)
    check("p1 from", p1.valid_from, T1)
    check("p1 open", p1.valid_to, None)
    check("sequences", [p.sequence for p in periods], [0, 1])


def test_multiple_replacements_partition_line():
    periods = build_periods(
        initial_chip="C0",
        race_start=T0,
        replacements=[
            ev(2, "C1", "C2", T2),
            ev(1, "C0", "C1", T1),
        ],
    )
    check(
        "chips ordered",
        [p.chip_code for p in periods],
        ["C0", "C1", "C2"],
    )
    check("at T0", active_chip_at(periods, T0), "C0")
    check("at T1", active_chip_at(periods, T1), "C1")
    check("at T2", active_chip_at(periods, T2), "C2")
    check("late", active_chip_at(periods, CUTOFF), "C2")


def test_in_race_window_closed():
    check("start in", in_race_window(T0, T0, CUTOFF), True)
    check("cutoff in", in_race_window(CUTOFF, T0, CUTOFF), True)
    check("inside in", in_race_window(T1, T0, CUTOFF), True)
    check("before out", in_race_window(T0 - timedelta(seconds=1), T0, CUTOFF),
          False)
    check("after out", in_race_window(CUTOFF + timedelta(seconds=1), T0, CUTOFF),
          False)


def test_replacement_conflict_old_chip_scan_at_or_after_boundary():
    # Existing scan from old chip stamped at T1 exactly: after replacement
    # the boundary belongs to the new chip -> conflict.
    conflicts = replacement_conflicts(
        initial_chip="C0",
        race_start=T0,
        replacements=[],
        pending=ev(1, "C0", "C1", T1),
        scans=[("C0", T1)],
    )
    check("exact-instant conflict", len(conflicts), 1)

    # One second earlier: still in the old window -> fine.
    conflicts = replacement_conflicts(
        initial_chip="C0",
        race_start=T0,
        replacements=[],
        pending=ev(1, "C0", "C1", T1),
        scans=[("C0", T1 - timedelta(seconds=1))],
    )
    check("strictly earlier fine", conflicts, [])

    # Revoked scans are passed too and conflict the same way.
    conflicts = replacement_conflicts(
        initial_chip="C0",
        race_start=T0,
        replacements=[ev(1, "C0", "C1", T1)],
        pending=ev(2, "C1", "C2", T2),
        scans=[("C1", T1), ("C1", T2)],
    )
    check("second replacement conflicts", len(conflicts), 1)
    check("conflicting chip", conflicts[0][0], "C1")
    check("conflicting time", conflicts[0][1], T2)


def test_pre_start_scan_still_resolves_initial_window():
    # Used to surface a clear out-of-window rejection rather than an
    # attribution failure.
    periods = build_periods(
        initial_chip="C0", race_start=T0, replacements=[ev(1, "C0", "C1", T1)]
    )
    period = active_period_at(periods, T0 - timedelta(minutes=1))
    check("pre-start initial chip", period.chip_code, "C0")


def run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok {t.__name__}")
    print(f"\n{len(tests)} chip tests passed")


if __name__ == "__main__":
    run_all()
