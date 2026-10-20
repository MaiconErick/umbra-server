#!/bin/bash
# One real turn through the agent, to prove the dispatcher change didn't break
# the normal tool path.
set -u
API=http://127.0.0.1:8765
SID=$(curl -s -X POST $API/api/v2/sessions -H 'Content-Type: application/json' \
        -d '{"summary":"zz smoke"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')
echo "sid=$SID"
curl -s -X POST $API/api/v2/sessions/$SID/send -H 'Content-Type: application/json' \
  -d '{"text":"Run the shell command `echo dispatch-ok` and reply with only its output."}' ; echo
for i in $(seq 1 30); do
  sleep 3
  LAST=$(curl -s -m 3 "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d["messages"][-1] if d["messages"] else {}
print(r.get("role"), "|", (r.get("text") or "")[:80])
' 2>/dev/null) || continue
  case "$LAST" in assistant*) echo "answer: $LAST"; break;; esac
done
curl -s -X DELETE "$API/api/v2/sessions/$SID?wipe=true"; echo
