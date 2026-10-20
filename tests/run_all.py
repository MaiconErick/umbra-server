"""
Run every test in tests/.

    venv/bin/python tests/run_all.py            # all of them
    venv/bin/python tests/run_all.py coding     # only names containing "coding"

Each test file is a standalone script: it prints "ok/FAIL" lines and exits
non-zero on failure. They touch only throwaway files (sids 0998/0999, temp
dirs under /tmp), so they are safe to run against the live install.

The end-to-end scripts in tests/e2e/ need the server running and the API
token; run them by hand (bash tests/e2e/test_smoke3.sh).
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main() -> int:
    flt = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(f for f in os.listdir(HERE)
                   if f.startswith("test_") and f.endswith(".py") and flt in f)
    if not files:
        print("no tests matched", flt)
        return 2
    failed = []
    for name in files:
        t0 = time.time()
        try:
            p = subprocess.run([sys.executable, os.path.join(HERE, name)],
                               cwd=ROOT, capture_output=True, text=True, timeout=600)
            code, out = p.returncode, (p.stdout + p.stderr)
        except subprocess.TimeoutExpired:
            code, out = 124, "timeout after 600 s"
        dt = time.time() - t0
        ok = code == 0
        print(("PASS " if ok else "FAIL ") + f"{name}  ({dt:.1f}s)")
        if not ok:
            failed.append(name)
            for line in out.strip().splitlines()[-15:]:
                print("    " + line)
    print(f"\n{len(files) - len(failed)}/{len(files)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
