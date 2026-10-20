#!/bin/bash
# Real turn: does Umbra use the code map to locate something?
set -u
API=http://127.0.0.1:8765
TOK=$(grep '^XUMBRA_API_TOKEN=' /home/pi/MaiconErick/Umbra/.env | cut -d= -f2-)
AUTH="Authorization: Bearer $TOK"
SID=$(curl -s -X POST $API/api/v2/sessions -H "$AUTH" -H 'Content-Type: application/json' \
        -d '{"summary":"zz code map"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')
echo "sid=$SID"
curl -s -X POST $API/api/v2/sessions/$SID/send -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"In the project /home/pi/MaiconErick/xUmbra, which file holds MatrixStream and which functions does it expose? One short answer, names only."}' ; echo

for i in $(seq 1 40); do
  sleep 3
  LAST=$(curl -s -m 3 -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d["messages"][-1] if d["messages"] else {}
print(r.get("role"), "|", (r.get("text") or "")[:220])
' 2>/dev/null) || continue
  case "$LAST" in assistant*) echo "answer: $LAST"; break;; esac
done

echo "--- tools used:"
curl -s -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for m in d["messages"]:
    for t in ((m.get("turn") or {}).get("tools") or []):
        print(" ", t.get("name"), "|", str(t.get("args"))[:80], "|", str(t.get("result"))[:100].replace("\n"," "))
'
curl -s -X DELETE "$API/api/v2/sessions/$SID?wipe=true" -H "$AUTH" > /dev/null
