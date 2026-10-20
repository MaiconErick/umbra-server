"""Tests for edit_file / grep / glob / run, the project brief and compaction."""
import os, sys, tempfile, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import tools as T
from agent.core import UmbraAgent, trim_history
from store import project_brief

fails = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else "   <- " + str(detail)[:200]))
    if not cond:
        fails.append(label)


root = tempfile.mkdtemp(prefix="zz_coding_")
src = os.path.join(root, "core.py")
with open(src, "w") as f:
    f.write("def a():\n    return 1\n\ndef b():\n    return 2\n\ndef c():\n    return 2\n")

# --- edit_file ---
r = T.dispatch_tool("edit_file", {"path": src, "old_string": "def a():\n    return 1",
                                  "new_string": "def a():\n    return 42"})
check("edit_file applies", "Edited" in r and "return 42" in open(src).read(), r)
check("edit_file shows context", "return 42" in r and "| " in r, r)

r = T.dispatch_tool("edit_file", {"path": src, "old_string": "return 2", "new_string": "return 3"})
check("edit_file refuses ambiguous match", r.startswith("ERROR") and "2 times" in r, r)

r = T.dispatch_tool("edit_file", {"path": src, "old_string": "return 2", "new_string": "return 3",
                                  "replace_all": True})
body = open(src).read()
check("edit_file replace_all", body.count("return 3") == 2, body)

r = T.dispatch_tool("edit_file", {"path": src, "old_string": "does not exist here", "new_string": "x"})
check("edit_file not found -> error", r.startswith("ERROR") and "not found" in r, r)

r = T.dispatch_tool("edit_file", {"path": src, "old_string": "def a():\n    return 42\n", "new_string": ""})
check("edit_file empty new_string deletes", "def a():" not in open(src).read(), open(src).read())

r = T.dispatch_tool("edit_file", {"path": os.path.join(root, "nope.py"), "old_string": "x", "new_string": "y"})
check("edit_file missing file -> clean error", r.startswith("File not found"), r)

# --- grep / glob ---
os.makedirs(os.path.join(root, "sub", "build"), exist_ok=True)
with open(os.path.join(root, "sub", "app.kt"), "w") as f:
    f.write("fun main() {\n  println(\"Needle\")\n}\n")
with open(os.path.join(root, "sub", "build", "ignored.kt"), "w") as f:
    f.write("Needle\n")

r = T.dispatch_tool("grep", {"pattern": "Needle", "path": root})
check("grep finds the match", "app.kt:2" in r, r)
check("grep skips build dirs", "ignored.kt" not in r, r)

r = T.dispatch_tool("grep", {"pattern": "needle", "path": root, "ignore_case": True, "include": "*.kt"})
check("grep ignore_case + include", "app.kt:2" in r, r)

r = T.dispatch_tool("grep", {"pattern": "definitely-not-here", "path": root})
check("grep no match message", r.startswith("No matches"), r)

r = T.dispatch_tool("glob", {"pattern": "*.kt", "path": root})
check("glob finds kt files", "app.kt" in r, r)

# --- run ---
r = T.dispatch_tool("run", {"path": root})
check("run refuses unknown project", "Couldn't tell" in r, r)

with open(os.path.join(root, "Makefile"), "w") as f:
    f.write("all:\n\t@echo make-ok\n")
r = T.dispatch_tool("run", {"path": root})
check("run detects Makefile and runs it", "make-ok" in r, r)

# --- project brief ---
with open(os.path.join(root, "CLAUDE.md"), "w") as f:
    f.write("# Project\nAlways run the tests before answering.\n")
found = project_brief.find(os.path.join(root, "sub", "app.kt"))
check("brief found from a nested file", bool(found) and found[1].endswith("CLAUDE.md"), found)
check("brief text read", found and "Always run the tests" in found[0], found)


class Stub(UmbraAgent):
    def __init__(self):
        self._turn = {}
        self.conversation = []
        self.current_sid = None


s = Stub()
b1 = UmbraAgent._brief_for(s, os.path.join(root, "sub", "app.kt"))
b2 = UmbraAgent._brief_for(s, os.path.join(root, "sub", "app.kt"))
check("brief injected once per turn", b1.startswith("[project brief") and b2 == "", (b1[:40], b2))

s2 = Stub()
big = [{"role": "user", "content": "please refactor the parser"},
       {"role": "assistant", "content": "working on it", "tool_calls": [
           {"function": {"name": "edit_file"}}, {"function": {"name": "run"}}]}]
big += [{"role": "assistant", "content": "y" * 2000} for _ in range(20)]
s2.conversation = list(big)
UmbraAgent._compact_history(s2)
first = s2.conversation[0]
check("compaction adds a note", first["role"] == "user" and first["content"].startswith("[system] Earlier"), first["content"][:80])
check("compaction note keeps the tools", "edit_file" in first["content"] and "run" in first["content"], first["content"][:300])
check("compaction keeps the budget", sum(len(str(m.get("content") or "")) for m in s2.conversation) <= 24000 + 2000)

s3 = Stub()
s3.conversation = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
UmbraAgent._compact_history(s3)
check("short history untouched by compaction", len(s3.conversation) == 2 and not s3.conversation[0]["content"].startswith("[system]"))

shutil.rmtree(root, ignore_errors=True)
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
