"""Business logic: races, scans, revocation, adjudication, sealing."""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .adjudication import Scan as DomainScan
from .adjudication import Segment as DomainSegment
from .adjudication import adjudicate_runner
from .chips import (
    ChipPeriod,
    ChipReplacementEvent,
    active_period_at,
    build_periods,
    in_race_window,
    replacement_conflicts,
)
from .database import (
    ChipReplacement,
    ClockOffset,
    Point,
    Race,
    ResultSnapshot,
    Runner,
    Scan,
    SegmentRow,
    utcnow,
)
from .schemas import ChipReplacementIn, ClockOffsetIn, RaceCreate, ScanCreate


class ApiError(Exception):
    """Error mapped to an HTTP response by the router."""

    def __init__(self, status_code: int, error: str, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.error = error
        self.detail = detail


def _json_safe(value: Any) -> Any:
    """Convert a result payload into JSONB-storable plain values."""

    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _as_utc(value: datetime) -> datetime:
    """Timestamps are stored tz-aware UTC; normalise any backend variation."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------


def _segment_out(s: SegmentRow) -> dict:
    return {
        "position": s.position,
        "from_point": s.from_point_code,
        "to_point": s.to_point_code,
        "min_seconds": float(s.min_seconds),
        "max_seconds": float(s.max_seconds),
    }


def serialize_race(race: Race) -> dict:
    return {
        "id": str(race.id),
        "name": race.name,
        "start_time": race.start_time,
        "cutoff_time": race.cutoff_time,
        "sealed": race.sealed,
        "created_at": race.created_at,
        "sealed_at": race.sealed_at,
        "offset_version": race.offset_version or 0,
        "points": [
            {
                "position": p.position,
                "code": p.code,
                "name": p.name,
                "is_finish": p.is_finish,
            }
            for p in sorted(race.points, key=lambda p: p.position)
        ],
        "segments": [
            _segment_out(s)
            for s in sorted(race.segments, key=lambda s: s.position)
        ],
        "runners": [
            {
                "id": str(r.id),
                "bib": r.bib,
                "name": r.name,
                "chip": r.chip_code,
            }
            for r in race.runners
        ],
    }


def serialize_scan(scan: Scan, *, replayed: bool = False) -> dict:
    return {
        "scan_no": scan.scan_no,
        # The chip the device literally observed; stored on the scan itself
        # so a later chip replacement never rewrites it.
        "chip": scan.chip_code,
        "point": scan.point.code,
        "occurred_at": scan.occurred_at,
        "revoked": scan.revoked,
        "replayed": replayed,
    }


def _valid_uuid(value: str, *, what: str = "identifier") -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise ApiError(404, "not_found", f"invalid {what}: {value!r}")


# --------------------------------------------------------------------------
# Race creation (configuration is fixed afterwards: no update endpoints)
# --------------------------------------------------------------------------


def create_race(db: Session, payload: RaceCreate) -> Race:
    # Extra defensive checks beyond pydantic (validation normally raises 422).
    if payload.cutoff_time <= payload.start_time:
        raise ApiError(422, "invalid_time_window",
                       "cutoff_time must be later than start_time")
    if any(s.min_seconds < 0 or s.max_seconds < 0
           for s in payload.segments):
        raise ApiError(422, "invalid_segment", "durations must be non-negative")
    if any(s.min_seconds > s.max_seconds for s in payload.segments):
        raise ApiError(422, "invalid_segment",
                       "min_seconds must be <= max_seconds")
    codes = [p.code for p in payload.points]
    if len(codes) != len(set(codes)):
        raise ApiError(422, "duplicate_point", "point codes must be unique")
    chips = [r.chip for r in payload.runners]
    if len(chips) != len(set(chips)):
        raise ApiError(422, "duplicate_chip", "chip bindings must be unique")

    race_id = str(uuid.uuid4())
    race = Race(
        id=race_id,
        name=payload.name,
        start_time=payload.start_time,
        cutoff_time=payload.cutoff_time,
        sealed=False,
    )
    db.add(race)

    points: list[Point] = []
    for i, p in enumerate(payload.points, start=1):
        point = Point(
            race_id=race_id,
            position=i,
            code=p.code,
            name=p.name,
            # Last ordered point is always the finish.
            is_finish=(i == len(payload.points)) or p.is_finish,
        )
        points.append(point)
        db.add(point)

    all_codes = ["START"] + codes
    for i, seg in enumerate(payload.segments, start=1):
        db.add(
            SegmentRow(
                race_id=race_id,
                position=i,
                from_point_code=all_codes[i - 1],
                to_point_code=all_codes[i],
                min_seconds=Decimal(str(seg.min_seconds)),
                max_seconds=Decimal(str(seg.max_seconds)),
            )
        )

    for r in payload.runners:
        db.add(
            Runner(
                id=str(uuid.uuid4()),
                race_id=race_id,
                bib=r.bib,
                name=r.name,
                chip_code=r.chip,
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "race_conflict", "race could not be created")

    db.refresh(race)
    return race


# --------------------------------------------------------------------------
# Loading helpers
# --------------------------------------------------------------------------


def get_race_locked(db: Session, race_id: str, *, fresh: bool = False) -> Race:
    """Race row with SELECT ... FOR UPDATE (serialises every mutating op).

    The lock serialises scan submit/revoke, offset changes, chip
    replacements and sealing with each other; point/segment/runner/offset
    collections are eager-loaded so adjudication and listings are consistent
    under the same lock. ``fresh=True`` forces a database re-read of those
    collections (used right after a commit within the same session, since
    expire_on_commit is disabled).
    """

    race_id = _valid_uuid(race_id, what="race_id")
    options = [
        selectinload(Race.points),
        selectinload(Race.segments),
        selectinload(Race.runners).selectinload(Runner.chip_replacements),
        selectinload(Race.clock_offsets),
        selectinload(Race.chip_replacements),
    ]
    stmt = select(Race).where(Race.id == race_id).with_for_update()
    if fresh:
        stmt = stmt.execution_options(populate_existing=True)
    race = db.scalars(stmt.options(*options)).first()
    if race is None:
        raise ApiError(404, "race_not_found", f"race {race_id} does not exist")
    return race


def get_race(db: Session, race_id: str) -> Race:
    race_id = _valid_uuid(race_id, what="race_id")
    race = db.get(
        Race,
        race_id,
        options=[
            selectinload(Race.points),
            selectinload(Race.segments),
            selectinload(Race.runners).selectinload(Runner.chip_replacements),
        ],
    )
    if race is None:
        raise ApiError(404, "race_not_found", f"race {race_id} does not exist")
    return race


def _get_runner_404(db: Session, race: Race, runner_id: str) -> Runner:
    runner_id = _valid_uuid(runner_id, what="runner_id")
    runner = db.scalars(
        select(Runner)
        .where(Runner.id == runner_id, Runner.race_id == race.id)
        .execution_options(populate_existing=True)
        .options(selectinload(Runner.chip_replacements))
    ).first()
    if runner is None:
        raise ApiError(
            404,
            "runner_not_found",
            f"runner {runner_id} does not exist in this race",
        )
    return runner


# --------------------------------------------------------------------------
# Scan ingestion
# --------------------------------------------------------------------------


def _runner_periods(
    race: Race, runner: Runner, events: Optional[list[ChipReplacement]] = None
) -> tuple[ChipPeriod, ...]:
    if events is None:
        events = sorted(
            runner.chip_replacements,
            key=lambda e: (_as_utc(e.effective_at), e.sequence),
        )
    initial_chip = events[0].old_chip_code if events else runner.chip_code
    return build_periods(
        initial_chip=initial_chip,
        race_start=race.start_time,
        replacements=[
            ChipReplacementEvent(
                sequence=e.sequence,
                old_chip_code=e.old_chip_code,
                new_chip_code=e.new_chip_code,
                effective_at=e.effective_at,
            )
            for e in events
        ],
    )


def _runner_current_chip(runner: Runner) -> str:
    if not runner.chip_replacements:
        return runner.chip_code
    latest = max(
        runner.chip_replacements,
        key=lambda e: (_as_utc(e.effective_at), e.sequence),
    )
    return latest.new_chip_code


def _chip_owner_map(db: Session, race: Race) -> dict[str, tuple[Runner, tuple[ChipPeriod, ...]]]:
    """chip_code -> (runner, validity periods) for every chip ever bound."""

    owners: dict[str, tuple[Runner, tuple[ChipPeriod, ...]]] = {}
    for runner in race.runners:
        periods = _runner_periods(race, runner)
        for period in periods:
            owners[period.chip_code] = (runner, periods)
    return owners


def _resolve_scan_refs(
    db: Session, race: Race, payload: ScanCreate
) -> tuple[Runner, Point]:
    """Resolve (chip, occurred_at) -> runner and point -> point.

    The chip is matched against the chip valid for *this runner* at the
    scan's raw device timestamp (same rule for late/out-of-order uploads):

      * a chip never bound to the race -> 404 unknown_chip;
      * a chip bound but outside the race window / not active for that
        runner at that instant -> 409, explicitly rejected;
      * an unknown point -> 404.
    """

    owners = _chip_owner_map(db, race)
    binding = owners.get(payload.chip)
    if binding is None:
        raise ApiError(404, "unknown_chip",
                       f"chip {payload.chip!r} is not bound to this race")
    runner, periods = binding

    occurred_at = _as_utc(payload.occurred_at)
    period = active_period_at(periods, occurred_at)
    if occurred_at < _as_utc(race.start_time):
        # Before the start no chip window has begun yet: reject explicitly
        # instead of silently attributing the observation.
        raise ApiError(
            409,
            "scan_before_race_start",
            f"scan occurred_at {occurred_at.isoformat()} is before the race "
            f"start {_as_utc(race.start_time).isoformat()}",
        )
    if period is None or period.chip_code != payload.chip:
        active = active_period_at(periods, occurred_at)
        active_chip = active.chip_code if active is not None else None
        raise ApiError(
            409,
            "chip_outside_valid_period",
            f"chip {payload.chip!r} is not valid for runner "
            f"{runner.bib!r} at {occurred_at.isoformat()}; the chip active "
            f"then is {active_chip!r}",
        )

    point = db.scalars(
        select(Point).where(
            Point.race_id == race.id, Point.code == payload.point
        )
    ).first()
    if point is None:
        raise ApiError(404, "unknown_point",
                       f"point {payload.point!r} does not exist in this race")
    return runner, point


def _existing_replay_or_conflict(
    existing: Scan, runner: Runner, point: Point, occurred_at: datetime,
    scan_no: str, chip: str,
) -> Optional[Scan]:
    """Return the existing scan for an idempotent replay, else raise.

    A revoked scan keeps its unique number: a byte-identical retransmission
    replays as revoked; any other content is a conflict (use a new scan_no
    for a genuinely new observation). The comparison includes the chip, so
    replaying an old scan under a replaced chip still conflicts rather than
    being silently rewritten.
    """

    same = (
        existing.runner_id == runner.id
        and existing.point_id == point.id
        and _as_utc(existing.occurred_at) == _as_utc(occurred_at)
        and existing.chip_code == chip
    )
    if not same:
        raise ApiError(
            409,
            "scan_conflict",
            f"scan_no {scan_no!r} already exists with different "
            "chip/point/occurred_at",
        )
    return existing


def submit_scan(
    db: Session, race_id: str, payload: ScanCreate
) -> tuple[Scan, bool]:
    """Insert one scan. Returns (scan, replayed).

    Same scan_no + same (chip, point, occurred_at) is an idempotent replay.
    Same scan_no with different content is a 409 conflict.
    """

    race = get_race_locked(db, race_id)
    if race.sealed:
        raise ApiError(409, "race_sealed",
                       "race is sealed; no scans are accepted")

    runner, point = _resolve_scan_refs(db, race, payload)

    existing = db.scalars(
        select(Scan).where(Scan.race_id == race.id, Scan.scan_no == payload.scan_no)
    ).first()
    if existing is not None:
        return _existing_replay_or_conflict(
            existing, runner, point, payload.occurred_at, payload.scan_no,
            payload.chip,
        ), True

    scan = Scan(
        race_id=race.id,
        scan_no=payload.scan_no,
        runner_id=runner.id,
        point_id=point.id,
        chip_code=payload.chip,
        occurred_at=payload.occurred_at,
        revoked=False,
    )
    db.add(scan)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "scan_conflict",
                       f"scan_no {payload.scan_no!r} already exists")
    db.refresh(scan)
    return scan, False


def submit_scan_batch(
    db: Session, race_id: str, payloads: Iterable[ScanCreate]
) -> list[dict]:
    """Validate/insert many scans in one transaction, one result per item."""

    results: list[dict] = []
    try:
        race = get_race_locked(db, race_id)
        if race.sealed:
            raise ApiError(409, "race_sealed",
                           "race is sealed; no scans are accepted")

        seen_in_batch: Counter[str] = Counter()
        for payload in payloads:
            seen_in_batch[payload.scan_no] += 1
            if seen_in_batch[payload.scan_no] > 1:
                results.append(
                    {
                        "status_code": 409,
                        "error": "scan_conflict",
                        "detail": f"scan_no {payload.scan_no!r} repeated "
                        "inside the same batch",
                        "scan": None,
                    }
                )
                continue
            try:
                runner, point = _resolve_scan_refs(db, race, payload)
            except ApiError as exc:
                results.append(
                    {
                        "status_code": exc.status_code,
                        "error": exc.error,
                        "detail": exc.detail,
                        "scan": None,
                    }
                )
                continue

            existing = db.scalars(
                select(Scan).where(
                    Scan.race_id == race.id, Scan.scan_no == payload.scan_no
                )
            ).first()
            if existing is not None:
                try:
                    replayed = _existing_replay_or_conflict(
                        existing, runner, point,
                        payload.occurred_at, payload.scan_no, payload.chip,
                    )
                except ApiError as exc:
                    results.append(
                        {
                            "status_code": exc.status_code,
                            "error": exc.error,
                            "detail": exc.detail,
                            "scan": None,
                        }
                    )
                    continue
                results.append(
                    {
                        "status_code": 200,
                        "error": None,
                        "detail": None,
                        "scan": serialize_scan(replayed, replayed=True),
                    }
                )
                continue

            scan = Scan(
                race_id=race.id,
                scan_no=payload.scan_no,
                runner_id=runner.id,
                point_id=point.id,
                chip_code=payload.chip,
                occurred_at=payload.occurred_at,
                revoked=False,
            )
            db.add(scan)
            try:
                db.flush()  # surface constraint errors within the loop
            except IntegrityError as exc:
                db.rollback()
                raise ApiError(409, "scan_conflict", str(exc.orig)) from exc
            results.append(
                {
                    "status_code": 201,
                    "error": None,
                    "detail": None,
                    "scan": serialize_scan(scan),
                }
            )

        db.commit()
    except ApiError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return results


# --------------------------------------------------------------------------
# Revocation
# --------------------------------------------------------------------------


def revoke_scan(db: Session, race_id: str, scan_no: str) -> Scan:
    race = get_race_locked(db, race_id)
    if race.sealed:
        raise ApiError(409, "race_sealed",
                       "race is sealed; revocation is rejected")

    scan = db.scalars(
        select(Scan).where(Scan.race_id == race.id, Scan.scan_no == scan_no)
    ).first()
    if scan is None:
        raise ApiError(404, "scan_not_found",
                       f"scan {scan_no!r} does not exist")
    if scan.revoked:
        raise ApiError(409, "scan_already_revoked",
                       f"scan {scan_no!r} is already revoked")

    scan.revoked = True
    db.commit()
    db.refresh(scan)
    return scan


# --------------------------------------------------------------------------
# Adjudication
# --------------------------------------------------------------------------


def _runner_results(
    db: Session,
    race: Race,
) -> list[dict]:
    points = sorted(race.points, key=lambda p: p.position)
    point_by_id = {p.id: p for p in points}
    position_by_id = {p.id: p.position for p in points}
    # Per-point signed integer-second clock offsets; an absent row is zero.
    offset_by_point_id = {o.point_id: int(o.offset_seconds) for o in race.clock_offsets}
    segments = [
        DomainSegment(
            position=s.position,
            min_seconds=float(s.min_seconds),
            max_seconds=float(s.max_seconds),
        )
        for s in sorted(race.segments, key=lambda s: s.position)
    ]

    active_scans = db.scalars(
        select(Scan)
        .where(Scan.race_id == race.id, Scan.revoked.is_(False))
        .order_by(Scan.occurred_at, Scan.scan_no)
    ).all()

    by_runner: dict[str, list[Scan]] = {}
    for scan in active_scans:
        by_runner.setdefault(scan.runner_id, []).append(scan)

    results: list[dict] = []
    for runner in sorted(race.runners, key=lambda r: r.bib):
        runner_scans = by_runner.get(runner.id, [])
        point_by_scan_no = {s.scan_no: point_by_id[s.point_id] for s in runner_scans}
        domain_scans = [
            DomainScan(
                scan_no=s.scan_no,
                point_position=position_by_id[s.point_id],
                occurred_at=s.occurred_at,
                # Raw occurred_at is preserved; the offset only shifts the
                # calibrated time used for adjudication.
                offset_seconds=offset_by_point_id.get(s.point_id, 0),
            )
            for s in runner_scans
        ]
        result = adjudicate_runner(
            race_start=race.start_time,
            cutoff=race.cutoff_time,
            segments=segments,
            scans=domain_scans,
        )
        used = [
            {
                "segment": u.segment_position,
                "point": point_by_scan_no[u.scan_no].code,
                "scan_no": u.scan_no,
                # Actual observed chip, taken straight from the scan row;
                # chip replacements never rewrite it.
                "chip": next(
                    s.chip_code for s in runner_scans if s.scan_no == u.scan_no
                ),
                "occurred_at": u.occurred_at,
                "calibrated_at": u.calibrated_at,
                "offset_seconds": u.offset_seconds,
                "segment_seconds": u.segment_seconds,
            }
            for u in result.used_scans
        ]
        results.append(
            {
                "runner_id": str(runner.id),
                "bib": runner.bib,
                "name": runner.name,
                "chip": runner.chip_code,
                "status": result.status,
                "finish_time": result.finish_time,
                "total_seconds": result.total_seconds,
                "used_scans": used,
            }
        )

    # Finished first by earliest finish, then lex-min scan-no sequence;
    # DNF afterwards by bib.
    used_map = {
        r["runner_id"]: [u["scan_no"] for u in r["used_scans"]]
        for r in results
    }
    finished = sorted(
        (r for r in results if r["status"] == "finished"),
        key=lambda r: (r["finish_time"], used_map[r["runner_id"]], r["bib"]),
    )
    dnf = sorted(
        (r for r in results if r["status"] != "finished"),
        key=lambda r: r["bib"],
    )
    ordered: list[dict] = []
    for rank, r in enumerate(finished, start=1):
        r["rank"] = rank
        ordered.append(r)
    for r in dnf:
        r["rank"] = None
        ordered.append(r)
    return ordered


def _live_payload(db: Session, race: Race) -> dict:
    # The race row is taken FOR UPDATE by the caller, serialising this read
    # against scan submit/revoke and offset changes, so provisional results
    # always reflect one committed scans + offset version.
    return {
        "race_id": str(race.id),
        "name": race.name,
        "sealed": False,
        "sealed_at": None,
        "offset_version": race.offset_version or 0,
        "results": _runner_results(db, race),
    }


def live_results(db: Session, race_id: str) -> dict:
    race = get_race_locked(db, race_id)
    try:
        if race.sealed:
            return sealed_results_payload(db, race)
        return _live_payload(db, race)
    finally:
        db.commit()  # release the FOR UPDATE row lock immediately


def runner_result(db: Session, race_id: str, runner_id: str) -> dict:
    runner_id = _valid_uuid(runner_id, what="runner_id")
    race = get_race_locked(db, race_id)
    try:
        if race.sealed:
            payload = sealed_results_payload(db, race)
        else:
            payload = _live_payload(db, race)
    finally:
        db.commit()  # release the FOR UPDATE row lock immediately
    for item in payload["results"]:
        if item["runner_id"] == runner_id:
            return item
    raise ApiError(404, "runner_not_found",
                   f"runner {runner_id} does not exist in this race")


# --------------------------------------------------------------------------
# Sealing
# --------------------------------------------------------------------------


def seal_race(db: Session, race_id: str) -> dict:
    """Freeze one consistent snapshot; idempotent for an already-sealed race."""

    race = get_race_locked(db, race_id)
    if race.sealed:
        return sealed_results_payload(db, race)

    results = _runner_results(db, race)
    sealed_at = utcnow()
    race.sealed = True
    race.sealed_at = sealed_at
    for item in results:
        db.add(
            ResultSnapshot(
                race_id=race.id,
                runner_id=item["runner_id"],
                payload=_json_safe(item),
            )
        )
    db.commit()
    return {
        "race_id": str(race.id),
        "name": race.name,
        "sealed": True,
        "sealed_at": sealed_at,
        "offset_version": race.offset_version or 0,
        "results": _json_safe(results),
    }


def sealed_results_payload(db: Session, race: Race) -> dict:
    rows = db.scalars(
        select(ResultSnapshot)
        .where(ResultSnapshot.race_id == race.id)
        .order_by(ResultSnapshot.id)
    ).all()
    payloads = [row.payload for row in rows]
    return {
        "race_id": str(race.id),
        "name": race.name,
        "sealed": True,
        "sealed_at": race.sealed_at,
        # Frozen alongside the snapshots; never changes after sealing.
        "offset_version": race.offset_version or 0,
        "results": payloads,
    }


# --------------------------------------------------------------------------
# Mid-race chip replacement
# --------------------------------------------------------------------------


def _timeline_payload(db: Session, race: Race, runner: Runner) -> dict:
    """Runner chip timeline: ordered half-open periods + replacement events."""

    periods = _runner_periods(race, runner)
    events = sorted(
        db.scalars(
            select(ChipReplacement).where(
                ChipReplacement.race_id == race.id,
                ChipReplacement.runner_id == runner.id,
            )
        ).all(),
        key=lambda e: e.sequence,
    )
    current_chip = periods[-1].chip_code
    return {
        "race_id": str(race.id),
        "runner_id": str(runner.id),
        "bib": runner.bib,
        "name": runner.name,
        "initial_chip": periods[0].chip_code,
        "current_chip": current_chip,
        "periods": [
            {
                "sequence": p.sequence,
                "chip": p.chip_code,
                "valid_from": p.valid_from,
                "valid_to": p.valid_to,
                "is_current": p.chip_code == current_chip,
            }
            for p in periods
        ],
        "replacements": [
            {
                "sequence": e.sequence,
                "old_chip": e.old_chip_code,
                "new_chip": e.new_chip_code,
                "effective_at": e.effective_at,
                "registered_at": e.registered_at,
                "note": getattr(e, "note", None),
            }
            for e in events
        ],
    }


def chip_timeline(db: Session, race_id: str, runner_id: str) -> dict:
    race = get_race_locked(db, race_id)
    try:
        runner = _get_runner_404(db, race, runner_id)
        return _timeline_payload(db, race, runner)
    finally:
        db.commit()  # release the FOR UPDATE row lock immediately


def register_chip_replacement(
    db: Session, race_id: str, runner_id: str, payload: ChipReplacementIn
) -> dict:
    """Register one mid-race chip replacement (race-lock serialised).

    The operation is all-or-nothing: sealed race, illegal effective time,
    reused chip or a conflict with already-received scans all reject the
    whole request; existing scan chips, scan numbers and ownership are
    never rewritten. On success the provisional results simply pick up the
    new binding on the next (on-demand) recompute.
    """

    race = get_race_locked(db, race_id)
    if race.sealed:
        raise ApiError(409, "race_sealed",
                       "race is sealed; chips cannot be replaced")
    runner = _get_runner_404(db, race, runner_id)

    effective_at = _as_utc(payload.effective_at)
    # Closed start..cutoff window; strictly later than the previous
    # replacement effective time (the first one only has to be >= start).
    if not in_race_window(effective_at, race.start_time, race.cutoff_time):
        raise ApiError(
            422,
            "invalid_effective_time",
            f"effective_at {effective_at.isoformat()} must fall inside the "
            f"race window [{_as_utc(race.start_time).isoformat()}, "
            f"{_as_utc(race.cutoff_time).isoformat()}]",
        )

    prior_events = sorted(
        runner.chip_replacements,
        key=lambda e: (_as_utc(e.effective_at), e.sequence),
    )
    if prior_events and effective_at <= _as_utc(prior_events[-1].effective_at):
        raise ApiError(
            422,
            "invalid_effective_time",
            "effective_at must be strictly later than the previous chip "
            f"replacement effective time "
            f"{_as_utc(prior_events[-1].effective_at).isoformat()}",
        )

    old_chip = _runner_current_chip(runner)
    if payload.new_chip == old_chip:
        raise ApiError(
            409,
            "chip_reused",
            f"new chip {payload.new_chip!r} is the chip already active for "
            "this runner",
        )

    # The new chip must never have been used in this race: no other runner's
    # binding (initial or after replacement) and never introduced before as
    # a replacement chip.
    used_chips: set[str] = set()
    for other in race.runners:
        other_periods = _runner_periods(race, other)
        used_chips.update(p.chip_code for p in other_periods)
    used_chips.update(e.new_chip_code for e in race.chip_replacements)
    if payload.new_chip in used_chips:
        raise ApiError(
            409,
            "chip_reused",
            f"chip {payload.new_chip!r} is already used in this race",
        )

    sequence = (max((e.sequence for e in runner.chip_replacements), default=0)
                + 1)
    pending = ChipReplacementEvent(
        sequence=sequence,
        old_chip_code=old_chip,
        new_chip_code=payload.new_chip,
        effective_at=effective_at,
    )

    # Every scan ever received for the runner (revoked scans included) must
    # remain inside a valid chip window after the replacement. The scan's
    # own chip_code is the truth, so nothing is rewritten here.
    runner_scans = db.scalars(
        select(Scan).where(Scan.race_id == race.id, Scan.runner_id == runner.id)
    ).all()
    conflicts = replacement_conflicts(
        initial_chip=_runner_periods(race, runner)[0].chip_code,
        race_start=race.start_time,
        replacements=[
            ChipReplacementEvent(
                sequence=e.sequence,
                old_chip_code=e.old_chip_code,
                new_chip_code=e.new_chip_code,
                effective_at=e.effective_at,
            )
            for e in prior_events
        ],
        pending=pending,
        scans=[(s.chip_code, s.occurred_at) for s in runner_scans],
    )
    if conflicts:
        detail = "; ".join(
            f"scan chip={chip!r} at {_as_utc(ts).isoformat()} would fall "
            "outside its valid period"
            for chip, ts in sorted(
                conflicts, key=lambda c: (_as_utc(c[1]), c[0])
            )[:5]
        )
        raise ApiError(
            409,
            "chip_replacement_conflict",
            f"replacement rejected: {len(conflicts)} already-received "
            f"scan(s) would become invalid ({detail})",
        )

    event_row = ChipReplacement(
        race_id=race.id,
        runner_id=runner.id,
        sequence=sequence,
        old_chip_code=old_chip,
        new_chip_code=payload.new_chip,
        effective_at=effective_at,
        registered_at=utcnow(),
        note=payload.note,
    )
    db.add(event_row)
    # The runner's *current* binding moves to the new chip. Initial chip and
    # every historical scan keep their original chip codes.
    runner.chip_code = payload.new_chip
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "chip_reused",
                       f"chip {payload.new_chip!r} is already used")

    # Reload under a fresh lock for the timeline response.
    race = get_race_locked(db, race_id, fresh=True)
    runner = _get_runner_404(db, race, runner_id)
    body = _timeline_payload(db, race, runner)
    db.commit()  # release the FOR UPDATE row lock
    body["registered"] = True
    return body


# --------------------------------------------------------------------------
# Per-point clock offsets
# --------------------------------------------------------------------------


def _find_point(race: Race, point_code: str) -> Point:
    for point in race.points:
        if point.code == point_code:
            return point
    raise ApiError(
        404,
        "unknown_point",
        f"point {point_code!r} does not exist in this race",
    )


def _offset_payload(race: Race, *, only: Optional[str] = None) -> dict:
    """All current per-point offsets (unset -> 0), in point order."""

    offset_by_point_id = {o.point_id: o for o in race.clock_offsets}
    items: list[dict] = []
    for point in sorted(race.points, key=lambda p: p.position):
        if only is not None and point.code != only:
            continue
        row = offset_by_point_id.get(point.id)
        if row is None:
            items.append(
                {
                    "point": point.code,
                    "offset_seconds": 0,
                    "configured": False,
                    "updated_at": None,
                }
            )
        else:
            items.append(
                {
                    "point": point.code,
                    "offset_seconds": int(row.offset_seconds),
                    "configured": True,
                    "updated_at": row.updated_at,
                }
            )
    return {
        "race_id": str(race.id),
        "offset_version": race.offset_version or 0,
        "offsets": items,
    }


def list_offsets(db: Session, race_id: str) -> dict:
    # Read under the same race lock as the mutations so the listing observes
    # one committed offset version (the lock itself is read-only here).
    race = get_race_locked(db, race_id)
    try:
        return _offset_payload(race)
    finally:
        db.commit()  # release the FOR UPDATE row lock immediately


def set_point_offset(
    db: Session, race_id: str, point_code: str, payload: ClockOffsetIn
) -> dict:
    """Set/replace one point's signed integer-second clock offset.

    Serialised via the race row lock with scan submission, revocation and
    sealing; a new offset version bumps the provisional results immediately.
    """

    race = get_race_locked(db, race_id)
    if race.sealed:
        raise ApiError(409, "race_sealed",
                       "race is sealed; clock offsets cannot be changed")
    point = _find_point(race, point_code)

    existing = next(
        (o for o in race.clock_offsets if o.point_id == point.id), None
    )
    now = utcnow()
    replaced = existing is not None
    if existing is None:
        db.add(
            ClockOffset(
                race_id=race.id,
                point_id=point.id,
                offset_seconds=payload.offset_seconds,
                updated_at=now,
            )
        )
    else:
        existing.offset_seconds = payload.offset_seconds
        existing.updated_at = now
    # New committed scans + offset version; provisional results are recomputed
    # by the next results query (there is nothing stored to invalidate).
    race.offset_version = (race.offset_version or 0) + 1
    db.commit()

    # Reload under a fresh lock so the response reflects the committed row
    # (the cached collection is not refreshed with expire_on_commit=False).
    race = get_race_locked(db, race_id, fresh=True)
    body = _offset_payload(race, only=point.code)
    item = body["offsets"][0]
    item["replaced"] = replaced
    db.commit()  # release the FOR UPDATE row lock
    return body
