"""FastAPI application: road-race split timing adjudication service."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from . import service
from .database import SessionLocal, init_db
from .schemas import (
    ChipReplaceIn,
    ChipTimelineOut,
    ClockOffsetIn,
    ClockOffsetListOut,
    ErrorBody,
    RaceCreate,
    RaceOut,
    RunnerResultOut,
    ScanBatchCreate,
    ScanBatchItem,
    ScanCreate,
    ScanOut,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Creates tables once the database is reachable (retries internally),
    # giving the one-command Compose boot its "database initialization".
    init_db()
    yield


app = FastAPI(
    title="Road Race Split Timing Adjudication Service",
    version="1.0.0",
    lifespan=lifespan,
    description=(
        "Race configuration, out-of-order chip scan ingestion with "
        "idempotent replay/conflict detection, per-point signed integer "
        "clock offsets, provisional results on calibrated times, scan "
        "revocation and sealing of a frozen result snapshot."
    ),
)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.exception_handler(service.ApiError)
async def api_error_handler(_: Request, exc: service.ApiError) -> JSONResponse:
    content = {"error": exc.error, "detail": exc.detail}
    content.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=content)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
def health() -> dict:
    return {"status": "ok"}


# --------------------------------------------------------------------------
# Races
# --------------------------------------------------------------------------


@app.post(
    "/races",
    response_model=RaceOut,
    status_code=201,
    tags=["races"],
    responses={409: {"model": ErrorBody}, 422: {"model": ErrorBody}},
)
def create_race(payload: RaceCreate, db: Session = Depends(get_db)) -> dict:
    race = service.create_race(db, payload)
    return service.serialize_race(race)


@app.get("/races/{race_id}", response_model=RaceOut, tags=["races"])
def get_race(race_id: str, db: Session = Depends(get_db)) -> dict:
    return service.serialize_race(service.get_race(db, race_id))


# --------------------------------------------------------------------------
# Scans
# --------------------------------------------------------------------------


@app.post(
    "/races/{race_id}/scans",
    response_model=ScanOut,
    tags=["scans"],
    responses={
        200: {"model": ScanOut, "description": "idempotent replay"},
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
    },
)
def submit_scan(
    race_id: str,
    payload: ScanCreate,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    scan, replayed = service.submit_scan(db, race_id, payload)
    response.status_code = 200 if replayed else 201
    return service.serialize_scan(scan, replayed=replayed)


@app.post(
    "/races/{race_id}/scans/batch",
    response_model=list[ScanBatchItem],
    tags=["scans"],
)
def submit_scan_batch(
    race_id: str,
    payload: ScanBatchCreate,
    db: Session = Depends(get_db),
) -> list[dict]:
    return service.submit_scan_batch(db, race_id, payload.scans)


@app.delete(
    "/races/{race_id}/scans/{scan_no}",
    response_model=ScanOut,
    tags=["scans"],
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
)
def revoke_scan(
    race_id: str, scan_no: str, db: Session = Depends(get_db)
) -> dict:
    scan = service.revoke_scan(db, race_id, scan_no)
    return service.serialize_scan(scan)


# --------------------------------------------------------------------------
# Clock offsets
# --------------------------------------------------------------------------


@app.get(
    "/races/{race_id}/offsets",
    response_model=ClockOffsetListOut,
    tags=["clock-offsets"],
    responses={404: {"model": ErrorBody}},
)
def list_clock_offsets(race_id: str, db: Session = Depends(get_db)) -> dict:
    return service.list_offsets(db, race_id)


@app.put(
    "/races/{race_id}/points/{point_code}/offset",
    response_model=ClockOffsetListOut,
    tags=["clock-offsets"],
    responses={
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
        422: {"model": ErrorBody},
    },
)
def put_clock_offset(
    race_id: str,
    point_code: str,
    payload: ClockOffsetIn,
    db: Session = Depends(get_db),
) -> dict:
    return service.set_point_offset(db, race_id, point_code, payload)


# --------------------------------------------------------------------------
# Mid-race chip replacement
# --------------------------------------------------------------------------


@app.post(
    "/races/{race_id}/chip-replacements",
    response_model=ChipTimelineOut,
    status_code=201,
    tags=["chip-replacement"],
    responses={
        404: {"model": ErrorBody},
        409: {"model": ErrorBody},
        422: {"model": ErrorBody},
    },
)
def register_chip_replacement(
    race_id: str,
    payload: ChipReplaceIn,
    db: Session = Depends(get_db),
) -> dict:
    return service.register_chip_replacement(db, race_id, payload)


@app.get(
    "/races/{race_id}/runners/{runner_id}/chip-timeline",
    response_model=ChipTimelineOut,
    tags=["chip-replacement"],
    responses={404: {"model": ErrorBody}},
)
def get_chip_timeline(
    race_id: str,
    runner_id: str,
    db: Session = Depends(get_db),
) -> dict:
    return service.chip_timeline(db, race_id, runner_id)


# --------------------------------------------------------------------------
# Results & sealing
# --------------------------------------------------------------------------

@app.get("/races/{race_id}/results", tags=["results"])
def race_results(race_id: str, db: Session = Depends(get_db)) -> dict:
    return service.live_results(db, race_id)


@app.get(
    "/races/{race_id}/runners/{runner_id}/result",
    response_model=RunnerResultOut,
    tags=["results"],
)
def runner_result(
    race_id: str, runner_id: str, db: Session = Depends(get_db)
) -> dict:
    return service.runner_result(db, race_id, runner_id)


@app.post("/races/{race_id}/seal", tags=["results"])
def seal_race(race_id: str, db: Session = Depends(get_db)) -> dict:
    return service.seal_race(db, race_id)
