#!/bin/bash
# Real turn: does the task tool run a sub-agent and keep only its report?
set -u
API=http://127.0.0.1:8765
TOK=$(grep '^XUMBRA_API_TOKEN=' /home/pi/MaiconErick/Umbra/.env | cut -d= -f2-)
AUTH="Authorization: Bearer $TOK"
BEFORE=$(ls ~/MaiconErick/Umbra/sessions/msgs/*.jsonl 2>/dev/null | wc -l)

SID=$(curl -s -X POST $API/api/v2/sessions -H "$AUTH" -H 'Content-Type: application/json' \
        -d '{"summary":"zz subagent"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')
echo "sid=$SID"
curl -s -X POST $API/api/v2/sessions/$SID/send -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"Use the task tool with a sub-agent to find in /home/pi/MaiconErick/xUmbra where MatrixStream is defined and which composeables that file exposes. Give me back only its report, short."}' ; echo

for i in $(seq 1 60); do
  sleep 3
  LAST=$(curl -s -m 3 -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d["messages"][-1] if d["messages"] else {}
print(r.get("role"), "|", (r.get("text") or "")[:300])
' 2>/dev/null) || continue
  case "$LAST" in assistant*) echo "answer: $LAST"; break;; esac
done

echo "--- tools used:"
curl -s -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for m in d["messages"]:
    for t in ((m.get("turn") or {}).get("tools") or []):
        print(" ", t.get("name"), "|", str(t.get("args"))[:70], "|", str(t.get("result"))[:120].replace("\n"," "))
'
AFTER=$(ls ~/MaiconErick/Umbra/sessions/msgs/*.jsonl 2>/dev/null | wc -l)
echo "--- sub-sessions cleaned up: before=$BEFORE after=$AFTER"
curl -s -X DELETE "$API/api/v2/sessions/$SID?wipe=true" -H "$AUTH" > /dev/null
