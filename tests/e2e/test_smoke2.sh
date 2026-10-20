#!/bin/bash
# Post-change smoke test: auth + one real turn through the new loops.
set -u
API=http://127.0.0.1:8765
TOK=$(grep '^XUMBRA_API_TOKEN=' /home/pi/MaiconErick/Umbra/.env | cut -d= -f2-)
AUTH="Authorization: Bearer $TOK"
SID=$(curl -s -X POST $API/api/v2/sessions -H "$AUTH" -H 'Content-Type: application/json' \
        -d '{"summary":"zz smoke2"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')
echo "sid=$SID"
curl -s -X POST $API/api/v2/sessions/$SID/send -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"Run the shell command `echo second-round-ok` and reply with only its output."}' ; echo
for i in $(seq 1 30); do
  sleep 3
  LAST=$(curl -s -m 3 -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d["messages"][-1] if d["messages"] else {}
print(r.get("role"), "|", (r.get("text") or "")[:60])
' 2>/dev/null) || continue
  case "$LAST" in assistant*) echo "answer: $LAST"; break;; esac
done
curl -s -X DELETE "$API/api/v2/sessions/$SID?wipe=true" -H "$AUTH"; echo
