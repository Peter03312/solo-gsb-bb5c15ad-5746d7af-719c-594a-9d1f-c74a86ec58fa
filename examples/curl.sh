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
