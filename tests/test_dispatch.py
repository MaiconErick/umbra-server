"""Regression test for the 'path' KeyError and the argument handling around it."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from agent import tools as T

fails = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else "   <- " + str(detail)[:120]))
    if not cond:
        fails.append(label)


r = T.dispatch_tool("write_file", {"content": "x"})
check("write_file without path -> error string", r.startswith("ERROR") and "path" in r, r)

r = T.dispatch_tool("read_file", {"file_path": "/etc/hostname"})
check("read_file accepts file_path alias", "needs" not in r.lower(), r)

r = T.dispatch_tool("write_file", {"path": "/tmp/zz_empty.txt", "content": ""})
check("write_file accepts empty content",
      os.path.exists("/tmp/zz_empty.txt") and os.path.getsize("/tmp/zz_empty.txt") == 0, r)
try:
    os.remove("/tmp/zz_empty.txt")
except OSError:
    pass

r = T.dispatch_tool("shell", {"cmd": "echo hello-alias"})
check("shell accepts cmd alias", "hello-alias" in r, r)

r = T.dispatch_tool("shell", {})
check("shell without command -> error string", r.startswith("ERROR") and "command" in r, r)

r = T.dispatch_tool("nope_tool", {})
check("unknown tool", "Unknown tool" in r, r)

r = T.dispatch_tool("tts", {"text": "   "})
check("tts with blank text -> error string", r.startswith("ERROR"), r)

def _boom(path, *_args):
    raise KeyError("internal")


_orig = T.tool_read_file
T.tool_read_file = _boom
r = T.dispatch_tool("read_file", {"path": "/etc/hostname"})
T.tool_read_file = _orig
check("internal KeyError reported as a crash, not a missing arg",
      "KeyError" in r and "needs" not in r, r)

escaped = False
for bad in (None, [], "x", {"command": None}, {"path": []}, {"content": {}}):
    try:
        T.dispatch_tool("shell", bad)
        T.dispatch_tool("read_file", bad)
        T.dispatch_tool("write_file", bad)
    except Exception as e:
        escaped = True
        print("ESCAPED:", repr(bad), e)
check("no exception escapes dispatch_tool", not escaped)

print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
