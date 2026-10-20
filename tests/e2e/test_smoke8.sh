#!/bin/bash
# Real turn: does the prompt of a suggestion request carry the games page?
set -u
API=http://127.0.0.1:8765
TOK=$(grep '^XUMBRA_API_TOKEN=' /home/pi/MaiconErick/Umbra/.env | cut -d= -f2-)
AUTH="Authorization: Bearer $TOK"
SID=$(curl -s -X POST $API/api/v2/sessions -H "$AUTH" -H 'Content-Type: application/json' \
        -d '{"summary":"zz memory routing"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["session"]["id"])')
echo "sid=$SID"
curl -s -X POST $API/api/v2/sessions/$SID/send -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"text":"Pesquisa na internet jogos de theory crafting e me recomenda 3."}' ; echo

for i in $(seq 1 50); do
  sleep 3
  LAST=$(curl -s -m 3 -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
r=d["messages"][-1] if d["messages"] else {}
print(r.get("role"), "|", (r.get("text") or "")[:200])
' 2>/dev/null) || continue
  case "$LAST" in assistant*) break;; esac
done

echo "--- full answer:"
curl -s -H "$AUTH" "$API/api/v2/sessions/$SID/state?limit=0" | python3 -c '
import json,sys
d=json.load(sys.stdin)
t=(d["messages"][-1].get("text") or "")
print(t[:900])
print("SIRALIM RECOMMENDED AGAIN:", "siralim" in t.lower())
'
echo "--- prompt of that turn:"
python3 - "$SID" <<'PY'
import json, sys
sid = sys.argv[1]
p = f"/home/pi/MaiconErick/Umbra/sessions/prompts/{sid}.json"
try:
    turns = json.load(open(p))
except Exception as e:
    print("no prompt snapshot:", e); raise SystemExit
for t in turns:
    seg = json.dumps(t.get("segments") or "", ensure_ascii=False)
    print("games page in system:", "games/games" in seg)
    print("Siralim in system:", "siralim" in seg.lower())
    print("From your memory:", "From your memory" in seg)
    r = t.get("router") or {}
    print("router:", json.dumps(r)[:200])
PY
curl -s -X DELETE "$API/api/v2/sessions/$SID?wipe=true" -H "$AUTH" > /dev/null
