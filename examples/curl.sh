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

# ---------------------------------------------------------------------------
# Mid-race chip replacement
# ---------------------------------------------------------------------------
# A fresh race keeps the window arithmetic readable: start T0, cutoff T0+1h,
# one point FIN; runner 2 binds CHIP-R. Create it and capture the runner id.
R2_JSON=$(curl -fsS -X POST "$BASE_URL/races" \
  -H 'Content-Type: application/json' \
  -d "{
    \"name\": \"chip replacement curl demo\",
    \"start_time\": \"$T0\",
    \"cutoff_time\": \"$(AT 3600)\",
    \"points\": [{\"code\": \"FIN\", \"name\": \"Finish\", \"is_finish\": true}],
    \"segments\": [{\"min_seconds\": 0, \"max_seconds\": 3600}],
    \"runners\": [{\"bib\": \"2\", \"name\": \"Ruth\", \"chip\": \"CHIP-R\"}]
  }")
R2_ID=$(echo "$R2_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
R2_RUNNER=$(echo "$R2_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin)["runners"][0]["id"])')

echo "## chip timeline before any replacement (one unbounded window)"
curl -fsS "$BASE_URL/races/$R2_ID/runners/$R2_RUNNER/chip-timeline"; echo

echo "## register a replacement CHIP-R2 effective at T0+20min (201)"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/chip-replacements" \
  -H 'Content-Type: application/json' \
  -d "{\"runner_id\": \"$R2_RUNNER\", \"new_chip\": \"CHIP-R2\", \"effective_at\": \"$(AT 1200)\"}"

echo "## reused chip is refused (HTTP 409 chip_already_used)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/chip-replacements" \
  -H 'Content-Type: application/json' \
  -d "{\"runner_id\": \"$R2_RUNNER\", \"new_chip\": \"CHIP-R\", \"effective_at\": \"$(AT 1500)\"}"

echo "## effective time outside [start, cutoff] is refused (HTTP 422)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/chip-replacements" \
  -H 'Content-Type: application/json' \
  -d "{\"runner_id\": \"$R2_RUNNER\", \"new_chip\": \"CHIP-RX\", \"effective_at\": \"$(AT 3601)\"}"

echo "## old-chip scan at/after the effective instant is rejected (HTTP 409)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"R-OLD20\",\"chip\":\"CHIP-R\",\"point\":\"FIN\",\"occurred_at\":\"$(AT 1200)\"}"

echo "## new-chip scan at the effective instant is accepted (HTTP 201)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"R-NEW20\",\"chip\":\"CHIP-R2\",\"point\":\"FIN\",\"occurred_at\":\"$(AT 1200)\"}"

echo "## out-of-order late upload at an old raw instant uses old windows (HTTP 201)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"R-OLD10\",\"chip\":\"CHIP-R\",\"point\":\"FIN\",\"occurred_at\":\"$(AT 600)\"}"

echo "## one more new-chip scan at T0+30min (HTTP 201)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/scans" \
  -H 'Content-Type: application/json' \
  -d "{\"scan_no\":\"R-NEW30\",\"chip\":\"CHIP-R2\",\"point\":\"FIN\",\"occurred_at\":\"$(AT 1800)\"}"

echo "## 2nd replacement at T0+25min would invalidate R-NEW30 (HTTP 409 + conflicts)"
curl -sS -w '\nHTTP %{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/chip-replacements" \
  -H 'Content-Type: application/json' \
  -d "{\"runner_id\": \"$R2_RUNNER\", \"new_chip\": \"CHIP-R3\", \"effective_at\": \"$(AT 1500)\"}"

echo "## seal (snapshot frozen; later submit/revoke/offset/replacement get 409)"
curl -fsS -X POST "$BASE_URL/races/$RACE_ID/seal"; echo
curl -fsS -X POST "$BASE_URL/races/$R2_ID/seal" >/dev/null; echo "race 2 sealed"

echo "## chip replacement after sealing (HTTP 409)"
curl -sS -o /dev/null -w '%{http_code}\n' -X POST "$BASE_URL/races/$R2_ID/chip-replacements" \
  -H 'Content-Type: application/json' \
  -d "{\"runner_id\": \"$R2_RUNNER\", \"new_chip\": \"CHIP-AFTER\", \"effective_at\": \"$(AT 1800)\"}"

echo "## chip timeline stays readable after sealing"
curl -fsS "$BASE_URL/races/$R2_ID/runners/$R2_RUNNER/chip-timeline"; echo
