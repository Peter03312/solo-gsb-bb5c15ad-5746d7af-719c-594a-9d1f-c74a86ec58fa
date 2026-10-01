"""SQLite compatibility shim used ONLY by local integration tests.

Production runs on PostgreSQL 16 (see docker-compose.yml). The local sandbox
may not provide Postgres, so the integration tests point DATABASE_URL at a
SQLite file. Two tiny dialect adaptations are required:

1. The ORM uses PostgreSQL-only column types ``JSONB`` and ``UUID``; compile
   them to portable SQLite types here.
2. SQLite does not round-trish timezone-aware datetimes: pydantic normalises
   every inbound timestamp to tz-aware UTC, so wrap the SQLite DATETIME
   processors to append ``+00:00`` on the way out. Comparisons in service
   code then stay aware-vs-aware.

Importing this module before ``app.database`` is enough; nothing in ``app/``
imports it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import BigInteger
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.types import JSON


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(element, compiler, **kw):  # noqa: ARG001
    # Only INTEGER PRIMARY KEY acts as the SQLite rowid (auto-increment);
    # a BIGINT PK column would otherwise stay NULL on insert.
    return "INTEGER"


@compiles(UUID, "sqlite")
def _uuid_sqlite(element, compiler, **kw):  # noqa: ARG001
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(element, compiler, **kw):  # noqa: ARG001
    return compiler.visit_JSON(JSON(), **kw)


# tz-aware round-trip -------------------------------------------------------
# pysqlite's DATETIME subclass overrides the processors, so patch that class
# (rather than only the generic DateTime base).
from sqlalchemy.dialects.sqlite.base import DATETIME as _SqliteDateTime

_orig_dt_bind = _SqliteDateTime.bind_processor
_orig_dt_result = _SqliteDateTime.result_processor


def _aware_bind_processor(self, dialect):
    base = _orig_dt_bind(self, dialect)
    if base is None:
        return None

    def process(value):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return base(value)

    return process


def _aware_result_processor(self, dialect, coltype):
    base = _orig_dt_result(self, dialect, coltype)
    if base is None:
        return None

    def process(value):
        parsed = base(value)
        if isinstance(parsed, datetime) and parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed

    return process


_SqliteDateTime.bind_processor = _aware_bind_processor
_SqliteDateTime.result_processor = _aware_result_processor

os.environ.setdefault(
    "DATABASE_URL", "sqlite+pysqlite:///" + os.path.abspath("test_race.db")
)
