"""Tests for the code map (symbols + FTS5)."""
import os, sys, tempfile, shutil, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from store import code_index
from agent import tools as T

fails = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else "   <- " + str(detail)[:220]))
    if not cond:
        fails.append(label)


root = tempfile.mkdtemp(prefix="zz_map_")
os.makedirs(os.path.join(root, "ui"))
os.makedirs(os.path.join(root, "build"))          # must be skipped

with open(os.path.join(root, "core.py"), "w") as f:
    f.write('"""The matrix rain engine."""\n\n'
            'class MatrixRain:\n'
            '    """Draws the falling letters."""\n'
            '    def draw(self, t):\n'
            '        """Fades the frame colour."""\n'
            '        pass\n\n'
            'def push_to_pi(apk, module):\n'
            '    """Copy the APK to the Pi."""\n'
            '    return module\n')
with open(os.path.join(root, "ui", "MatrixStream.kt"), "w") as f:
    f.write('// The matrix rain background used by the chat screen.\n'
            'class MatrixStream {\n'
            '    fun fadeColor(c: Int): Int { return c }\n'
            '}\n'
            'val MatrixGreen = 0x00FF41\n')
with open(os.path.join(root, "build", "Junk.kt"), "w") as f:
    f.write("class JunkToIgnore {}\n")

changed, note = code_index.refresh(root)
check("indexing worked", changed >= 2, (changed, note))
r = code_index.search("JunkToIgnore", root)
check("build dir skipped", "Junk.kt" not in r, r)

r = T.dispatch_tool("repo_map", {"op": "find", "pattern": "push_to_pi", "path": root})
check("find by symbol name", "push_to_pi" in r and "core.py:9" in r, r)

r = T.dispatch_tool("repo_map", {"op": "find", "pattern": "matrix", "path": root})
check("FTS finds docstrings/comments", "MatrixStream" in r and "MatrixRain" in r, r)

r = T.dispatch_tool("repo_map", {"op": "find", "pattern": "MatrixGreen", "path": root})
check("finds constants", "MatrixGreen" in r, r)

r = T.dispatch_tool("repo_map", {"op": "find", "pattern": "MatrixStream.kt", "path": root})
check("finds file rows", "MatrixStream.kt" in r, r)

r = T.dispatch_tool("repo_map", {"op": "outline", "path": os.path.join(root, "core.py")})
check("outline of one file", "class MatrixRain" in r and "function" in r, r)

r = T.dispatch_tool("repo_map", {"op": "outline", "path": os.path.join(root, "ui")})
check("outline of a folder", "MatrixStream" in r, r)

# --- freshness: a file edited by another process must be re-read ---
time.sleep(0.05)
with open(os.path.join(root, "core.py"), "a") as f:
    f.write('\ndef brand_new_symbol():\n    """Added later by another session."""\n    pass\n')
r = T.dispatch_tool("repo_map", {"op": "find", "pattern": "brand_new_symbol", "path": root})
check("edit from another session is picked up", "brand_new_symbol" in r, r)

# same size, different mtime (the classic mtime-only change)
p = os.path.join(root, "ui", "MatrixStream.kt")
st = os.stat(p)
with open(p, "a") as f:
    f.write("val MatrixBlue = 0x0000FF\n")
os.utime(p, (st.st_atime, st.st_mtime + 5))
r = T.dispatch_tool("repo_map", {"op": "find", "pattern": "MatrixBlue", "path": root})
check("mtime change re-indexes", "MatrixBlue" in r, r)

# --- deletion ---
os.remove(os.path.join(root, "ui", "MatrixStream.kt"))
code_index.ensure(root, ttl=0)   # a real query would sweep once the 30 s TTL expires
r = T.dispatch_tool("repo_map", {"op": "find", "pattern": "MatrixStream", "path": root})
check("deleted file leaves the index", "MatrixStream.kt" not in r, r)

st = code_index.stats()
check("stats sane", st["files"] >= 2 and st["symbols"] >= 3, st)

print("DB:", st["db_bytes"] // 1024, "KB for", st["files"], "files /", st["symbols"], "symbols")
shutil.rmtree(root, ignore_errors=True)
print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
