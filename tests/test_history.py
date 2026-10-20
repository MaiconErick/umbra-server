"""Tests for the token-aware history trim, output cap and tool schema cache."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import agent.core as C
from agent import toolset

fails = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else "   <- " + str(detail)[:140]))
    if not cond:
        fails.append(label)


def U(i, n=50):
    return {"role": "user", "content": ("u%d " % i) + "x" * n}


def A(i, n=50):
    return {"role": "assistant", "content": ("a%d " % i) + "y" * n}


# small conversation untouched
small = [U(0), A(0), U(1)]
check("short history untouched", C.trim_history(small) == small)

# many small messages -> capped by count, still starts with a user turn
conv = []
for i in range(100):
    conv.append(U(i) if i % 2 == 0 else A(i))
out = C.trim_history(conv)
check("message cap respected", len(out) <= C.HISTORY_MAX_MESSAGES + 1, len(out))
check("starts with a user turn", out[0]["role"] == "user", out[0])
check("keeps the newest message", out[-1] is conv[-1])

# big messages -> capped by characters
conv = [U(i, 5000) if i % 2 == 0 else A(i, 5000) for i in range(10)]
out = C.trim_history(conv)
total = sum(C._msg_chars(m) for m in out)
check("char budget respected", total <= C.HISTORY_MAX_CHARS, total)
check("big history starts with a user turn", out[0]["role"] == "user", out[0])

# a tool result whose call fell out of the window must not lead the history
conv = [{"role": "tool", "content": "orphan"}, A(0), {"role": "tool", "content": "t"},
        U(1), A(1)]
out = C.trim_history(conv)
check("leading tool/assistant rows dropped", out[0]["role"] == "user", out[0])

# multimodal content is counted by its text parts
mm = [{"role": "assistant", "content": [{"type": "text", "text": "z" * 100}]},
      {"role": "user", "content": [{"type": "text", "text": "w" * 50}]}]
check("multimodal chars counted",
      [C._msg_chars(m) for m in mm] == [100, 50], [C._msg_chars(m) for m in mm])

# no user message at all -> empty rather than an invalid request
check("history without a user turn is emptied",
      C.trim_history([A(0), A(1)]) == [])

check("output cap comes from config", C._max_output_tokens() == 8192, C._max_output_tokens())

a = toolset.build({"shell", "read_file"})
b = toolset.build({"shell", "read_file"})
check("tool schema cache is consistent",
      [t["function"]["name"] for t in a] == [t["function"]["name"] for t in b])
check("always-on tools present",
      {"memory", "load_tools", "web_search", "find_images"} <= {t["function"]["name"] for t in a},
      [t["function"]["name"] for t in a])
check("loaded tools included", {"shell", "read_file"} <= {t["function"]["name"] for t in a})

print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
