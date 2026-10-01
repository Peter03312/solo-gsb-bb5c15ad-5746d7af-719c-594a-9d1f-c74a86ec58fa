#!/usr/bin/env bash
# Minimal curl walkthrough of the split timing API.
# Requires: curl and python3 (python3 is only used to read the race id from
# the JSON response). Start the service first: docker compose up --build
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"
T0="2026-10-01T08:00:00+00:00"
AT() { python3 -c "
import datetime,sys
t=datetime.datetime.fromisoformat('$T0')+datetime.timedelta(seconds=$1)
print(t.isoformat())
"; }

echo "## health"
curl -fsS "$BASE_URL/health"; echo

echo "## create race"
RACE_JSON=$(curl -fsS -X POST "$BASE_URL/races" \
  -H 'Content-Type: application/json' \
  -d "{
    \"name\": \"curl demo\",
    \"start_time\": \"$T0\",
    \"cutoff_time\": \"$(AT 3600)\",
    \"points\": [
      {\"code\": \"C1\", \"name\": \"Checkpoint 1\"},
      {\"code\": \"FIN\", \"name\": \"Finish\", \"is_finish\": true}
    ],
    \"segments\": [
      {\"min_seconds\": 300, \"max_seconds\": 1800},
      {\"min_seconds\": 300, \"max_seconds\": 1800}
    ],
    \"runners\": [
      {\"bib\": \"1\", \"name\": \"Alice\", \"chip\": \"CHIP-A\"}
    ]
  }")
echo "$RACE_JSON"
RACE_ID=$(echo "$RACE_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')

echo "## submit scans out of order (FIN first)"
curl -fsS -X POST "$BASE_URL/races/$RACE_ID/scans" -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"S2\",\"chip\":\"CHIP-A\",\"point\":\"FIN\",\"occurred_at\":\"$(AT 1800)\"}"; echo
curl -fsS -X POST "$BASE_URL/races/$RACE_ID/scans" -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"S1\",\"chip\":\"CHIP-A\",\"point\":\"C1\",\"occurred_at\":\"$(AT 600)\"}"; echo

echo "## idempotent replay (HTTP 200, replayed=true)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$RACE_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"S1\",\"chip\":\"CHIP-A\",\"point\":\"C1\",\"occurred_at\":\"$(AT 600)\"}"

echo "## conflicting replay (HTTP 409)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$RACE_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"S1\",\"chip\":\"CHIP-A\",\"point\":\"C1\",\"occurred_at\":\"$(AT 601)\"}"

echo "## provisional results"
curl -fsS "$BASE_URL/races/$RACE_ID/results"; echo

echo "## clock offsets: all points read zero before configuration"
curl -fsS "$BASE_URL/races/$RACE_ID/offsets"; echo

echo "## set C1 device offset to +5 signed integer seconds (200, replaced=false)"
curl -fsS -X PUT "$BASE_URL/races/$RACE_ID/points/C1/offset" \
  -H 'Content-Type: application/json' -d '{"offset_seconds": 5}'; echo

echo "## replace it with -3 (200, replaced=true); provisional results recompute"
curl -fsS -X PUT "$BASE_URL/races/$RACE_ID/points/C1/offset" \
  -H 'Content-Type: application/json' -d '{"offset_seconds": -3}'; echo

echo "## non-integer offset is refused (HTTP 422)"
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT \
  "$BASE_URL/races/$RACE_ID/points/C1/offset" \
  -H 'Content-Type: application/json' -d '{"offset_seconds": 1.5}'

echo "## results now carry occurred_at plus calibrated_at/offset_seconds"
curl -fsS "$BASE_URL/races/$RACE_ID/results"; echo

echo "## revoke a scan"
curl -fsS -X DELETE "$BASE_URL/races/$RACE_ID/scans/S2"; echo

echo "## seal (snapshot frozen; later submit/revoke/offset change get 409)"
curl -fsS -X POST "$BASE_URL/races/$RACE_ID/seal"; echo

echo "## offset change after sealing (HTTP 409)"
curl -sS -o /dev/null -w '%{http_code}\n' -X PUT \
  "$BASE_URL/races/$RACE_ID/points/C1/offset" \
  -H 'Content-Type: application/json' -d '{"offset_seconds": 0}'

# ---------------------------------------------------------------------------
# Mid-race chip replacement (second race, keeps the walkthrough independent)
# ---------------------------------------------------------------------------
echo "## chip replacement: create a race with runner 1 on CHIP-A"
CR=$(curl -fsS -X POST "$BASE_URL/races" \
  -H 'Content-Type: application/json' \
  -d "{
    \"name\": \"chip replacement curl demo\",
    \"start_time\": \"$T0\",
    \"cutoff_time\": \"$(AT 3600)\",
    \"points\": [
      {\"code\": \"C1\", \"name\": \"Checkpoint 1\"},
      {\"code\": \"FIN\", \"name\": \"Finish\", \"is_finish\": true}
    ],
    \"segments\": [
      {\"min_seconds\": 300, \"max_seconds\": 3600},
      {\"min_seconds\": 300, \"max_seconds\": 3600}
    ],
    \"runners\": [
      {\"bib\": \"1\", \"name\": \"Alice\", \"chip\": \"CHIP-A\"}
    ]
  }")
echo "$CR"
CHIP_RACE_ID=$(echo "$CR" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
RUNNER_ID=$(echo "$CR" | python3 -c 'import json,sys;print(json.load(sys.stdin)["runners"][0]["id"])')

echo "## register CHIP-A -> CHIP-A2 effective at T0+900s (HTTP 201)"
curl -fsS -X POST \
  "$BASE_URL/races/$CHIP_RACE_ID/runners/$RUNNER_ID/chip-replacements" \
  -H 'Content-Type: application/json' \
  -d "{\"new_chip\": \"CHIP-A2\", \"effective_at\": \"$(AT 900)\", \"note\": \"broken chip\"}"
echo

echo "## query the chip timeline (periods + replacement events)"
curl -fsS "$BASE_URL/races/$CHIP_RACE_ID/runners/$RUNNER_ID/chip-timeline"; echo

echo "## old chip before the boundary (T0+800s) is still valid -> 201"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$CHIP_RACE_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"OLD-OK\",\"chip\":\"CHIP-A\",\"point\":\"C1\",\"occurred_at\":\"$(AT 800)\"}"

echo "## old chip at/after the boundary is explicitly rejected (HTTP 409)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$CHIP_RACE_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"OLD-BAD\",\"chip\":\"CHIP-A\",\"point\":\"C1\",\"occurred_at\":\"$(AT 900)\"}"

echo "## new chip at the boundary is valid -> 201"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$CHIP_RACE_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"NEW-OK\",\"chip\":\"CHIP-A2\",\"point\":\"FIN\",\"occurred_at\":\"$(AT 1800)\"}"

echo "## reusing an already-used chip number is rejected (HTTP 409 chip_reused)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  "$BASE_URL/races/$CHIP_RACE_ID/runners/$RUNNER_ID/chip-replacements" \
  -H 'Content-Type: application/json' \
  -d "{\"new_chip\": \"CHIP-A\", \"effective_at\": \"$(AT 1200)\"}"

echo "## effective time outside the start..cutoff window is rejected (HTTP 422)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST \
  "$BASE_URL/races/$CHIP_RACE_ID/runners/$RUNNER_ID/chip-replacements" \
  -H 'Content-Type: application/json' \
  -d "{\"new_chip\": \"CHIP-A3\", \"effective_at\": \"$(AT 4000)\"}"
