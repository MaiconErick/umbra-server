"""The 'you edited code but never built it' nudge: flags and decision."""
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import agent.core as C
from agent.core import UmbraAgent

fails = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else "   <- " + str(detail)[:160]))
    if not cond:
        fails.append(label)


# --- which commands count as "checked it" ---
for cmd in ("bash gradlew assembleDebug", "npm test", "npm run build", "pytest -q",
            "python3 -m pytest tests", "cargo check", "go test ./...", "make", "tsc --noEmit",
            "flutter analyze", "dotnet build"):
    check(f"counts: {cmd}", C._is_build_command(cmd), cmd)

for cmd in ("ls -la", "cat Makefile", "echo make it work", "git status", "git log",
            "npm install", "grep -n build main.go", "echo pytest"):
    check(f"ignores: {cmd}", not C._is_build_command(cmd), cmd)


class Stub(UmbraAgent):
    def __init__(self):
        self._turn = {"tools": [], "memory": [], "media": [], "calls": {}, "reads": {}}
        self.conversation = []
        self.current_sid = None

    def _set_status(self, *_a, **_k):
        pass

    def _emit(self, *_a, **_k):
        pass


s = Stub()
check("no edit yet -> no nudge", not UmbraAgent._should_nudge_verify(s))

# editing counts, and then the nudge is due
root = tempfile.mkdtemp(prefix="zz_nudge_")
p = os.path.join(root, "a.py")
open(p, "w").write("def a():\n    return 1\n")
UmbraAgent._handle_tool_inner(s, "edit_file", {"path": p, "old_string": "return 1", "new_string": "return 2"})
check("edit_file counts as an edit", s._turn.get("edits") == 1, s._turn.get("edits"))
check("edit with no build -> nudge due", UmbraAgent._should_nudge_verify(s))

# a failed edit must not count
before = s._turn.get("edits")
UmbraAgent._handle_tool_inner(s, "edit_file", {"path": p, "old_string": "nope-not-here", "new_string": "x"})
check("failed edit is not counted", s._turn.get("edits") == before, s._turn.get("edits"))

# a note is not code: editing it must not demand a build
note = os.path.join(root, "notes.txt")
open(note, "w").write("hello" + chr(10))
UmbraAgent._handle_tool_inner(s, "edit_file", {"path": note, "old_string": "hello", "new_string": "bye"})
check("txt edit is not counted as code", s._turn.get("edits") == before, s._turn.get("edits"))

# running a build clears it
s._turn["verify_nudged"] = False
UmbraAgent._handle_tool_inner(s, "run", {"path": root})
check("run that found no project does not clear the nudge",
      UmbraAgent._should_nudge_verify(s), s._turn.get("verified"))

s._turn["verified"] = True
check("built -> no nudge", not UmbraAgent._should_nudge_verify(s))

s._turn["verified"] = False
s._turn["verify_nudged"] = True
check("nudge only once per turn", not UmbraAgent._should_nudge_verify(s))

import shutil
shutil.rmtree(root, ignore_errors=True)
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
