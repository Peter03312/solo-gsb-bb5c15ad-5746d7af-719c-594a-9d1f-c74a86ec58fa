"""Pure split-timing adjudication logic.

This module deliberately has no third-party dependency so it can be unit
tested with a bare Python interpreter. The FastAPI layer loads rows from
PostgreSQL and feeds them to :func:`adjudicate_runner`.

Model
-----
* A race is created with a start instant, a cutoff instant, an ordered list
  of points (checkpoints + finish) and one closed interval
  ``[min, max]`` of non-negative duration for every segment:
  start -> point#1, point#1 -> point#2, ..., point#(n-1) -> point#n(finish).
* A runner may have zero or more scans at each point. Scans may arrive out
  of order. The adjudicator chooses, per runner, a path of exactly one scan
  per point in point order.
* Point timing devices may be off: every point carries a signed integer
  ``offset_seconds`` (absent means zero). Each scan keeps its original
  ``occurred_at``; all adjudication compares the *calibrated* time
  ``occurred_at + offset``.
* A path is *valid* when:
  - used scan calibrated instants are strictly increasing from one scan to
    the next (the calibrated time of scan i is strictly greater than that of
    scan i-1);
  - every segment duration (measured from the race start for the first
    segment, from the previous used scan afterwards, all in calibrated
    time) lies inside the configured closed interval — a first segment
    with lower bound 0 may therefore be stamped exactly at the start
    instant;
  - the calibrated finish scan is not later than the race cutoff.
* Among valid paths the one with the earliest calibrated finish instant
  wins; ties are broken by the lexicographically smallest tuple of scan
  numbers.
* Without any valid complete path the runner is "did_not_finish".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from datetime import datetime, timedelta


@dataclass(frozen=True)
class Segment:
    """Closed non-negative duration interval [min_seconds, max_seconds]."""

    position: int
    min_seconds: float
    max_seconds: float

    def contains(self, seconds: float) -> bool:
        return self.min_seconds <= seconds <= self.max_seconds


@dataclass(frozen=True)
class ChipPeriod:
    """Half-open chip validity window ``[effective_from, effective_to)``.

    The runner's originally bound chip is ``sequence == 0`` and has
    ``effective_from is None``: it is valid at every instant before the
    first replacement takes effect (including scans stamped before the race
    start). The latest (current) chip has ``effective_to is None`` and stays
    valid indefinitely. At the replacement instant itself the *new* chip is
    already valid and the old one is not (half-open boundary).
    """

    sequence: int  # 0 = original binding, 1.. = nth replacement
    chip: str
    effective_from: Optional[datetime]
    effective_to: Optional[datetime]
    is_initial: bool

    def contains(self, instant: datetime) -> bool:
        instant = _normalise(instant)
        if self.effective_from is not None and instant < _normalise(self.effective_from):
            return False
        if self.effective_to is not None and instant >= _normalise(self.effective_to):
            return False
        return True


def build_chip_periods(
    initial_chip: str,
    replacements: Sequence[tuple[int, str, datetime]] = (),
) -> tuple[ChipPeriod, ...]:
    """Build a runner's ordered chip windows.

    ``replacements`` is an ordered sequence of
    ``(sequence, chip, effective_at)`` (sequence 1-based). The original chip
    owns the window up to the first replacement; every replacement owns the
    window from its own effective instant up to the next replacement.
    """

    ordered = sorted(replacements, key=lambda r: r[0])
    # Boundary n introduces its chip at its effective instant; the original
    # chip has no left boundary.
    boundaries: list[tuple[int, str, Optional[datetime]]] = [
        (0, initial_chip, None)
    ]
    for sequence, chip, effective_at in ordered:
        boundaries.append((sequence, chip, _normalise(effective_at)))

    periods: list[ChipPeriod] = []
    for i, (sequence, chip, effective_from) in enumerate(boundaries):
        effective_to = (
            boundaries[i + 1][2] if i + 1 < len(boundaries) else None
        )
        periods.append(
            ChipPeriod(
                sequence=sequence,
                chip=chip,
                effective_from=effective_from,
                effective_to=effective_to,
                is_initial=(i == 0),
            )
        )
    return tuple(periods)


def active_period_at(
    periods: Sequence[ChipPeriod], instant: datetime
) -> Optional[ChipPeriod]:
    """Return the chip window valid at ``instant`` (raw device time).

    Windows are half-open and contiguous, so at most one matches; ``None``
    means the instant is outside every chip window of this runner.
    """

    for period in periods:
        if period.contains(instant):
            return period
    return None


@dataclass(frozen=True)
class Scan:
    """One chip observation at a point.

    ``point_position`` is the 1-based position of the point (1 = first
    checkpoint, n = finish). ``occurred_at`` is the raw device timestamp;
    ``offset_seconds`` is that point's signed integer clock offset (0 when
    unset). The calibrated instant is
    ``occurred_at + timedelta(seconds=offset_seconds)``.
    """

    scan_no: str
    point_position: int
    occurred_at: datetime
    offset_seconds: int = 0

    @property
    def calibrated_at(self) -> datetime:
        return _normalise(self.occurred_at) + timedelta(
            seconds=self.offset_seconds
        )


@dataclass(frozen=True)
class UsedScan:
    segment_position: int
    point_position: int
    scan_no: str
    occurred_at: datetime  # raw device timestamp, preserved as submitted
    calibrated_at: datetime  # occurred_at shifted by the point's offset
    offset_seconds: int = 0
    segment_seconds: float = 0.0


@dataclass(frozen=True)
class Result:
    status: str  # "finished" | "did_not_finish"
    finish_time: Optional[datetime]
    total_seconds: Optional[float]
    used_scans: tuple[UsedScan, ...]
    reason: Optional[str] = None


def _normalise(value: datetime) -> datetime:
    # All timestamps are stored timezone-aware (UTC) by the service, but be
    # defensive in this pure module.
    if value.tzinfo is None:
        from datetime import timezone

        return value.replace(tzinfo=timezone.utc)
    return value


def _valid_edge(
    occurred_at: datetime,
    previous_time: datetime,
    segment: Segment,
    *,
    from_start: bool,
) -> Optional[float]:
    """Return the segment duration in seconds when the edge is valid.

    The start -> first-point edge is governed solely by its closed interval
    (so ``min=0`` allows a scan stamped exactly at the start instant). Every
    later edge joins two scans and must additionally satisfy the strict
    increase of occurrence times.
    """

    elapsed = (occurred_at - previous_time).total_seconds()
    if not from_start and elapsed <= 0:
        return None
    if from_start and elapsed < 0:
        return None
    if not segment.contains(elapsed):
        return None
    return elapsed


def adjudicate_runner(
    *,
    race_start: datetime,
    cutoff: datetime,
    segments: Sequence[Segment],
    scans: Iterable[Scan],
) -> Result:
    """Choose the best valid complete path for one runner.

    Dynamic programming over points. ``states[p]`` maps a scan number at
    point p to ``(calibrated_at, scan_no tuple, used scan tuple)`` for the
    best partial path ending there; for an identical calibrated arrival time
    the lex-min scan-number sequence dominates.
    """

    race_start = _normalise(race_start)
    cutoff = _normalise(cutoff)

    if not segments:
        return Result(
            status="did_not_finish",
            finish_time=None,
            total_seconds=None,
            used_scans=(),
            reason="no_segments",
        )

    point_count = len(segments)

    # Group candidate scans by point; sort deterministically by calibrated
    # time then scan number so equal-time ties resolve identically
    # everywhere (all adjacency/interval/cutoff checks use calibrated time).
    layers: dict[int, list[Scan]] = {p: [] for p in range(1, point_count + 1)}
    for scan in scans:
        if 1 <= scan.point_position <= point_count:
            layers[scan.point_position].append(scan)
    for layer in layers.values():
        layer.sort(key=lambda s: (s.calibrated_at, s.scan_no))

    # Layer 0 is the virtual start: fixed time, empty scan sequence.
    # state: scan_no ->
    #   (calibrated_at, sequence tuple, tuple[UsedScan])
    states: dict[str, tuple[datetime, tuple[str, ...], tuple[UsedScan, ...]]] = {
        "__start__": (race_start, (), ())
    }

    for position in range(1, point_count + 1):
        segment = segments[position - 1]
        next_states: dict[
            str, tuple[datetime, tuple[str, ...], tuple[UsedScan, ...]]
        ] = {}

        for scan in layers[position]:
            happened = scan.calibrated_at
            if position == point_count and happened > cutoff:
                # A finish scan after cutoff can never belong to a valid
                # path; skip it early.
                continue

            best: Optional[
                tuple[datetime, tuple[str, ...], tuple[UsedScan, ...]]
            ] = None

            for prev_time, prev_seq, prev_used in states.values():
                elapsed = _valid_edge(
                    happened,
                    prev_time,
                    segment,
                    from_start=position == 1,
                )
                if elapsed is None:
                    continue
                candidate = (
                    happened,
                    prev_seq + (scan.scan_no,),
                    prev_used
                    + (
                        UsedScan(
                            segment_position=position,
                            point_position=position,
                            scan_no=scan.scan_no,
                            occurred_at=_normalise(scan.occurred_at),
                            calibrated_at=happened,
                            offset_seconds=scan.offset_seconds,
                            segment_seconds=elapsed,
                        ),
                    ),
                )
                # For equal arrival time keep the lex-min scan sequence.
                if best is None or (
                    candidate[1] < best[1]
                ):
                    best = candidate

            if best is not None:
                current = next_states.get(scan.scan_no)
                # Only one item per scan_no in the layer, but keep the guard.
                if current is None or best[1] < current[1]:
                    next_states[scan.scan_no] = best

        states = next_states
        if not states:
            return Result(
                status="did_not_finish",
                finish_time=None,
                total_seconds=None,
                used_scans=(),
                reason=f"no_valid_path_to_point_{position}",
            )

    # Finish layer: earliest finish time, then lex-min scan-number sequence.
    winner: Optional[
        tuple[datetime, tuple[str, ...], tuple[UsedScan, ...]]
    ] = None
    for finish_time, seq, used in states.values():
        key = (finish_time, seq)
        if winner is None or key < (winner[0], winner[1]):
            winner = (finish_time, seq, used)

    assert winner is not None  # states non-empty, so a winner exists
    finish_time, _seq, used = winner
    if finish_time > cutoff:
        # Defensive: finish scans above the cutoff were already filtered.
        return Result(
            status="did_not_finish",
            finish_time=None,
            total_seconds=None,
            used_scans=(),
            reason="after_cutoff",
        )

    return Result(
        status="finished",
        finish_time=finish_time,
        total_seconds=(finish_time - race_start).total_seconds(),
        used_scans=used,
    )
