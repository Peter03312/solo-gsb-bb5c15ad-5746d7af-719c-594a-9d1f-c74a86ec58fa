"""End-to-end HTTP integration for the chip-replacement feature.

Runs against SQLite via tests/sqlite_shim (production uses PostgreSQL).
Run from repo root:  python3 tests/integration_chip.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

# Fresh DB file per run (must be removed BEFORE the engine module loads).
db_file = os.path.abspath("test_race.db")
if os.path.exists(db_file):
    os.remove(db_file)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sqlite_shim  # noqa: E402,F401  (must precede app imports)

os.environ["DATABASE_URL"] = f"sqlite+pysqlite:///{db_file}"

from fastapi.testclient import TestClient  # noqa: E402

from app.database import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)

T0 = datetime(2026, 10, 1, 8, 0, 0, tzinfo=timezone.utc)
CUTOFF = T0 + timedelta(hours=1)
failures: list[str] = []


def at(minute: float) -> str:
    return (T0 + timedelta(seconds=minute * 60)).isoformat()


def check(label: str, cond, *, extra=None):
    mark = "ok  " if cond else "FAIL"
    print(f"{mark} {label}")
    if not cond:
        failures.append(label)
        if extra is not None:
            print("     ->", extra)


def method(m: str, path: str, body=None, expected=None):
    resp = client.request(m, path, json=body)
    if expected is not None:
        check(f"{m} {path} -> {expected} (got {resp.status_code})",
              resp.status_code == expected, extra=resp.text)
    return resp


# ---------------------------------------------------------------- race ----
resp = method("POST", "/races", {
    "name": "Chip replacement IT",
    "start_time": at(0),
    "cutoff_time": at(60),
    "points": [
        {"code": "C1", "name": "CP1"},
        {"code": "C2", "name": "CP2"},
        {"code": "FIN", "name": "Finish", "is_finish": True},
    ],
    "segments": [
        {"min_seconds": 0, "max_seconds": 3600},
        {"min_seconds": 0, "max_seconds": 3600},
        {"min_seconds": 0, "max_seconds": 3600},
    ],
    "runners": [
        {"bib": "F01", "name": "Frank", "chip": "F-OLD"},
        {"bib": "G02", "name": "Grace", "chip": "G-OLD"},
    ],
}, expected=201)
race = resp.json()
rp = f"/races/{race['id']}"
rid = race["id"]
frank = next(r for r in race["runners"] if r["bib"] == "F01")
grace = next(r for r in race["runners"] if r["bib"] == "G02")
check("race output carries current_chip == chip initially",
      frank["current_chip"] == "F-OLD" and grace["current_chip"] == "G-OLD")

# ---------------------------------------------------- unknown identifiers --
method("POST", f"/races/{'0'*8}-0000-0000-0000-{'0'*12}/chip-replacements",
       {"runner_id": frank["id"], "new_chip": "X", "effective_at": at(10)},
       expected=404)
method("POST", f"{rp}/chip-replacements",
       {"runner_id": "not-a-uuid", "new_chip": "X", "effective_at": at(10)},
       expected=404)
method("GET", f"{rp}/runners/not-a-uuid/chip-timeline", expected=404)
method("GET",
       f"{rp}/runners/{'0'*8}-0000-0000-0000-{'0'*12}/chip-timeline",
       expected=404)

# ------------------------------------------------------------ timeline 0 ---
resp = method("GET", f"{rp}/runners/{frank['id']}/chip-timeline", expected=200)
tl0 = resp.json()
check("initial timeline: single unbounded period, original=current",
      tl0["replacement_count"] == 0
      and tl0["original_chip"] == "F-OLD"
      and tl0["current_chip"] == "F-OLD"
      and len(tl0["periods"]) == 1
      and tl0["periods"][0] == {
          "sequence": 0, "chip": "F-OLD", "is_initial": True,
          "effective_from": None, "effective_to": None,
          "is_current": True, "registered_at": None,
      },
      extra=tl0)

# ---------------------------------------------------------- invalid time ---
method("POST", f"{rp}/chip-replacements",
       {"runner_id": frank["id"], "new_chip": "F-BAD1",
        "effective_at": at(-0.0001)}, expected=422)
method("POST", f"{rp}/chip-replacements",
       {"runner_id": frank["id"], "new_chip": "F-BAD2",
        "effective_at": at(60.0001)}, expected=422)
# exactly at the window edges is legal (checked later with a real chip);
# here only the illegal-side validation matters.

# ------------------------------------------------------------- used chip --
method("POST", f"{rp}/chip-replacements",
       {"runner_id": frank["id"], "new_chip": "G-OLD", "effective_at": at(10)},
       expected=409)
r = method("POST", f"{rp}/chip-replacements",
           {"runner_id": frank["id"], "new_chip": "F-OLD",
            "effective_at": at(10)}, expected=409)
check("reusing own original chip -> chip_already_used",
      r.json()["error"] == "chip_already_used", extra=r.json())

# --------------------------------------- scans before the replacement ----
# Frank on the old chip: C1 at 10, C2 at 12, FIN at 20 (revoked later).
method("POST", f"{rp}/scans",
       {"scan_no": "F-C1", "chip": "F-OLD", "point": "C1",
        "occurred_at": at(10)}, expected=201)
method("POST", f"{rp}/scans",
       {"scan_no": "F-C2", "chip": "F-OLD", "point": "C2",
        "occurred_at": at(12)}, expected=201)
r = method("POST", f"{rp}/scans",
           {"scan_no": "F-FINOLD", "chip": "F-OLD", "point": "FIN",
            "occurred_at": at(20)}, expected=201)
check("new scan stored submitted chip", r.json()["chip"] == "F-OLD")

# revocation still works (and revoked scans join the conflict check later)
method("DELETE", f"{rp}/scans/F-FINOLD", expected=200)

# ------------------------------------------------ register replacement ----
# F-NEW at minute 15. The revoked FIN scan at minute 20 uses the old chip and
# lands at/after 15 -> whole-request conflict, including the revoked row.
r = method("POST", f"{rp}/chip-replacements",
           {"runner_id": frank["id"], "new_chip": "F-NEW",
            "effective_at": at(15)}, expected=409)
body = r.json()
check("conflict error code + lists accepted scan incl. revoked",
      body["error"] == "chip_replacement_conflict"
      and {c["scan_no"] for c in body["conflicts"]} == {"F-FINOLD"}
      and body["conflicts"][0]["revoked"] is True,
      extra=body)
check("nothing written after rejected replacement (count stays 0)",
      client.get(f"{rp}/runners/{frank['id']}/chip-timeline")
      .json()["replacement_count"] == 0)

# move the effective instant strictly after the offending raw timestamp
r = method("POST", f"{rp}/chip-replacements",
           {"runner_id": frank["id"], "new_chip": "F-NEW",
            "effective_at": at(22)}, expected=201)
tl1 = r.json()


def iso(ts):
    # Normalise the JSON serialisation (pydantic renders UTC as trailing Z).
    return None if ts is None else datetime.fromisoformat(
        ts.replace("Z", "+00:00")
    ).isoformat()


check("accepted replacement timeline",
      tl1["replacement_count"] == 1
      and tl1["current_chip"] == "F-NEW"
      and [(p["chip"], p["is_initial"], p["is_current"]) for p in tl1["periods"]]
      == [("F-OLD", True, False), ("F-NEW", False, True)]
      and iso(tl1["periods"][0]["effective_to"]) == at(22)
      and iso(tl1["periods"][1]["effective_from"]) == at(22)
      and tl1["periods"][1]["effective_to"] is None
      and tl1["periods"][0]["registered_at"] is None
      and tl1["periods"][1]["registered_at"] is not None,
      extra=tl1)

# --------------------------- scans around the half-open boundary ----------
# old chip at exactly 22 -> rejected (new chip owns the instant)
r = method("POST", f"{rp}/scans",
           {"scan_no": "F-OLD-AT22", "chip": "F-OLD", "point": "FIN",
            "occurred_at": at(22)}, expected=409)
check("old chip at effective instant rejected explicitly",
      r.json()["error"] == "scan_outside_chip_period", extra=r.json())

# unknown chip -> 404 keeps precedence over the period rule
method("POST", f"{rp}/scans",
       {"scan_no": "F-UK", "chip": "NO-SUCH", "point": "FIN",
        "occurred_at": at(23)}, expected=404)

# new chip at 22 accepted
r = method("POST", f"{rp}/scans",
           {"scan_no": "F-FINNEW", "chip": "F-NEW", "point": "FIN",
            "occurred_at": at(22)}, expected=201)
check("new chip at the effective instant accepted",
      r.json()["chip"] == "F-NEW")

# out-of-order late retransmission of an OLD scan after replacement:
# raw minute 12 -> old chip valid then -> rejected, and the active chip hint
# names the old chip.
r = method("POST", f"{rp}/scans",
           {"scan_no": "F-LATE1", "chip": "F-NEW", "point": "C2",
            "occurred_at": at(12)}, expected=409)
check("out-of-order new-chip scan at an old instant rejected; active=old",
      r.json()["error"] == "scan_outside_chip_period"
      and "F-OLD" in r.json()["detail"], extra=r.json())

# an out-of-order scan of the old chip at an earlier instant is accepted
r = method("POST", f"{rp}/scans",
           {"scan_no": "F-LATE2", "chip": "F-OLD", "point": "C2",
            "occurred_at": at(11)}, expected=201)
check("late old-chip scan inside old window accepted",
      r.status_code == 201)

# replay/conflict conventions unchanged for an already-accepted scan_no:
# identical content -> 200 replay even though minute-12 is before replacement
r = method("POST", f"{rp}/scans",
           {"scan_no": "F-C2", "chip": "F-OLD", "point": "C2",
            "occurred_at": at(12)}, expected=200)
check("identical retransmission is replay",
      r.json()["replayed"] is True and r.json()["chip"] == "F-OLD")
# different content -> 409 scan_conflict (not the period error)
r = method("POST", f"{rp}/scans",
           {"scan_no": "F-C2", "chip": "F-OLD", "point": "C2",
            "occurred_at": at(13)}, expected=409)
check("different retransmission is scan_conflict",
      r.json()["error"] == "scan_conflict", extra=r.json())

# batch endpoint applies the same rules
r = method("POST", f"{rp}/scans/batch", {"scans": [
    {"scan_no": "B1", "chip": "F-NEW", "point": "C1", "occurred_at": at(23)},
    {"scan_no": "B2", "chip": "F-OLD", "point": "C1", "occurred_at": at(23)},
    {"scan_no": "B3", "chip": "NOPE", "point": "C1", "occurred_at": at(23)},
    {"scan_no": "F-C1", "chip": "F-OLD", "point": "C1", "occurred_at": at(10)},
]}, expected=200)
codes = [i["status_code"] for i in r.json()]
check("batch: accepted / period reject / unknown chip / replay",
      codes == [201, 409, 404, 200], extra=r.json())
check("batch period reject error code",
      r.json()[1]["error"] == "scan_outside_chip_period")

# ------------------------------------------- second replacement -----------
# must be strictly later than minute 22
r = method("POST", f"{rp}/chip-replacements",
           {"runner_id": frank["id"], "new_chip": "F-NEW2",
            "effective_at": at(22)}, expected=422)
check("equal effective time -> invalid_effective_time",
      r.json()["error"] == "invalid_effective_time", extra=r.json())

# B1 at minute 23 on F-NEW blocks a replacement at 23
r = method("POST", f"{rp}/chip-replacements",
           {"runner_id": frank["id"], "new_chip": "F-NEW2",
            "effective_at": at(23)}, expected=409)
check("second replacement conflicts with new-chip scan B1",
      r.json()["error"] == "chip_replacement_conflict"
      and {c["scan_no"] for c in r.json()["conflicts"]} == {"B1"},
      extra=r.json())

method("POST", f"{rp}/scans",
       {"scan_no": "F-C2N", "chip": "F-NEW", "point": "C2",
        "occurred_at": at(23)}, expected=201)
r = method("POST", f"{rp}/chip-replacements",
           {"runner_id": frank["id"], "new_chip": "F-NEW2",
            "effective_at": at(30)}, expected=201)
tl2 = r.json()
check("second replacement timeline contiguous",
      [p["chip"] for p in tl2["periods"]] == ["F-OLD", "F-NEW", "F-NEW2"]
      and iso(tl2["periods"][1]["effective_from"]) == at(22)
      and iso(tl2["periods"][1]["effective_to"]) == at(30)
      and iso(tl2["periods"][2]["effective_from"]) == at(30)
      and tl2["replacement_count"] == 2
      and tl2["current_chip"] == "F-NEW2", extra=tl2)

# F-NEW at minute 30 is now out of window
r = method("POST", f"{rp}/scans",
           {"scan_no": "F-N1-AT30", "chip": "F-NEW", "point": "FIN",
            "occurred_at": at(30)}, expected=409)
check("middle chip scan at next boundary rejected",
      r.json()["error"] == "scan_outside_chip_period")

# ---------------------------------------------------- results recompute ----
# Frank's active scans: old C1@10, old C2@11/12, new FIN@22, new C2@23 ...
# plus F-NEW2 has no scans. Best complete path uses C1@10 (F-OLD), a C2 after
# it with an active chip, and FIN@22. C2@12 (F-OLD) -> FIN@22 (F-NEW) is a
# legal cross-chip path; used_scans must echo each scan's raw chip.
resp = method("GET", f"{rp}/results", expected=200)
row = next(x for x in resp.json()["results"] if x["bib"] == "F01")
used = [(u["point"], u["chip"], u["scan_no"]) for u in row["used_scans"]]
check("result finished crossing chips with raw chips preserved",
      row["status"] == "finished"
      and used == [("C1", "F-OLD", "F-C1"),
                   ("C2", "F-OLD", "F-C2"),
                   ("FIN", "F-NEW", "F-FINNEW")]
      and row["current_chip"] == "F-NEW2",
      extra=row)
# Grace untouched
grow = next(x for x in resp.json()["results"] if x["bib"] == "G02")
check("other runner unaffected (DNF, original chip)",
      grow["status"] == "did_not_finish"
      and grow["chip"] == "G-OLD" and grow["current_chip"] == "G-OLD")

# --------------------------------------------------------------- sealing --
r = client.post(f"{rp}/seal")
check("seal -> 200", r.status_code == 200, extra=r.text)
snap = next(x for x in r.json()["results"] if x["bib"] == "F01")
check("snapshot keeps raw chips in used_scans",
      [u["chip"] for u in snap["used_scans"]] == ["F-OLD", "F-OLD", "F-NEW"])
# after sealing: replacement rejected, timeline still readable
method("POST", f"{rp}/chip-replacements",
       {"runner_id": frank["id"], "new_chip": "F-AFTER",
        "effective_at": at(40)}, expected=409)
method("GET", f"{rp}/runners/{frank['id']}/chip-timeline", expected=200)
# snapshot invariant after the rejected request
r2 = client.get(f"{rp}/results")
check("snapshot unchanged after rejected post-seal replacement",
      r2.json()["results"] == r.json()["results"])

# --------------------------------------- a clean second runner flow --------
resp = method("POST", "/races", {
    "name": "Grace race",
    "start_time": at(0), "cutoff_time": at(60),
    "points": [{"code": "FIN", "name": "Finish", "is_finish": True}],
    "segments": [{"min_seconds": 0, "max_seconds": 3600}],
    "runners": [{"bib": "H1", "name": "Hank", "chip": "H-OLD"}],
}, expected=201)
r2id = resp.json()["id"]; h = resp.json()["runners"][0]
r2p = f"/races/{r2id}"
# boundary-legal effective times: exactly start and exactly cutoff
method("POST", f"{r2p}/chip-replacements",
       {"runner_id": h["id"], "new_chip": "H-MID",
        "effective_at": at(0)}, expected=201)
# a chip already used in race #1 CAN be bound in a different race
method("POST", f"{r2p}/chip-replacements",
       {"runner_id": h["id"], "new_chip": "F-OLD",
        "effective_at": at(30)}, expected=201)
method("POST", f"{r2p}/chip-replacements",
       {"runner_id": h["id"], "new_chip": "H-END",
        "effective_at": at(60)}, expected=201)
# old chip before start (minute -1) accepted; H-MID at 0:00 accepted
method("POST", f"{r2p}/scans",
       {"scan_no": "H-PRE", "chip": "H-OLD", "point": "FIN",
        "occurred_at": at(-1)}, expected=201)
method("POST", f"{r2p}/scans",
       {"scan_no": "H-START", "chip": "H-MID", "point": "FIN",
        "occurred_at": at(0)}, expected=201)
# F-OLD at exactly cutoff -> rejected (H-END owns the boundary)
r = method("POST", f"{r2p}/scans",
           {"scan_no": "H-MID-CUT", "chip": "F-OLD", "point": "FIN",
            "occurred_at": at(60)}, expected=409)
check("cutoff boundary belongs to the last chip",
      "H-END" in r.json()["detail"], extra=r.json())
method("POST", f"{r2p}/scans",
       {"scan_no": "H-FIN", "chip": "H-END", "point": "FIN",
        "occurred_at": at(60)}, expected=201)

# within the same race every already-used chip is blocked (sequence issue is
# moot: reuse -> 409 regardless of effective time)
for chip in ("H-OLD", "H-MID", "H-END", "F-OLD"):
    r = method("POST", f"{r2p}/chip-replacements",
               {"runner_id": h["id"], "new_chip": chip,
                "effective_at": at(59)}, expected=409)
    check(f"reuse blocked in same race: {chip}",
          r.json()["error"] == "chip_already_used")

print()
if failures:
    print(f"{len(failures)} FAILURES:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("ALL INTEGRATION CHECKS PASSED")
