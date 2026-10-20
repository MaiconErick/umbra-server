#!/bin/bash
# Real turn: does Umbra actually load and use edit_file?
set -u
API=http://127.0.0.1:8765
TOK=$(grep '^XUMBRA_API_TOKEN=' /home/pi/MaiconErick/Umbra/.env | cut -d= -f2-)
AUTH="Authorization: Bearer $TOK"
rm -f /tmp/zz_umbra_edit.txt
printf 'alpha beta gamma\n' > /tmp/zz_umbra_edit.txt

SID=$(curl -s -X POST $API/api/v2/sessions -H "$AUTH" -H 'Content-Type: application/json' \
        -d '{"summary":"zz coding tools"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')
echo "sid=$SID"
curl -s -X POST $API/api/v2/sessions/$SID/send -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"Load the edit_file tool (load_tools) and use it on /tmp/zz_umbra_edit.txt to replace the word beta with the word Umbra. Then tell me the new content of the file in one line."}' ; echo

for i in $(seq 1 40); do
  sleep 3
  LAST=$(curl -s -m 3 -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d["messages"][-1] if d["messages"] else {}
print(r.get("role"), "|", (r.get("text") or "")[:160])
' 2>/dev/null) || continue
  case "$LAST" in assistant*) echo "answer: $LAST"; break;; esac
done

echo "--- file now:"; cat /tmp/zz_umbra_edit.txt
echo "--- tools the turn used:"
curl -s -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
for m in d["messages"]:
    t = m.get("turn") or {}
    for tool in (t.get("tools") or []):
        print(" ", tool.get("name"), "|", str(tool.get("args"))[:90])
'
curl -s -X DELETE "$API/api/v2/sessions/$SID?wipe=true" -H "$AUTH" > /dev/null
rm -f /tmp/zz_umbra_edit.txt
