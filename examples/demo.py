#!/usr/bin/env python3
"""End-to-end calling example for the split timing adjudication API.

Only uses the Python standard library. By default it talks to the
docker-compose service:

    python3 examples/demo.py
    BASE_URL=http://127.0.0.1:8000 python3 examples/demo.py

The script walks through every requirement:
  1. create a race (start/cutoff, ordered points + finish, closed duration
     intervals per segment, chip bindings)
  2. rejected configuration: invalid time window, bad interval, dup chip
  3. submit scans out of order: idempotent replay, conflicting replay,
     unknown chip / unknown point rejection
  4. provisional results: earliest valid finish, lex-min scan-no tiebreak,
     did-not-finish cases
  5. revoke a scan before sealing -> provisional result recomputed
  6. per-point clock offsets: query (unset -> 0), set/replace signed
     integer seconds, immediate provisional recompute from the same
     committed scans; raw occurred_at kept, calibrated_at shown; non-integer
     / unknown point / sealed races rejected
  7. mid-race chip replacement: register a brand-new chip + effective time,
     scans matched against half-open chip windows on the raw device time
     (out-of-order too), out-of-window scans rejected, reused chips /
     illegal times explicit errors, conflicting replacement (revoked scans
     included) rejected as a whole without rewriting anything, timeline
     query, provisional recompute across chips, sealing freezes it
  8. seal the race -> frozen snapshot (offsets + chip timeline included);
     afterwards submit/revoke/offset change/replacement are rejected
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Fixed UTC instant so the demo is reproducible.
T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
CUTOFF = T0 + timedelta(hours=1)


def at(minute: int, second: int = 0) -> str:
    return (T0 + timedelta(minutes=minute, seconds=second)).isoformat()


def ts_equals(actual, expected: str) -> bool:
    """Compare ISO-8601 instants regardless of Z / +00:00 rendering."""
    if actual == expected:
        return True
    try:
        return datetime.fromisoformat(str(actual)) == datetime.fromisoformat(
            expected
        )
    except ValueError:
        return False


def request(method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else None
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        return exc.code, json.loads(payload) if payload else None


def step(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(status: int, body) -> None:
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2, ensure_ascii=False, default=str))


def expect(status: int, actual: int, label: str) -> None:
    if status != actual:
        print(f"!! expected {status} for {label}, got {actual}")
        sys.exit(1)
    print(f"ok [{actual}] {label}")


def main() -> None:
    # ------------------------------------------------------------------ #
    step("1. Create the race")
    # ------------------------------------------------------------------ #
    race_body = {
        "name": "City 10K 2026",
        "start_time": at(0),
        "cutoff_time": at(60),
        # Ordered points; the last one is the finish (FIN).
        "points": [
            {"code": "C1", "name": "Checkpoint 1"},
            {"code": "C2", "name": "Checkpoint 2"},
            {"code": "FIN", "name": "Finish", "is_finish": True},
        ],
        # start->C1, C1->C2, C2->FIN: closed non-negative intervals (sec)
        "segments": [
            {"min_seconds": 300, "max_seconds": 1800},
            {"min_seconds": 300, "max_seconds": 1800},
            {"min_seconds": 300, "max_seconds": 1800},
        ],
        "runners": [
            {"bib": "A01", "name": "Alice", "chip": "CHIP-A"},
            {"bib": "B02", "name": "Bob", "chip": "CHIP-B"},
            {"bib": "C03", "name": "Carol", "chip": "CHIP-C"},
            {"bib": "D04", "name": "Dan", "chip": "CHIP-D"},
        ],
    }
    status, body = request("POST", "/races", race_body)
    show(status, body)
    expect(201, status, "race created")
    race_id = body["id"]
    rp = f"/races/{race_id}"
    runner_id = {r["bib"]: r["id"] for r in body["runners"]}

    # ------------------------------------------------------------------ #
    step("2. Invalid configurations are rejected (422)")
    # ------------------------------------------------------------------ #
    bad = dict(race_body)
    bad["name"] = "bad window"
    bad["cutoff_time"] = at(-1)
    status, body = request("POST", "/races", bad)
    show(status, body)
    expect(422, status, "cutoff before start rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "bad interval"
    bad["segments"][0] = {"min_seconds": 900, "max_seconds": 300}
    status, body = request("POST", "/races", bad)
    expect(422, status, "min > max interval rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "dup chip"
    bad["runners"][1]["chip"] = "CHIP-A"
    status, body = request("POST", "/races", bad)
    expect(422, status, "duplicate chip rejected")

    bad = json.loads(json.dumps(race_body))
    bad["name"] = "segment count mismatch"
    bad["segments"].pop()
    status, body = request("POST", "/races", bad)
    expect(422, status, "segment/point count mismatch rejected")

    # ------------------------------------------------------------------ #
    step("3. Submit scans out of order")
    # ------------------------------------------------------------------ #
    def scan(scan_no, chip, point, minute, second=0):
        return request(
            "POST",
            f"{rp}/scans",
            {
                "scan_no": scan_no,
                "chip": chip,
                "point": point,
                "occurred_at": at(minute, second),
            },
        )

    # Alice: clean 10/20/30 path, plus an out-of-order early FIN scan that
    # creates a slower alternative path (25 -> 30 only 5 minutes < 300s
    # minimum, so it is unusable as C2->FIN, leaving the canonical path).
    status, _ = scan("A-030", "CHIP-A", "FIN", 30)
    expect(201, status, "Alice FIN (submitted first, out of order)")
    status, _ = scan("A-010", "CHIP-A", "C1", 10)
    expect(201, status, "Alice C1")
    status, _ = scan("A-020", "CHIP-A", "C2", 20)
    expect(201, status, "Alice C2")

    # Bob: never crosses the finish -> DNF.
    status, _ = scan("B-010", "CHIP-B", "C1", 11)
    expect(201, status, "Bob C1")
    status, _ = scan("B-020", "CHIP-B", "C2", 22)
    expect(201, status, "Bob C2 (no FIN scan)")

    # Carol: C2->FIN gap too short (2 minutes) -> no valid path -> DNF.
    status, _ = scan("C-010", "CHIP-C", "C1", 10)
    expect(201, status, "Carol C1")
    status, _ = scan("C-020", "CHIP-C", "C2", 20)
    expect(201, status, "Carol C2")
    status, _ = scan("C-022", "CHIP-C", "FIN", 22)
    expect(201, status, "Carol FIN too early (still accepted as a scan)")

    # Dan: two complete paths with identical finish time (30 min).
    #   path X: "DA01","DB01","DC01"
    #   path Y: "DA02","DB02","DC02"
    # Both finish at the same instant, so the lex-min tuple wins:
    # ("DA01","DB01","DC01") < ("DA02","DB02","DC02").
    status, _ = scan("DA02", "CHIP-D", "C1", 10)
    expect(201, status, "Dan C1 scan DA02")
    status, _ = scan("DA01", "CHIP-D", "C1", 10)
    expect(201, status, "Dan C1 scan DA01")
    status, _ = scan("DB02", "CHIP-D", "C2", 20)
    expect(201, status, "Dan C2 scan DB02")
    status, _ = scan("DB01", "CHIP-D", "C2", 20)
    expect(201, status, "Dan C2 scan DB01")
    status, _ = scan("DC02", "CHIP-D", "FIN", 30)
    expect(201, status, "Dan FIN scan DC02")
    status, _ = scan("DC01", "CHIP-D", "FIN", 30)
    expect(201, status, "Dan FIN scan DC01")

    # --- idempotency & conflict --------------------------------------- #
    status, body = scan("A-010", "CHIP-A", "C1", 10)
    show(status, body)
    expect(200, status, "same scan_no + same content is idempotent replay")
    assert body["replayed"] is True

    status, body = scan("A-010", "CHIP-A", "C1", 11)
    show(status, body)
    expect(409, status, "same scan_no + different content conflicts")

    status, body = scan("X-001", "CHIP-UNKNOWN", "C1", 10)
    show(status, body)
    expect(404, status, "unknown chip rejected")

    status, body = scan("X-002", "CHIP-A", "NEVER", 10)
    show(status, body)
    expect(404, status, "unknown point rejected")

    # --- batch endpoint ------------------------------------------------- #
    status, body = request(
        "POST",
        f"{rp}/scans/batch",
        {
            "scans": [
                # unknown chip
                {"scan_no": "Z1", "chip": "NOPE", "point": "C1",
                 "occurred_at": at(10)},
                # valid new
                {"scan_no": "BATCH-1", "chip": "CHIP-A", "point": "C1",
                 "occurred_at": at(12)},
                # idempotent replay
                {"scan_no": "A-010", "chip": "CHIP-A", "point": "C1",
                 "occurred_at": at(10)},
            ]
        },
    )
    show(status, body)
    expect(200, status, "batch accepted with per-item statuses")
    assert [item["status_code"] for item in body] == [404, 201, 200]

    # BATCH-1 gives Alice a C1 at minute 12; earliest finish still uses
    # A-010(10) -> A-020(20) -> A-030(30).

    # ------------------------------------------------------------------ #
    step("4. Provisional results (before sealing)")
    # ------------------------------------------------------------------ #
    status, results = request("GET", f"{rp}/results")
    show(status, results)
    expect(200, status, "provisional results")
    by_bib = {r["bib"]: r for r in results["results"]}

    assert by_bib["A01"]["status"] == "finished"
    assert [u["scan_no"] for u in by_bib["A01"]["used_scans"]] == [
        "A-010", "A-020", "A-030"
    ]
    assert by_bib["A01"]["rank"] == 1
    print("ok Alice fastest, rank 1, scans A-010/A-020/A-030")

    assert by_bib["B02"]["status"] == "did_not_finish"
    print("ok Bob DNF (no finish scan)")

    assert by_bib["C03"]["status"] == "did_not_finish"
    print("ok Carol DNF (segment outside allowed interval)")

    assert by_bib["D04"]["status"] == "finished"
    assert [u["scan_no"] for u in by_bib["D04"]["used_scans"]] == [
        "DA01", "DB01", "DC01"
    ]
    assert by_bib["D04"]["finish_time"] == by_bib["A01"]["finish_time"]
    assert by_bib["D04"]["rank"] == 2
    print("ok Dan tied finish time -> lex-min scan sequence DA01/DB01/DC01")

    # single-runner view
    status, body = request(
        "GET", f"{rp}/runners/{runner_id['A01']}/result"
    )
    expect(200, status, "single runner result")
    assert body["bib"] == "A01"

    # ------------------------------------------------------------------ #
    step("5. Revoke a scan before sealing -> results recompute")
    # ------------------------------------------------------------------ #
    # Revoke Alice's FIN scan. She drops to DNF provisionally.
    status, body = request("DELETE", f"{rp}/scans/A-030")
    show(status, body)
    expect(200, status, "scan revoked")

    status, results = request("GET", f"{rp}/results")
    by_bib = {r["bib"]: r for r in results["results"]}
    assert by_bib["A01"]["status"] == "did_not_finish"
    print("ok Alice provisionally DNF after her FIN scan was revoked")

    # double revoke rejected
    status, body = request("DELETE", f"{rp}/scans/A-030")
    show(status, body)
    expect(409, status, "revoking an already-revoked scan rejected")

    # Revoking an unknown scan 404s
    status, body = request("DELETE", f"{rp}/scans/NO-SUCH-SCAN")
    expect(404, status, "unknown scan revocation 404")

    # ------------------------------------------------------------------ #
    step("6. Per-point clock offsets")
    # ------------------------------------------------------------------ #
    # A dedicated race makes the offset arithmetic easy to follow.
    status, off_race_body = request(
        "POST",
        "/races",
        {
            "name": "Clock offset demo",
            "start_time": at(0),
            "cutoff_time": at(60),
            "points": [
                {"code": "C1", "name": "Checkpoint 1"},
                {"code": "FIN", "name": "Finish", "is_finish": True},
            ],
            "segments": [
                {"min_seconds": 300, "max_seconds": 3600},
                {"min_seconds": 300, "max_seconds": 3600},
            ],
            "runners": [
                {"bib": "E01", "name": "Eve", "chip": "CHIP-E"},
            ],
        },
    )
    expect(201, status, "offset demo race created")
    orp = f"/races/{off_race_body['id']}"

    # No offset configured yet -> every point reports 0.
    status, body = request("GET", f"{orp}/offsets")
    show(status, body)
    expect(200, status, "list offsets")
    assert body["offset_version"] == 0
    assert [(o["point"], o["offset_seconds"], o["configured"]) for o in body["offsets"]] == [
        ("C1", 0, False), ("FIN", 0, False),
    ]
    print("ok unset offsets read back as zero")

    # Scans: C1 at raw minute 10, FIN at raw minute 61 -- the raw finish is
    # one minute past the cutoff, so Eve is DNF before calibration.
    def oscan(scan_no, point, minute, second=0):
        return request(
            "POST",
            f"{orp}/scans",
            {
                "scan_no": scan_no,
                "chip": "CHIP-E",
                "point": point,
                "occurred_at": at(minute, second),
            },
        )

    status, _ = oscan("E-010", "C1", 10)
    expect(201, status, "Eve C1 scan")
    status, _ = oscan("E-061", "FIN", 61)
    expect(201, status, "Eve FIN scan (raw minute 61, after cutoff)")

    status, body = request("GET", f"{orp}/results")
    assert body["results"][0]["status"] == "did_not_finish"
    print("ok raw finish after cutoff -> DNF")

    # Set FIN's device to -60 s (clock runs fast): calibrated finish is
    # minute 60, exactly at the cutoff -> finished.
    status, body = request(
        "PUT", f"{orp}/points/FIN/offset", {"offset_seconds": -60}
    )
    show(status, body)
    expect(200, status, "set FIN offset -60")
    assert body["offset_version"] == 1
    item = body["offsets"][0]
    assert item["point"] == "FIN" and item["offset_seconds"] == -60
    assert item["configured"] is True and item["replaced"] is False
    print("ok first set reports replaced=false and bumps offset_version")

    status, body = request("GET", f"{orp}/results")
    show(status, body)
    expect(200, status, "results recomputed after offset change")
    assert body["offset_version"] == 1
    row = body["results"][0]
    assert row["status"] == "finished"
    finish_used = [u for u in row["used_scans"] if u["point"] == "FIN"][0]
    # Raw scan time is preserved; calibrated time and totals are shifted.
    assert ts_equals(finish_used["occurred_at"], at(61))
    assert ts_equals(finish_used["calibrated_at"], at(60))
    assert finish_used["offset_seconds"] == -60
    assert ts_equals(row["finish_time"], at(60))
    assert row["total_seconds"] == 3600.0
    print("ok provisional result immediately uses the new offset; raw kept")

    # Replace the offset (same endpoint, new value) -> replaced=true, another
    # version bump; calibrated minute 59 changes total/rank data immediately.
    status, body = request(
        "PUT", f"{orp}/points/FIN/offset", {"offset_seconds": -120}
    )
    expect(200, status, "replace FIN offset -120")
    assert body["offset_version"] == 2
    assert body["offsets"][0]["replaced"] is True
    status, body = request("GET", f"{orp}/results")
    assert body["offset_version"] == 2
    assert ts_equals(body["results"][0]["finish_time"], at(59))
    print("ok replacing an offset bumps the version and recomputes again")

    # Non-integer offsets are explicitly refused (pydantic strict int -> 422).
    for bad in (1.5, "2", True):
        status, body = request(
            "PUT", f"{orp}/points/C1/offset", {"offset_seconds": bad}
        )
        show(status, body)
        expect(422, status, f"non-integer offset {bad!r} rejected")

    # Unknown point / unknown race -> 404.
    status, body = request(
        "PUT", f"{orp}/points/NOPE/offset", {"offset_seconds": 1}
    )
    show(status, body)
    expect(404, status, "unknown point offset rejected")

    status, body = request(
        "PUT",
        "/races/00000000-0000-0000-0000-000000000000/points/C1/offset",
        {"offset_seconds": 1},
    )
    expect(404, status, "unknown race offset rejected")

    # Listing again still shows the committed values (-120 at FIN, 0 at C1).
    status, body = request("GET", f"{orp}/offsets")
    assert {o["point"]: o["offset_seconds"] for o in body["offsets"]} == {
        "C1": 0, "FIN": -120
    }
    print("ok offset query returns current per-point values")

    # Seal this race; its snapshot freezes the calibrated results.
    status, sealed_off = request("POST", f"{orp}/seal")
    expect(200, status, "offset race sealed")
    assert sealed_off["offset_version"] == 2
    snap_finish = [
        u for r in sealed_off["results"] for u in r["used_scans"]
        if u["point"] == "FIN"
    ][0]
    assert ts_equals(snap_finish["occurred_at"], at(61))
    assert ts_equals(snap_finish["calibrated_at"], at(59))
    print("ok sealed snapshot keeps raw times and calibrated times")

    # After sealing: offset changes rejected; the snapshot stays as frozen.
    status, body = request(
        "PUT", f"{orp}/points/FIN/offset", {"offset_seconds": 0}
    )
    show(status, body)
    expect(409, status, "offset change after sealing rejected")

    status, body = request("GET", f"{orp}/results")
    assert body["offset_version"] == 2
    assert ts_equals(body["results"][0]["finish_time"], at(59))
    print("ok sealed results are unaffected by later requests")

    # The offset query still works on a sealed race (read-only).
    status, body = request("GET", f"{orp}/offsets")
    expect(200, status, "offset query still allowed after sealing")

    # ------------------------------------------------------------------ #
    step("7. Mid-race chip replacement")
    # ------------------------------------------------------------------ #
    # A dedicated race keeps the chip-window arithmetic easy to read.
    # Window: start minute 0 -> cutoff minute 60, two points (C1, FIN).
    status, chip_race_body = request(
        "POST",
        "/races",
        {
            "name": "Chip replacement demo",
            "start_time": at(0),
            "cutoff_time": at(60),
            "points": [
                {"code": "C1", "name": "Checkpoint 1"},
                {"code": "FIN", "name": "Finish", "is_finish": True},
            ],
            "segments": [
                {"min_seconds": 0, "max_seconds": 3600},
                {"min_seconds": 0, "max_seconds": 3600},
            ],
            "runners": [
                {"bib": "F01", "name": "Frank", "chip": "F-OLD"},
                {"bib": "K02", "name": "Kim", "chip": "K-OLD"},
            ],
        },
    )
    expect(201, status, "chip demo race created")
    crp = f"/races/{chip_race_body['id']}"
    frank_id = next(
        r["id"] for r in chip_race_body["runners"] if r["bib"] == "F01"
    )
    assert chip_race_body["runners"][0]["current_chip"] == "F-OLD"
    print("ok race output exposes current_chip (== chip before replacement)")

    # Initial timeline: one unbounded window for the original chip.
    status, body = request("GET", f"{crp}/runners/{frank_id}/chip-timeline")
    show(status, body)
    expect(200, status, "read initial chip timeline")
    assert body["replacement_count"] == 0
    assert body["original_chip"] == body["current_chip"] == "F-OLD"
    assert len(body["periods"]) == 1
    assert body["periods"][0]["effective_from"] is None
    assert body["periods"][0]["effective_to"] is None
    print("ok original chip has one unbounded window [null, null)")

    # Unknown race / runner give a clear 404.
    status, body = request(
        "GET",
        f"/races/00000000-0000-0000-0000-000000000000/runners/{frank_id}/chip-timeline",
    )
    expect(404, status, "timeline unknown race 404")
    status, body = request(
        "POST", f"{crp}/chip-replacements",
        {"runner_id": "not-a-uuid", "new_chip": "X",
         "effective_at": at(10)},
    )
    expect(404, status, "replacement invalid runner id 404")
    status, body = request(
        "POST", f"{crp}/chip-replacements",
        {"runner_id": "00000000-0000-0000-0000-000000000000",
         "new_chip": "X", "effective_at": at(10)},
    )
    expect(404, status, "replacement unknown runner 404")

    # Illegal effective times -> 422 invalid_effective_time.
    for bad_at in (at(-1), at(61)):
        status, body = request(
            "POST", f"{crp}/chip-replacements",
            {"runner_id": frank_id, "new_chip": "F-TMP",
             "effective_at": bad_at},
        )
        show(status, body)
        expect(422, status, f"effective_at {bad_at} outside window rejected")
        assert body["error"] == "invalid_effective_time"

    # Reused chips (any original binding or earlier replacement in THIS
    # race) -> 409 chip_already_used.
    for used in ("F-OLD", "K-OLD"):
        status, body = request(
            "POST", f"{crp}/chip-replacements",
            {"runner_id": frank_id, "new_chip": used,
             "effective_at": at(10)},
        )
        show(status, body)
        expect(409, status, f"reused chip {used} rejected")
        assert body["error"] == "chip_already_used"

    def fscan(scan_no, chip, point, minute, second=0):
        return request(
            "POST", f"{crp}/scans",
            {"scan_no": scan_no, "chip": chip, "point": point,
             "occurred_at": at(minute, second)},
        )

    # Frank on the original chip: C1 @10, FIN @20 (later revoked).
    status, _ = fscan("F-C1", "F-OLD", "C1", 10)
    expect(201, status, "Frank C1 on original chip")
    status, _ = fscan("F-FIN-OLD", "F-OLD", "FIN", 20)
    expect(201, status, "Frank FIN on original chip")
    status, _ = request("DELETE", f"{crp}/scans/F-FIN-OLD")
    expect(200, status, "revoke Frank old FIN scan")

    # Replacing at minute 15 would push the revoked FIN (@20, old chip) into
    # the new chip's window: the WHOLE request is rejected and the conflict
    # list names the revoked scan. Nothing is rewritten.
    status, body = request(
        "POST", f"{crp}/chip-replacements",
        {"runner_id": frank_id, "new_chip": "F-NEW",
         "effective_at": at(15)},
    )
    show(status, body)
    expect(409, status, "conflicting replacement rejected as a whole")
    assert body["error"] == "chip_replacement_conflict"
    assert [c["scan_no"] for c in body["conflicts"]] == ["F-FIN-OLD"]
    assert body["conflicts"][0]["revoked"] is True
    print("ok conflict list includes revoked scans; no stored data changes")
    status, body = request("GET", f"{crp}/runners/{frank_id}/chip-timeline")
    assert body["replacement_count"] == 0
    print("ok no replacement row is left behind after the rejection")

    # Move the effective instant strictly after the offending raw timestamp.
    status, body = request(
        "POST", f"{crp}/chip-replacements",
        {"runner_id": frank_id, "new_chip": "F-NEW",
         "effective_at": at(22)},
    )
    show(status, body)
    expect(201, status, "replacement registered at minute 22")
    assert body["replacement_count"] == 1
    assert body["current_chip"] == "F-NEW"
    periods = body["periods"]
    assert periods[0]["chip"] == "F-OLD" and periods[0]["is_initial"]
    assert ts_equals(periods[0]["effective_to"], at(22))
    assert periods[1]["chip"] == "F-NEW"
    assert ts_equals(periods[1]["effective_from"], at(22))
    assert periods[1]["effective_to"] is None
    print("ok windows: F-OLD [null,22) then F-NEW [22, null)")

    # Scans are matched on the raw device time against the half-open windows;
    # the boundary instant itself already belongs to the new chip.
    status, body = fscan("F-OLD-AT22", "F-OLD", "FIN", 22)
    show(status, body)
    expect(409, status, "old chip at the effective instant explicitly rejected")
    assert body["error"] == "scan_outside_chip_period"
    assert "F-NEW" in body["detail"]
    print("ok out-of-window rejection names the active chip")

    status, body = fscan("F-FIN-NEW", "F-NEW", "FIN", 22)
    show(status, body)
    expect(201, status, "new chip at the effective instant accepted")
    assert body["chip"] == "F-NEW"

    # Out-of-order late delivery: a minute-12 observation uploaded AFTER the
    # replacement is judged by its raw time -> F-NEW is not valid then, the
    # active chip was F-OLD.
    status, body = fscan("F-LATE", "F-NEW", "C1", 12)
    show(status, body)
    expect(409, status, "late new-chip scan at an old instant rejected")
    assert body["error"] == "scan_outside_chip_period"
    assert "F-OLD" in body["detail"]
    print("ok乱序补传按原始发生时间判定 (out-of-order judged by raw time)")
    status, _ = fscan("F-LATE2", "F-OLD", "C1", 11)
    expect(201, status, "late old-chip scan inside old window accepted")

    # The replay/conflict convention for an already known scan_no is
    # unchanged and takes precedence over the chip-period rule.
    status, body = fscan("F-C1", "F-OLD", "C1", 10)
    expect(200, status, "identical retransmission still replays 200")
    assert body["replayed"] is True
    status, body = fscan("F-C1", "F-OLD", "C1", 13)
    expect(409, status, "different retransmission still scan_conflict")

    # A runner may replace multiple times; each effective time must be
    # strictly later than the previous one.
    status, body = request(
        "POST", f"{crp}/chip-replacements",
        {"runner_id": frank_id, "new_chip": "F-NEW2",
         "effective_at": at(22)},
    )
    show(status, body)
    expect(422, status, "non-strictly-later effective time rejected")
    status, body = request(
        "POST", f"{crp}/chip-replacements",
        {"runner_id": frank_id, "new_chip": "F-NEW2",
         "effective_at": at(30)},
    )
    expect(201, status, "second replacement at minute 30 accepted")
    assert [p["chip"] for p in body["periods"]] == [
        "F-OLD", "F-NEW", "F-NEW2"
    ]
    assert body["current_chip"] == "F-NEW2"
    print("ok multiple replacements form contiguous half-open windows")
    status, body = fscan("F-MID-AT30", "F-NEW", "FIN", 30)
    expect(409, status, "middle chip scan at next boundary rejected")

    # Provisional results recompute under the same rules and keep every used
    # scan's original chip. Frank crosses chips: C1@10 F-OLD -> FIN@22 F-NEW.
    status, results = request("GET", f"{crp}/results")
    show(status, results)
    expect(200, status, "provisional results after replacements")
    frank = next(r for r in results["results"] if r["bib"] == "F01")
    assert frank["status"] == "finished"
    assert frank["current_chip"] == "F-NEW2"
    assert [(u["point"], u["chip"]) for u in frank["used_scans"]] == [
        ("C1", "F-OLD"), ("FIN", "F-NEW")
    ]
    print("ok finish path crosses chips; raw chip preserved per used scan")

    # Sealing freezes the timeline/results; replacement is rejected after.
    status, sealed_chip = request("POST", f"{crp}/seal")
    expect(200, status, "chip demo race sealed")
    snap = next(r for r in sealed_chip["results"] if r["bib"] == "F01")
    assert [u["chip"] for u in snap["used_scans"]] == ["F-OLD", "F-NEW"]
    status, body = request(
        "POST", f"{crp}/chip-replacements",
        {"runner_id": frank_id, "new_chip": "F-AFTER",
         "effective_at": at(40)},
    )
    show(status, body)
    expect(409, status, "replacement after sealing rejected")
    # Timeline stays readable and the snapshot never changes.
    status, body = request("GET", f"{crp}/runners/{frank_id}/chip-timeline")
    expect(200, status, "timeline still readable after sealing")
    assert body["current_chip"] == "F-NEW2"
    status, body = request("GET", f"{crp}/results")
    assert body["results"] == sealed_chip["results"]
    print("ok sealed snapshot/timeline are frozen and unaffected")

    # ------------------------------------------------------------------ #
    step("8. Seal the main race and verify the frozen snapshot")
    # ------------------------------------------------------------------ #
    status, sealed = request("POST", f"{rp}/seal")
    show(status, sealed)
    expect(200, status, "race sealed")
    assert sealed["sealed"] is True

    # Sealing is idempotent and returns the same snapshot.
    status, sealed_again = request("POST", f"{rp}/seal")
    expect(200, status, "re-seal is idempotent")
    assert sealed_again["results"] == sealed["results"]
    print("ok repeated seal returns the same frozen snapshot")

    # GET results now comes from the snapshot (Alice stays DNF).
    status, body = request("GET", f"{rp}/results")
    by_bib = {r["bib"]: r for r in body["results"]}
    assert by_bib["A01"]["status"] == "did_not_finish"
    print("ok snapshot serves results after sealing")

    # After sealing: submit and revoke are rejected.
    status, body = scan("LATE", "CHIP-A", "FIN", 35)
    show(status, body)
    expect(409, status, "scan submission after sealing rejected")

    status, body = request("DELETE", f"{rp}/scans/BATCH-1")
    show(status, body)
    expect(409, status, "revocation after sealing rejected")

    print("\nALL DEMO STEPS PASSED")


if __name__ == "__main__":
    main()
