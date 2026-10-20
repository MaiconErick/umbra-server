"""read_file windows, todo list and background jobs."""
import os, sys, time, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import tools as T

fails = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else "   <- " + str(detail)[:200]))
    if not cond:
        fails.append(label)


root = tempfile.mkdtemp(prefix="zz_tools2_")
big = os.path.join(root, "big.txt")
with open(big, "w") as f:
    for i in range(1, 1001):
        f.write(f"line {i}\n")

# --- read_file windows ---
r = T.dispatch_tool("read_file", {"path": big, "offset": 10, "limit": 5})
check("window header", "lines 10-14 of 1000" in r, r[:120])
check("window body", r.split("\n")[1].strip() == "line 10" and r.strip().endswith("line 14"), r)
check("points to the next window", "offset=15" in r, r[:160])

r = T.dispatch_tool("read_file", {"path": big, "offset": 990, "limit": 50})
check("window clamps to the end", "lines 990-1000 of 1000" in r and "offset=" not in r, r[:120])

r = T.dispatch_tool("read_file", {"path": big})
check("no window -> bounded chunk", "of 1000" in r and len(r) < 12000, (len(r), r[:80]))

r = T.dispatch_tool("read_file", {"path": big, "offset": 5000})
check("offset past the end", "past the end" in r, r)

# --- todo ---
T.set_current_sid("zz_todo_test")
r = T.dispatch_tool("todo", {"op": "set", "items": ["read the code", "change it", "build it"]})
check("todo set", "[ ] t1 read the code" in r and "0/3" in r, r)

r = T.dispatch_tool("todo", {"op": "start", "item_id": "t1"})
check("todo start", "[~] t1" in r, r)

r = T.dispatch_tool("todo", {"op": "done", "item_id": "t1"})
check("todo done", "[x] t1" in r and "1/3" in r, r)

r = T.dispatch_tool("todo", {"op": "add", "text": "ship it"})
check("todo add", "t4 ship it" in r, r)

r = T.dispatch_tool("todo", {"op": "list"})
check("todo list", "[x] t1" in r and "t4" in r, r)

r = T.dispatch_tool("todo", {"op": "update", "item_id": "t9", "status": "done"})
check("todo unknown id", r.startswith("No todo with id"), r)

r = T.dispatch_tool("todo", {"op": "clear"})
check("todo clear", "empty" in r.lower(), r)
T.set_current_sid(None)

# --- jobs ---
r = T.dispatch_tool("jobs", {"op": "start", "command": "sleep 2; echo job-done"})
check("job started", r.startswith("Started j"), r)
jid = r.split()[1]
time.sleep(3)
r = T.dispatch_tool("jobs", {"op": "output", "job_id": jid})
check("job output after it finished", "exit 0" in r and "job-done" in r, r)

r = T.dispatch_tool("jobs", {"op": "start", "command": "sleep 60"})
jid2 = r.split()[1]
r = T.dispatch_tool("jobs", {"op": "list"})
check("job list shows it running", jid2 in r and "running" in r, r)
r = T.dispatch_tool("jobs", {"op": "kill", "job_id": jid2})
check("job kill", "SIGTERM" in r, r)
time.sleep(1)
r = T.dispatch_tool("jobs", {"op": "output", "job_id": jid2})
check("killed job reports finished", "finished" in r, r)

import shutil
shutil.rmtree(root, ignore_errors=True)
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
