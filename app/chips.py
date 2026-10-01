"""Pure chip-replacement timeline logic (no third-party dependencies).

A runner starts the race with one *initial chip* (the binding recorded at
race creation). Staff may register chip replacements while the race is
unsealed; each replacement introduces a new chip that is valid **from an
effective instant on**. The timeline therefore partitions race time into
half-open validity windows::

    initial chip:  [race_start,            t1)
    replacement 1: [t1,                   t2)
    replacement 2: [t2,               +inf  )

(an earlier-only window of the initial chip covers any instants before the
race start as well, so pre-start scans can still be resolved to the
initial chip and explicitly rejected as out-of-window — never silently
attributed to someone else).

Rules implemented here, shared by ingestion and registration:

* a scan is attributed by the chip observed by the device and the chip
  valid at the scan's *raw occurred_at*; out-of-order/backfilled scans use
  the exact same rule;
* a scan whose chip is not active for that runner at that instant is
  *outside the valid window* and must be rejected;
* registering a replacement that would move an already-received scan
  (revoked scans included) into an invalid window conflicts — the whole
  registration must be rejected and no existing data is rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence


def _normalise(value: datetime) -> datetime:
    # Stored timestamps are timezone-aware UTC (PostgreSQL); be defensive for
    # backends/tests that hand back a naive value by treating it as UTC.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ChipReplacementEvent:
    """One registered replacement, ordered by ``effective_at``."""

    sequence: int
    old_chip_code: str
    new_chip_code: str
    effective_at: datetime


@dataclass(frozen=True)
class ChipPeriod:
    """Half-open validity window of one chip for one runner.

    ``valid_from`` (inclusive) and ``valid_to`` (exclusive; ``None`` means
    open-ended, i.e. the chip currently attached to the runner).
    """

    chip_code: str
    valid_from: Optional[datetime]
    valid_to: Optional[datetime]
    sequence: int  # 0 for the initial chip, 1.. for replacement chips


def build_periods(
    *,
    initial_chip: str,
    race_start: datetime,
    replacements: Sequence[ChipReplacementEvent],
) -> tuple[ChipPeriod, ...]:
    """Build the ordered half-open chip windows for one runner.

    The initial chip is reported as valid from ``race_start``; its effective
    domain for matching also covers anything earlier.
    """

    ordered = sorted(
        replacements,
        key=lambda r: (_normalise(r.effective_at), r.sequence),
    )
    periods: list[ChipPeriod] = []
    current_chip = initial_chip
    current_from = _normalise(race_start)
    for index, event in enumerate(ordered):
        effective = _normalise(event.effective_at)
        periods.append(
            ChipPeriod(
                chip_code=current_chip,
                valid_from=current_from,
                valid_to=effective,
                sequence=index,
            )
        )
        current_chip = event.new_chip_code
        current_from = effective
    periods.append(
        ChipPeriod(
            chip_code=current_chip,
            valid_from=current_from,
            valid_to=None,
            sequence=len(ordered),
        )
    )
    return tuple(periods)


def active_period_at(
    periods: Sequence[ChipPeriod], occurred_at: datetime
) -> Optional[ChipPeriod]:
    """Return the chip window active at ``occurred_at``.

    Times before ``race_start`` resolve to the initial chip window (its
    valid_from is the race start) so the caller can reject them as
    outside the race window instead of misattributing them.
    """

    if not periods:
        return None
    initial = periods[0]
    occurred_at = _normalise(occurred_at)
    if occurred_at < initial.valid_from:
        return initial
    for period in periods:
        if period.valid_to is None:
            return period
        if period.valid_from <= occurred_at < period.valid_to:
            return period
    return periods[-1]


def active_chip_at(
    periods: Sequence[ChipPeriod], occurred_at: datetime
) -> Optional[str]:
    period = active_period_at(periods, occurred_at)
    return period.chip_code if period is not None else None


def in_race_window(
    occurred_at: datetime, race_start: datetime, cutoff: datetime
) -> bool:
    """The legal scan-time window is the closed start..cutoff interval."""

    return (
        _normalise(race_start)
        <= _normalise(occurred_at)
        <= _normalise(cutoff)
    )


def replacement_conflicts(
    *,
    initial_chip: str,
    race_start: datetime,
    replacements: Sequence[ChipReplacementEvent],
    pending: ChipReplacementEvent,
    scans: Iterable[tuple[str, datetime]],
) -> list[tuple[str, datetime]]:
    """Return received scans (``(chip, occurred_at)``) invalidated by pending.

    ``scans`` must include revoked scans too: a replacement must not leave a
    received scan (even a revoked one) outside its chip window.
    """

    candidate = build_periods(
        initial_chip=initial_chip,
        race_start=race_start,
        replacements=tuple(replacements) + (pending,),
    )
    conflicts: list[tuple[str, datetime]] = []
    for chip, occurred_at in scans:
        period = active_period_at(candidate, occurred_at)
        if period is None or period.chip_code != chip:
            conflicts.append((chip, occurred_at))
    return conflicts
