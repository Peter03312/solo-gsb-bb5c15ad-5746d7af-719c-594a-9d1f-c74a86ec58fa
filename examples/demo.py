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
  7. seal the race -> frozen snapshot (offsets included); afterwards
     submit/revoke/offset change are rejected
  8. mid-race chip replacement: half-open chip windows matched on the raw
     device time (out-of-order backfill included), explicit rejection
     outside windows, all-or-nothing conflicts with received/revoked scans,
     reused-chip / illegal-time / sealed errors, timeline query, results
     recomputed across the original and replacement chips
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


def instant(value: str) -> datetime:
    """Parse an API timestamp, normalising 'Z'/naive forms to UTC, so the
    example compares instants regardless of the backend's rendering."""

    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


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
    assert instant(finish_used["occurred_at"]) == instant(at(61))
    assert instant(finish_used["calibrated_at"]) == instant(at(60))
    assert finish_used["offset_seconds"] == -60
    assert instant(row["finish_time"]) == instant(at(60))
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
    assert instant(body["results"][0]["finish_time"]) == instant(at(59))
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
    assert instant(snap_finish["occurred_at"]) == instant(at(61))
    assert instant(snap_finish["calibrated_at"]) == instant(at(59))
    print("ok sealed snapshot keeps raw times and calibrated times")

    # After sealing: offset changes rejected; the snapshot stays as frozen.
    status, body = request(
        "PUT", f"{orp}/points/FIN/offset", {"offset_seconds": 0}
    )
    show(status, body)
    expect(409, status, "offset change after sealing rejected")

    status, body = request("GET", f"{orp}/results")
    assert body["offset_version"] == 2
    assert instant(body["results"][0]["finish_time"]) == instant(at(59))
    print("ok sealed results are unaffected by later requests")

    # The offset query still works on a sealed race (read-only).
    status, body = request("GET", f"{orp}/offsets")
    expect(200, status, "offset query still allowed after sealing")

    # ------------------------------------------------------------------ #
    step("7. Seal the main race and verify the frozen snapshot")
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

    # ------------------------------------------------------------------ #
    step("8. Mid-race chip replacement")
    # ------------------------------------------------------------------ #
    # Frank starts on CHIP-F. His chip breaks mid-race: staff register a
    # replacement to CHIP-F2 effective at minute 15. The timeline shows the
    # half-open chip windows; scans are matched on the raw device time.
    status, chip_race_body = request(
        "POST",
        "/races",
        {
            "name": "Chip replacement demo",
            "start_time": at(0),
            "cutoff_time": at(60),
            "points": [
                {"code": "C1", "name": "Checkpoint 1"},
                {"code": "C2", "name": "Checkpoint 2"},
                {"code": "FIN", "name": "Finish", "is_finish": True},
            ],
            "segments": [
                {"min_seconds": 300, "max_seconds": 3600},
                {"min_seconds": 300, "max_seconds": 3600},
                {"min_seconds": 300, "max_seconds": 3600},
            ],
            "runners": [
                {"bib": "F01", "name": "Frank", "chip": "CHIP-F"},
                {"bib": "G02", "name": "Grace", "chip": "CHIP-G"},
            ],
        },
    )
    expect(201, status, "chip-replacement race created")
    crp = f"/races/{chip_race_body['id']}"
    frank_id = next(
        r["id"] for r in chip_race_body["runners"] if r["bib"] == "F01"
    )
    grace_id = next(
        r["id"] for r in chip_race_body["runners"] if r["bib"] == "G02"
    )

    def fscan(scan_no, chip, point, minute):
        return request(
            "POST",
            f"{crp}/scans",
            {
                "scan_no": scan_no,
                "chip": chip,
                "point": point,
                "occurred_at": at(minute),
            },
        )

    # C1 on the original chip at minute 10 (out-of-order: it is registered
    # only after the replacement below, which is exactly the late backfill
    # case -- matching always uses the raw occurred_at).
    status, _ = request(
        "POST",
        f"{crp}/runners/{frank_id}/chip-replacements",
        {"new_chip": "CHIP-F2", "effective_at": at(15),
         "note": "cracked casing"},
    )
    expect(201, status, "register Frank CHIP-F -> CHIP-F2 at minute 15")

    # Timelines are queryable: initial chip, both half-open windows, events.
    status, body = request(
        "GET", f"{crp}/runners/{frank_id}/chip-timeline"
    )
    show(status, body)
    expect(200, status, "query Frank chip timeline")
    assert body["initial_chip"] == "CHIP-F"
    assert body["current_chip"] == "CHIP-F2"
    assert [p["chip"] for p in body["periods"]] == ["CHIP-F", "CHIP-F2"]
    assert instant(body["periods"][0]["valid_to"]) == instant(at(15))
    assert body["periods"][1]["valid_to"] is None
    assert body["replacements"][0]["old_chip"] == "CHIP-F"
    print("ok timeline shows [start,15)=CHIP-F, [15,inf)=CHIP-F2")

    # Late/out-of-order backfill on the OLD chip, strictly before 15: ok.
    status, _ = fscan("F-010", "CHIP-F", "C1", 10)
    expect(201, status, "backfilled old-chip scan before boundary accepted")

    # Old chip at/after the boundary is explicitly rejected; the new chip is
    # valid exactly at the boundary.
    status, body = fscan("F-BAD", "CHIP-F", "C2", 15)
    show(status, body)
    expect(409, status, "old-chip scan at the boundary rejected")
    assert body["error"] == "chip_outside_valid_period"

    status, _ = fscan("F-020", "CHIP-F2", "C2", 20)
    expect(201, status, "new-chip scan after boundary accepted")
    status, _ = fscan("F-030", "CHIP-F2", "FIN", 30)
    expect(201, status, "new-chip finish scan accepted")

    # Unknown chip stays a 404; scans outside the race window are rejected.
    status, body = fscan("F-X", "CHIP-NEVER", "C1", 20)
    expect(404, status, "unknown chip on scan rejected")
    status, body = fscan("F-PRE", "CHIP-F", "C1", -1)
    show(status, body)
    expect(409, status, "pre-start scan rejected before the start window")

    # Provisional result combines both chips and keeps the original chip on
    # each used scan.
    status, body = request("GET", f"{crp}/results")
    row = next(r for r in body["results"] if r["bib"] == "F01")
    assert row["status"] == "finished"
    assert [u["scan_no"] for u in row["used_scans"]] == [
        "F-010", "F-020", "F-030"
    ]
    assert [u["chip"] for u in row["used_scans"]] == [
        "CHIP-F", "CHIP-F2", "CHIP-F2"
    ]
    print("ok provisional result stitches CHIP-F then CHIP-F2, original "
          "chips preserved per scan")

    # A replacement that would invalidate a received scan is rejected as a
    # whole: Grace has an old-chip scan at minute 40 (even revoking it does
    # not help -- revoked scans still participate in this check).
    status, _ = fscan("G-040", "CHIP-G", "C2", 40)
    expect(201, status, "Grace old-chip C2 at minute 40")
    status, body = request(
        "POST",
        f"{crp}/runners/{grace_id}/chip-replacements",
        {"new_chip": "CHIP-G2", "effective_at": at(30)},
    )
    show(status, body)
    expect(409, status, "replacement conflicting with received scan rejected")
    assert body["error"] == "chip_replacement_conflict"
    status, _ = request("DELETE", f"{crp}/scans/G-040")
    expect(200, status, "revoke Grace's scan")
    status, body = request(
        "POST",
        f"{crp}/runners/{grace_id}/chip-replacements",
        {"new_chip": "CHIP-G2", "effective_at": at(30)},
    )
    expect(409, status, "revoked scan still blocks the same replacement")
    status, _ = request(
        "POST",
        f"{crp}/runners/{grace_id}/chip-replacements",
        {"new_chip": "CHIP-G2", "effective_at": at(41)},
    )
    expect(201, status, "replacement after every received scan accepted")

    # Illegal inputs: reused chip (another runner / prior chip), bad window,
    # non-increasing effective time, unknown runner.
    status, body = request(
        "POST",
        f"{crp}/runners/{frank_id}/chip-replacements",
        {"new_chip": "CHIP-F", "effective_at": at(40)},
    )
    expect(409, status, "reusing Frank's original chip rejected")
    assert body["error"] == "chip_reused"
    status, body = request(
        "POST",
        f"{crp}/runners/{frank_id}/chip-replacements",
        {"new_chip": "CHIP-G2", "effective_at": at(40)},
    )
    expect(409, status, "reusing Grace's replacement chip rejected")
    status, body = request(
        "POST",
        f"{crp}/runners/{frank_id}/chip-replacements",
        {"new_chip": "CHIP-F3", "effective_at": at(14)},
    )
    expect(422, status, "effective time not later than previous rejected")
    assert body["error"] == "invalid_effective_time"
    status, body = request(
        "POST",
        f"{crp}/runners/{frank_id}/chip-replacements",
        {"new_chip": "CHIP-F3", "effective_at": at(61)},
    )
    expect(422, status, "effective time after cutoff rejected")
    status, body = request(
        "GET",
        f"{crp}/runners/00000000-0000-0000-0000-000000000000/chip-timeline",
    )
    expect(404, status, "unknown runner timeline 404")

    # Sealing freezes the timeline policy: no replacements afterwards, and
    # the snapshot is unaffected.
    status, _ = request("POST", f"{crp}/seal")
    expect(200, status, "chip-replacement race sealed")
    status, body = request(
        "POST",
        f"{crp}/runners/{frank_id}/chip-replacements",
        {"new_chip": "CHIP-F9", "effective_at": at(45)},
    )
    show(status, body)
    expect(409, status, "replacement after sealing rejected")
    status, body = request(
        "GET", f"{crp}/runners/{frank_id}/chip-timeline"
    )
    expect(200, status, "timeline still readable after sealing")

    print("\nALL DEMO STEPS PASSED")


if __name__ == "__main__":
    main()
