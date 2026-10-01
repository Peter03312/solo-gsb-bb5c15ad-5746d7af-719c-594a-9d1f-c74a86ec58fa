"""SQLAlchemy engine/session setup and declarative models."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,

)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://racer:racer@db:5432/race_timing",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Race(Base):
    __tablename__ = "races"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cutoff_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    sealed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    sealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Bumped on every accepted per-point clock-offset change; provisional
    # results are recomputed against the same committed scans/offset version.
    offset_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    points: Mapped[list["Point"]] = relationship(
        back_populates="race",
        cascade="all, delete-orphan",
        order_by="Point.position",
    )
    segments: Mapped[list["SegmentRow"]] = relationship(
        back_populates="race",
        cascade="all, delete-orphan",
        order_by="SegmentRow.position",
    )
    runners: Mapped[list["Runner"]] = relationship(
        back_populates="race", cascade="all, delete-orphan"
    )
    scans: Mapped[list["Scan"]] = relationship(
        back_populates="race", cascade="all, delete-orphan"
    )
    snapshots: Mapped[list["ResultSnapshot"]] = relationship(
        back_populates="race", cascade="all, delete-orphan"
    )
    clock_offsets: Mapped[list["ClockOffset"]] = relationship(
        back_populates="race", cascade="all, delete-orphan"
    )
    chip_replacements: Mapped[list["ChipReplacement"]] = relationship(
        back_populates="race",
        cascade="all, delete-orphan",
        order_by="ChipReplacement.sequence",
    )


class Point(Base):
    __tablename__ = "race_points"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("races.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    is_finish: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    race: Mapped[Race] = relationship(back_populates="points")

    __table_args__ = (
        UniqueConstraint("race_id", "position", name="uq_point_race_position"),
        UniqueConstraint("race_id", "code", name="uq_point_race_code"),
    )


class ClockOffset(Base):
    """Signed integer-second clock offset for one (race, point) device.

    Only present once staff has set an offset; an absent row means zero.
    Frozen at sealing together with the result snapshots.
    """

    __tablename__ = "clock_offsets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("races.id", ondelete="CASCADE"), nullable=False
    )
    point_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("race_points.id", ondelete="CASCADE"),
        nullable=False,
    )
    offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    race: Mapped[Race] = relationship(back_populates="clock_offsets")
    point: Mapped[Point] = relationship()

    __table_args__ = (
        UniqueConstraint("race_id", "point_id", name="uq_offset_race_point"),
    )


class SegmentRow(Base):
    __tablename__ = "segments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("races.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(nullable=False)  # 1-based
    from_point_code: Mapped[str] = mapped_column(String(64), nullable=False)
    to_point_code: Mapped[str] = mapped_column(String(64), nullable=False)
    # Closed, non-negative duration interval in seconds.
    min_seconds: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)
    max_seconds: Mapped[float] = mapped_column(Numeric(12, 3), nullable=False)

    race: Mapped[Race] = relationship(back_populates="segments")

    __table_args__ = (
        UniqueConstraint("race_id", "position", name="uq_segment_race_position"),
    )


class Runner(Base):
    __tablename__ = "runners"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    race_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("races.id", ondelete="CASCADE"), nullable=False
    )
    bib: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    chip_code: Mapped[str] = mapped_column(String(128), nullable=False)

    race: Mapped[Race] = relationship(back_populates="runners")
    scans: Mapped[list["Scan"]] = relationship(
        back_populates="runner", cascade="all, delete-orphan"
    )
    chip_replacements: Mapped[list["ChipReplacement"]] = relationship(
        back_populates="runner", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("race_id", "bib", name="uq_runner_race_bib"),
        UniqueConstraint("race_id", "chip_code", name="uq_runner_race_chip"),
    )


class ChipReplacement(Base):
    """One registered mid-race chip replacement for a runner.

    The new chip is valid from ``effective_at`` (inclusive) until the next
    replacement's effective instant (exclusive). The current (last) chip
    stays valid indefinitely. Replacements are append-only: existing rows,
    scan rows and their original chip codes are never rewritten.
    """

    __tablename__ = "chip_replacements"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("races.id", ondelete="CASCADE"), nullable=False
    )
    runner_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("runners.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 1-based: this runner's n-th replacement (the original binding is 0).
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    chip_code: Mapped[str] = mapped_column(String(128), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    race: Mapped[Race] = relationship(back_populates="chip_replacements")
    runner: Mapped[Runner] = relationship(back_populates="chip_replacements")

    __table_args__ = (
        # One runner cannot replace twice at the same sequence; a chip may be
        # introduced only once within a race.
        UniqueConstraint("race_id", "runner_id", "sequence",
                         name="uq_chiprepl_race_runner_seq"),
        UniqueConstraint("race_id", "chip_code",
                         name="uq_chiprepl_race_chip"),
    )


class Scan(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("races.id", ondelete="CASCADE"), nullable=False
    )
    scan_no: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("runners.id", ondelete="RESTRICT"),
        nullable=False,
    )
    point_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("race_points.id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Raw chip code submitted with the scan, frozen at ingestion. It is the
    # chip that was valid for the runner at the raw occurred_at; chip
    # replacement never rewrites this column.
    chip_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    race: Mapped[Race] = relationship(back_populates="scans")
    runner: Mapped[Runner] = relationship(back_populates="scans")
    point: Mapped[Point] = relationship()

    __table_args__ = (
        UniqueConstraint("race_id", "scan_no", name="uq_scan_race_no"),
        Index("ix_scan_race_runner", "race_id", "runner_id"),
        Index("ix_scan_race_point", "race_id", "point_id"),
    )


class ResultSnapshot(Base):
    __tablename__ = "result_snapshots"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    race_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        ForeignKey("races.id", ondelete="CASCADE"),
        nullable=False,
    )
    runner_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("runners.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Full frozen payload: status, finish_time, total_seconds, used_scans ...
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    race: Mapped[Race] = relationship(back_populates="snapshots")

    __table_args__ = (
        # One frozen row per runner when a race is sealed.
        UniqueConstraint("race_id", "runner_id", name="uq_snapshot_race_runner"),
    )


def init_db(max_retries: int = 30, delay_seconds: float = 1.0) -> None:
    """Create tables, waiting for PostgreSQL to accept connections.

    Used both at application startup and as the "database initialization"
    step of the one-command Docker Compose boot.
    """

    last_error: Exception | None = None
    for _ in range(max_retries):
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            break
        except Exception as exc:  # pragma: no cover - startup/boot path
            last_error = exc
            time.sleep(delay_seconds)
    else:  # pragma: no cover
        raise RuntimeError(f"database not reachable: {last_error}")

    Base.metadata.create_all(bind=engine)

    # Lightweight additive migration for deployments whose scans table
    # predates the chip-replacement feature. create_all never alters existing
    # tables, so add the raw chip_code column in place when it is missing;
    # rows created before the migration fall back to the runner's original
    # chip binding (NULL is coalesced on read).
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if "scans" in inspector.get_table_names():
        scan_columns = {col["name"] for col in inspector.get_columns("scans")}
        if "chip_code" not in scan_columns:
            with engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE scans ADD COLUMN chip_code VARCHAR(128)")
                )
