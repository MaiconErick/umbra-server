"""Sanity checks for the deferred-restart guard (run on the Pi)."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from store.restart import is_restart_command

should_hit = [
    'cd /home/pi/MaiconErick/Umbra && sudo systemctl restart umbra 2>/dev/null || systemctl --user restart umbra 2>/dev/null; sleep 3',
    'sudo -n systemctl restart umbra.service 2>&1 | tail -2',
    'cd ~/MaiconErick/Umbra; setsid nohup bash -c "sleep 75; systemctl restart umbra.service" >/dev/null 2>&1 < /dev/null &',
    '(sleep 2; sudo systemctl restart umbra) >/dev/null 2>&1 &',
    'bash bin/restart.sh fix',
    'systemctl stop umbra',
    'sudo systemctl --user restart umbra',
]

should_miss = [
    'grep -n "systemctl restart umbra" logs/commands.txt | head',
    'systemctl status umbra --no-pager',
    'ls -la; echo systemctl restart umbra',
    'systemctl restart xwapp',
    'cat bin/restart.sh',
]

bad = 0
for c in should_hit:
    ok = is_restart_command(c)
    bad += 0 if ok else 1
    print("HIT " if ok else "MISS", "|", c[:78])
for c in should_miss:
    ok = not is_restart_command(c)
    bad += 0 if ok else 1
    print("ok  " if ok else "FALSE-POSITIVE", "|", c[:78])

print("FAILURES:", bad)
sys.exit(1 if bad else 0)
