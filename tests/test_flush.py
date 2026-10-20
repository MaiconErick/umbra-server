"""Direct test of Hub.flush_partials / save_queues (throwaway sid 0999)."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from api.hub import HUB, _Turn
from store import messages as msg_store

sid = "0999"
t = _Turn(sid, "hello", "app")
t.partial = "This is the half-written answer the user was reading."
HUB.turns[sid] = t
n = HUB.flush_partials()
HUB.turns.clear()

rows = msg_store.list_messages(sid)
print("saved:", n)
for r in rows:
    print(" ", r["role"], repr(r["text"][:60]), "interrupted=", bool(r.get("interrupted")))

# the resume check must see the user row, not the draft, as the last real row
print("draft flagged correctly:", len(rows) == 1 and rows[0].get("interrupted") is True)

# queue snapshot round-trip with a sid that is not a real session (never starts)
HUB.queues = {"nosuch": [{"text": "queued msg", "media_ids": [], "source": "app"}]}
HUB.save_queues()
path = os.path.join("/home/pi/MaiconErick/Umbra/data/chats", "queue_snapshot.json")
print("snapshot written:", os.path.exists(path))
HUB.queues = {}
print("restored (should be 0, unknown session):", HUB.restore_queues())
print("snapshot consumed:", not os.path.exists(path))

os.remove(os.path.join("/home/pi/MaiconErick/Umbra/sessions/msgs", sid + ".jsonl"))
print("cleanup ok")
