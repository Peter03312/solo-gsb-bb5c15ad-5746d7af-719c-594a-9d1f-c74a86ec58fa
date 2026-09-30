"""Pydantic v2 request/response models."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _as_utc(value: datetime) -> datetime:
    """Normalise to timezone-aware UTC so comparisons never mix aware/naive."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


class PointIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    name: str = Field("", max_length=255)
    is_finish: bool = False

    @field_validator("code")
    @classmethod
    def _code_not_start(cls, v: str) -> str:
        if v.strip().upper() == "START":
            raise ValueError("'START' is reserved for the virtual start line")
        return v.strip()

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return v.strip()


class SegmentIn(BaseModel):
    min_seconds: float = Field(..., ge=0)
    max_seconds: float = Field(..., ge=0)

    @model_validator(mode="after")
    def _ordered(self):
        if self.min_seconds > self.max_seconds:
            raise ValueError("min_seconds must be <= max_seconds")
        return self


class RunnerIn(BaseModel):
    bib: str = Field(..., min_length=1, max_length=64)
    name: str = Field("", max_length=255)
    chip: str = Field(..., min_length=1, max_length=128)


class RaceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    start_time: datetime
    cutoff_time: datetime
    points: list[PointIn] = Field(..., min_length=1)
    segments: list[SegmentIn] = Field(..., min_length=1)
    runners: list[RunnerIn] = Field(..., min_length=1)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("start_time", "cutoff_time")
    @classmethod
    def _times_utc(cls, v: datetime) -> datetime:
        return _as_utc(v)

    @model_validator(mode="after")
    def _validate(self):
        if self.cutoff_time <= self.start_time:
            raise ValueError("cutoff_time must be later than start_time")

        codes = [p.code for p in self.points]
        if len(codes) != len(set(codes)):
            raise ValueError("duplicate point code")

        # One segment per ordered pair: start -> p1, p1 -> p2, ..., pn-1 -> pn
        if len(self.segments) != len(self.points):
            raise ValueError(
                f"expected {len(self.points)} segments (one per point from "
                f"start), got {len(self.segments)}"
            )

        bibs = [r.bib for r in self.runners]
        if len(bibs) != len(set(bibs)):
            raise ValueError("duplicate runner bib")
        chips = [r.chip for r in self.runners]
        if len(chips) != len(set(chips)):
            raise ValueError("duplicate chip binding")
        return self


class ScanCreate(BaseModel):
    scan_no: str = Field(..., min_length=1, max_length=64)
    chip: str = Field(..., min_length=1, max_length=128)
    point: str = Field(..., min_length=1, max_length=64)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _time_utc(cls, v: datetime) -> datetime:
        return _as_utc(v)


class ScanBatchCreate(BaseModel):
    scans: list[ScanCreate] = Field(..., min_length=1)


class ClockOffsetIn(BaseModel):
    # Strict int: 1.5 / 1.0 / "2" / true are all rejected (422), so a
    # non-integer offset can never be stored.
    offset_seconds: int = Field(strict=True)


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


class PointOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    position: int
    code: str
    name: str
    is_finish: bool


class SegmentOut(BaseModel):
    position: int
    from_point: str
    to_point: str
    min_seconds: float
    max_seconds: float


class RunnerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    bib: str
    name: str
    chip: str


class RaceOut(BaseModel):
    id: str
    name: str
    start_time: datetime
    cutoff_time: datetime
    sealed: bool
    created_at: datetime
    sealed_at: Optional[datetime]
    offset_version: int
    points: list[PointOut]
    segments: list[SegmentOut]
    runners: list[RunnerOut]


class UsedScanOut(BaseModel):
    segment: int
    point: str
    scan_no: str
    occurred_at: datetime  # raw device timestamp as submitted
    calibrated_at: datetime  # occurred_at + point clock offset
    offset_seconds: int
    segment_seconds: float


class RunnerResultOut(BaseModel):
    runner_id: str
    bib: str
    name: str
    chip: str
    status: Literal["finished", "did_not_finish"]
    finish_time: Optional[datetime]
    total_seconds: Optional[float]
    rank: Optional[int]
    used_scans: list[UsedScanOut]


class ScanOut(BaseModel):
    scan_no: str
    chip: str
    point: str
    occurred_at: datetime
    revoked: bool
    replayed: bool = False


class ScanBatchItem(BaseModel):
    status_code: int
    error: Optional[str] = None
    detail: Optional[str] = None
    scan: Optional[ScanOut] = None


class ClockOffsetOut(BaseModel):
    point: str
    offset_seconds: int  # 0 when never set
    configured: bool
    updated_at: Optional[datetime] = None
    replaced: bool = False


class ClockOffsetListOut(BaseModel):
    race_id: str
    offset_version: int
    offsets: list[ClockOffsetOut]


class ErrorBody(BaseModel):
    error: str
    detail: str
