#!/bin/bash
# Real turn: does the unverified-edit nudge fire when the model edits and stops?
set -u
API=http://127.0.0.1:8765
TOK=$(grep '^XUMBRA_API_TOKEN=' /home/pi/MaiconErick/Umbra/.env | cut -d= -f2-)
AUTH="Authorization: Bearer $TOK"
printf 'alpha beta\n' > /tmp/zz_nudge.txt

SID=$(curl -s -X POST $API/api/v2/sessions -H "$AUTH" -H 'Content-Type: application/json' \
        -d '{"summary":"zz nudge"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')
echo "sid=$SID"
curl -s -X POST $API/api/v2/sessions/$SID/send -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"Use edit_file to change the word alpha to omega in /tmp/zz_nudge.txt. Do not run any build or test. Just tell me what you did."}' ; echo

for i in $(seq 1 40); do
  sleep 3
  LAST=$(curl -s -m 3 -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d["messages"][-1] if d["messages"] else {}
print(r.get("role"), "|", (r.get("text") or "")[:200])
' 2>/dev/null) || continue
  case "$LAST" in assistant*) echo "answer: $LAST"; break;; esac
done

echo "--- file:"; cat /tmp/zz_nudge.txt
echo "--- nudge fired?"
grep -o 'Unverified edit[^"]*' ~/MaiconErick/Umbra/sessions/events/$SID.jsonl 2>/dev/null | head -2
grep -c 'You changed code in this turn' ~/MaiconErick/Umbra/sessions/events/$SID.jsonl 2>/dev/null
curl -s -X DELETE "$API/api/v2/sessions/$SID?wipe=true" -H "$AUTH" > /dev/null
rm -f /tmp/zz_nudge.txt
