#!/bin/bash
# Real turn: does the project's CLAUDE.md reach the model when it touches the folder?
set -u
API=http://127.0.0.1:8765
TOK=$(grep '^XUMBRA_API_TOKEN=' /home/pi/MaiconErick/Umbra/.env | cut -d= -f2-)
AUTH="Authorization: Bearer $TOK"
mkdir -p /tmp/zzproj/sub
printf '# zzproj\nIMPORTANT: this project uses tabs, never spaces.\n' > /tmp/zzproj/CLAUDE.md
printf 'first line\nsecond line\n' > /tmp/zzproj/sub/notes.txt

SID=$(curl -s -X POST $API/api/v2/sessions -H "$AUTH" -H 'Content-Type: application/json' \
        -d '{"summary":"zz brief"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')
echo "sid=$SID"
curl -s -X POST $API/api/v2/sessions/$SID/send -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"Read /tmp/zzproj/sub/notes.txt and tell me, in one short line, what the project rules say about indentation."}' ; echo

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

echo "--- did the tool result carry the brief?"
curl -s -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
hit = False
for m in d["messages"]:
    for tool in ((m.get("turn") or {}).get("tools") or []):
        res = str(tool.get("result") or "")
        if "[project brief" in res or "tabs, never spaces" in res:
            hit = True
            print("  ", tool.get("name"), "|", res[:140].replace("\n", " "))
print("BRIEF IN TOOL RESULT:", hit)
'
curl -s -X DELETE "$API/api/v2/sessions/$SID?wipe=true" -H "$AUTH" > /dev/null
rm -rf /tmp/zzproj
