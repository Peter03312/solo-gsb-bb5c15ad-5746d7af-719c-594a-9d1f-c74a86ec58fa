"""HTTP-level tests for mid-race chip replacement.

Uses an in-memory SQLite database (PostgreSQL is not required): the
PostgreSQL-only column types are compiled to SQLite equivalents only here,
the application code is untouched. Run:

    pytest tests/test_api_chip_replacement.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")

# --- switch the engine to in-memory SQLite before app modules import it ---
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"

from sqlalchemy import event  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app import database  # noqa: E402

# The PostgreSQL-only column types already compile onto SQLite via
# app/database.py (UUID->VARCHAR, JSONB->JSON, BIGINT PK->INTEGER).
# Recreate the SQLite engine with a shared in-memory connection pool so the
# TestClient thread and the test thread see one database.
database.engine = database.create_engine(
    "sqlite+pysqlite:///:memory:",
    future=True,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
database.SessionLocal.configure(bind=database.engine)


@event.listens_for(database.engine, "connect")
def _fk_on(dbapi_conn, _record):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


database.init_db()

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

UTC = timezone.utc
T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=UTC)
CUTOFF = T0 + timedelta(hours=1)


def iso(dt):
    return dt.isoformat()


def parsed(value):
    # ISO strings may be rendered with a trailing 'Z' (or, on SQLite test
    # storage, without an explicit offset); compare all as UTC instants.
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def same_instant(a, b):
    return parsed(a) == parsed(b)


def at(minute, second=0):
    return iso(T0 + timedelta(minutes=minute, seconds=second))


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def make_race(client, runners=None, name="chip race", points=None,
              segments=None):
    points = points or [
        {"code": "C1", "name": "cp1"},
        {"code": "FIN", "name": "Finish", "is_finish": True},
    ]
    segments = segments or [
        {"min_seconds": 60, "max_seconds": 3600},
        {"min_seconds": 60, "max_seconds": 3600},
    ]
    runners = runners or [
        {"bib": "A01", "name": "Alice", "chip": "CHIP-A"},
        {"bib": "B02", "name": "Bob", "chip": "CHIP-B"},
    ]
    r = client.post(
        "/races",
        json={
            "name": name,
            "start_time": at(0),
            "cutoff_time": at(60),
            "points": points,
            "segments": segments,
            "runners": runners,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["id"], {x["bib"]: x["id"] for x in body["runners"]}


def scan(client, race, scan_no, chip, point, minute, second=0):
    return client.post(
        f"/races/{race}/scans",
        json={
            "scan_no": scan_no,
            "chip": chip,
            "point": point,
            "occurred_at": at(minute, second),
        },
    )


# --------------------------------------------------------------------------


def test_timeline_initial_binding(client):
    race, runners = make_race(client)
    r = client.get(f"/races/{race}/runners/{runners['A01']}/chip-timeline")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["initial_chip"] == "CHIP-A"
    assert body["current_chip"] == "CHIP-A"
    assert len(body["replacements"]) == 0
    assert len(body["periods"]) == 1
    p = body["periods"][0]
    assert p["chip"] == "CHIP-A"
    assert same_instant(p["valid_from"], at(0))
    assert p["valid_to"] is None
    assert p["is_current"] is True


def test_unknown_race_and_runner_404(client):
    r = client.get(
        "/races/00000000-0000-0000-0000-000000000000/runners/"
        "00000000-0000-0000-0000-000000000000/chip-timeline"
    )
    assert r.status_code == 404
    race, runners = make_race(client)
    r = client.get(
        f"/races/{race}/runners/00000000-0000-0000-0000-000000000000/"
        "chip-timeline"
    )
    assert r.status_code == 404
    assert r.json()["error"] == "runner_not_found"


def test_register_replacement_and_scan_matching_boundary(client):
    race, runners = make_race(client)
    alice = runners["A01"]

    # Pre-replacement scan on the old chip at minute 10.
    assert scan(client, race, "S10", "CHIP-A", "C1", 10).status_code == 201

    r = client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(15)},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["registered"] is True
    assert body["current_chip"] == "CHIP-A2"
    assert [p["chip"] for p in body["periods"]] == ["CHIP-A", "CHIP-A2"]
    p0, p1 = body["periods"]
    assert same_instant(p0["valid_to"], at(15)) and p0["is_current"] is False
    assert same_instant(p1["valid_from"], at(15)) and p1["valid_to"] is None
    assert len(body["replacements"]) == 1
    ev = body["replacements"][0]
    assert ev["old_chip"] == "CHIP-A" and ev["new_chip"] == "CHIP-A2"
    assert same_instant(ev["effective_at"], at(15)) and ev["sequence"] == 1

    # Old chip strictly before the boundary still matches (out-of-order
    # backfill), exact boundary and after must be rejected; the new chip is
    # valid at/after the boundary.
    assert scan(client, race, "S14", "CHIP-A", "C1", 14).status_code == 201
    r = scan(client, race, "S15OLD", "CHIP-A", "C1", 15)
    assert r.status_code == 409
    assert r.json()["error"] == "chip_outside_valid_period"
    assert scan(client, race, "S15", "CHIP-A2", "C1", 15).status_code == 201
    assert scan(client, race, "S16", "CHIP-A2", "FIN", 16).status_code == 201

    # Old chip before start: explicit pre-start rejection.
    r = scan(client, race, "SPRE", "CHIP-A", "C1", -1)
    assert r.status_code == 409
    assert r.json()["error"] == "scan_before_race_start"

    # Post-cutoff observations are still ingested (the final chip window is
    # open-ended; the cutoff governs adjudication, not ingestion).
    assert scan(client, race, "SLATE", "CHIP-A2", "FIN", 61).status_code == 201


def test_multiple_replacements_require_later_effective_time(client):
    race, runners = make_race(client)
    alice = runners["A01"]
    post = lambda chip, minute: client.post(  # noqa: E731
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": chip, "effective_at": at(minute)},
    )
    assert post("CHIP-A2", 20).status_code == 201
    r = post("CHIP-A3", 20)  # equal -> rejected
    assert r.status_code == 422 and r.json()["error"] == "invalid_effective_time"
    r = post("CHIP-A3", 19)  # earlier -> rejected
    assert r.status_code == 422
    r = post("CHIP-A3", -1)  # before start
    assert r.status_code == 422 and r.json()["error"] == "invalid_effective_time"
    r = post("CHIP-A3", 60, )  # exactly cutoff -> allowed (closed window)
    assert r.status_code == 201, r.text
    r = post("CHIP-A4", 61)  # after cutoff
    assert r.status_code == 422

    # Three periods on the timeline; middle chip window [20,60).
    body = client.get(f"/races/{race}/runners/{alice}/chip-timeline").json()
    assert [p["chip"] for p in body["periods"]] == [
        "CHIP-A", "CHIP-A2", "CHIP-A3"
    ]
    assert body["replacements"][1]["sequence"] == 2
    assert scan(client, race, "M", "CHIP-A2", "C1", 59, 59).status_code == 201
    r = scan(client, race, "M2", "CHIP-A2", "C1", 60)
    assert r.status_code == 409 and r.json()["error"] == "chip_outside_valid_period"
    assert scan(client, race, "M3", "CHIP-A3", "C1", 60).status_code == 201


def test_reused_chip_rejected(client):
    race, runners = make_race(client)
    alice, bob = runners["A01"], runners["B02"]

    # Another runner's current binding.
    r = client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-B", "effective_at": at(10)},
    )
    assert r.status_code == 409 and r.json()["error"] == "chip_reused"

    assert client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(10)},
    ).status_code == 201

    # Reusing Alice's own initial/current chip and a prior replacement chip.
    r = client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A", "effective_at": at(20)},
    )
    assert r.status_code == 409 and r.json()["error"] == "chip_reused"
    r = client.post(
        f"/races/{race}/runners/{bob}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(20)},
    )
    assert r.status_code == 409 and r.json()["error"] == "chip_reused"

    # Unknown chip on scan is still 404.
    r = scan(client, race, "X", "NOPE", "C1", 12)
    assert r.status_code == 404 and r.json()["error"] == "unknown_chip"


def test_replacement_conflicting_with_received_scans_rejected(client):
    race, runners = make_race(client)
    alice = runners["A01"]
    # Old chip has a scan at minute 20 (active scans and revoked scans alike).
    assert scan(client, race, "S20", "CHIP-A", "C1", 20).status_code == 201

    r = client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(20)},
    )
    assert r.status_code == 409
    err = r.json()
    assert err["error"] == "chip_replacement_conflict"
    assert "S20" in err["detail"] or "scan chip='CHIP-A'" in err["detail"]

    # Earlier than every received old-chip scan is accepted (no rewrite).
    assert client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(21)},
    ).status_code == 201

    # The conflicting request changed nothing: S20 still reports CHIP-A and
    # there is exactly one replacement.
    body = client.get(f"/races/{race}/results").json()
    alice_row = next(x for x in body["results"] if x["bib"] == "A01")
    # S20 was at C1 only -> DNF, but the scan keeps its original chip if shown.
    assert scan(client, race, "S20", "CHIP-A", "C1", 20).status_code == 200
    replay = scan(client, race, "S20", "CHIP-A", "C1", 20).json()
    assert replay["chip"] == "CHIP-A" and replay["replayed"] is True
    tl = client.get(f"/races/{race}/runners/{alice}/chip-timeline").json()
    assert len(tl["replacements"]) == 1


def test_conflict_includes_revoked_scan(client):
    race, runners = make_race(client)
    alice = runners["A01"]
    assert scan(client, race, "S30", "CHIP-A", "FIN", 30).status_code == 201
    assert client.delete(f"/races/{race}/scans/S30").status_code == 200
    # Even revoked scans block a replacement that would invalidate them.
    r = client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(30)},
    )
    assert r.status_code == 409
    assert r.json()["error"] == "chip_replacement_conflict"


def test_results_recompute_with_mixed_chips_and_original_chip_preserved(client):
    race, runners = make_race(client)
    alice = runners["A01"]
    # C1 at minute 10 on the old chip; switch at minute 15; FIN on new chip.
    assert scan(client, race, "A-010", "CHIP-A", "C1", 10).status_code == 201
    assert client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(15)},
    ).status_code == 201
    assert scan(client, race, "A-020", "CHIP-A2", "FIN", 20).status_code == 201

    body = client.get(f"/races/{race}/results").json()
    row = next(x for x in body["results"] if x["bib"] == "A01")
    assert row["status"] == "finished"
    used = {u["point"]: u for u in row["used_scans"]}
    assert used["C1"]["chip"] == "CHIP-A" and used["C1"]["scan_no"] == "A-010"
    assert used["FIN"]["chip"] == "CHIP-A2" and used["FIN"]["scan_no"] == "A-020"
    assert row["chip"] == "CHIP-A2"  # current chip on the runner header


def test_sealed_race_rejects_replacement_and_snapshot_frozen(client):
    race, runners = make_race(client)
    alice = runners["A01"]
    assert client.post(f"/races/{race}/seal").status_code == 200
    r = client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(10)},
    )
    assert r.status_code == 409 and r.json()["error"] == "race_sealed"

    # Timeline is still readable after sealing.
    assert client.get(
        f"/races/{race}/runners/{alice}/chip-timeline"
    ).status_code == 200


def test_batch_item_outside_period_gets_409(client):
    race, runners = make_race(client)
    alice = runners["A01"]
    assert client.post(
        f"/races/{race}/runners/{alice}/chip-replacements",
        json={"new_chip": "CHIP-A2", "effective_at": at(15)},
    ).status_code == 201
    r = client.post(
        f"/races/{race}/scans/batch",
        json={
            "scans": [
                {"scan_no": "OK", "chip": "CHIP-A2", "point": "C1",
                 "occurred_at": at(16)},
                {"scan_no": "OLD", "chip": "CHIP-A", "point": "C1",
                 "occurred_at": at(16)},
            ]
        },
    )
    assert r.status_code == 200
    items = r.json()
    assert [i["status_code"] for i in items] == [201, 409]
    assert items[1]["error"] == "chip_outside_valid_period"


def test_same_scan_no_conflict_convention_unchanged(client):
    race, _ = make_race(client)
    assert scan(client, race, "N1", "CHIP-A", "C1", 10).status_code == 201
    # identical replay
    r = scan(client, race, "N1", "CHIP-A", "C1", 10)
    assert r.status_code == 200 and r.json()["replayed"] is True
    # different time conflicts
    r = scan(client, race, "N1", "CHIP-A", "C1", 11)
    assert r.status_code == 409 and r.json()["error"] == "scan_conflict"
