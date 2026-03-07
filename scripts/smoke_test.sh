#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
FAIL=0
TOTAL=0

query() {
  local msg="$1"
  TOTAL=$((TOTAL + 1))
  local resp
  resp=$(curl -sf -X POST "$BASE_URL/chat" \
    -H 'Content-Type: application/json' \
    -d "{\"message\":\"$msg\"}" 2>/dev/null) || { echo "FAIL [$msg] — no response"; FAIL=$((FAIL + 1)); return; }
  local content
  content=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('content',''))" 2>/dev/null)
  if [ -z "$content" ] || echo "$content" | grep -qi "data is ready"; then
    echo "FAIL [$msg] — ${content:0:80}"
    FAIL=$((FAIL + 1))
  else
    echo "PASS [$msg] — ${content:0:100}"
  fi
}

# Check server is up
if ! curl -sf "$BASE_URL/chat" -X POST -H 'Content-Type: application/json' -d '{"message":"ping"}' >/dev/null 2>&1; then
  echo "ERROR: Server not responding at $BASE_URL"
  exit 1
fi

echo "=== Single-turn queries ==="
query "hello"
query "show my team"
query "league summary"
query "waiver recs"
query "who won gw27"
query "ownership scarcity"
query "strength of schedule"
query "league entries"

echo ""
echo "=== Multi-turn session test ==="
SID="smoke-$(date +%s)"
for msg in "standings for gw3" "show standings" "league summary"; do
  TOTAL=$((TOTAL + 1))
  resp=$(curl -sf -X POST "$BASE_URL/chat" \
    -H 'Content-Type: application/json' \
    -d "{\"message\":\"$msg\",\"session_id\":\"$SID\"}" 2>/dev/null) || { echo "FAIL [$msg] — no response"; FAIL=$((FAIL + 1)); continue; }
  content=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('content',''))" 2>/dev/null)
  if [ -z "$content" ]; then
    echo "FAIL [$msg] — empty content"
    FAIL=$((FAIL + 1))
  else
    echo "PASS [$msg] — ${content:0:100}"
  fi
done

echo ""
echo "=== Results: $((TOTAL - FAIL))/$TOTAL passed ==="
[ "$FAIL" -eq 0 ] && echo "ALL PASSED" || echo "$FAIL FAILED"
exit "$FAIL"
