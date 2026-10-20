"""Tests for the auto budget extension, empty-bubble skip and folded ids."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent.core import UmbraAgent
from store import messages as msg_store

fails = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else "   <- " + str(detail)[:140]))
    if not cond:
        fails.append(label)


class Stub:
    def __init__(self):
        self._turn = {}
        self.conversation = []
        self.current_sid = None
        self.emitted = []

    def _cancelled(self):
        return False

    def _emit(self, *a, **k):
        self.emitted.append((a, k))


s = Stub()
check("first extension raises the limit", UmbraAgent._extend_tool_budget(s, 60) == 120, s._turn)
check("only one extension per turn", UmbraAgent._extend_tool_budget(s, 120) == 120)
check("note appended for the model", any("extended" in str(m) for m in s.conversation))
check("status event emitted", any("budget" in str(k).lower() for _, k in s.emitted), s.emitted)

s2 = Stub()
s2._cancelled = lambda: True
check("no extension once cancelled", UmbraAgent._extend_tool_budget(s2, 60) == 60)

b = Stub()
b.current_sid = "0998"
try:
    os.remove("/home/pi/MaiconErick/Umbra/sessions/msgs/0998.jsonl")
except OSError:
    pass
check("empty assistant row is not stored", UmbraAgent._store(b, "assistant", "") is None)
check("assistant row with turn details is kept",
      UmbraAgent._store(b, "assistant", "", turn={"tools": []}) is not None)
check("assistant row with text is kept", UmbraAgent._store(b, "assistant", "hi") is not None)
check("user row always stored", UmbraAgent._store(b, "user", "") is not None)
try:
    os.remove("/home/pi/MaiconErick/Umbra/sessions/msgs/0998.jsonl")
except OSError:
    pass

items = [
    {"id": "m-a", "role": "assistant", "ts": 100, "text": "narrating", "interim": True},
    {"id": "m-b", "role": "assistant", "ts": 101, "text": "done", "turn": {"tools": [{"name": "shell"}]}},
    {"id": "m-c", "role": "assistant", "ts": 102, "text": "narrating2", "interim": True},
    {"id": "m-d", "role": "assistant", "ts": 103, "text": "narrating3", "interim": True},
    {"id": "m-e", "role": "user", "ts": 104, "text": "hello"},
]
out = msg_store._fold_interim(items)
ids = [r["id"] for r in out]
check("folded ids are unique", len(ids) == len(set(ids)), ids)
check("narration folds into its tool call",
      out[0]["turn"]["tools"][0]["text"] == "narrating", out[0])
check("trailing narration keeps its own rows",
      [r["text"] for r in out[-3:]] == ["narrating2", "narrating3", "hello"],
      [r["text"] for r in out])
check("ids stable across reads",
      [r["id"] for r in msg_store._fold_interim(items)] == ids)

print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
