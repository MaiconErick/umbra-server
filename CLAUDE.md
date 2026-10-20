# Umbra server (Rock Pi, ~/MaiconErick/Umbra)

FastAPI on :8765 + Telegram bot, systemd unit `umbra`. Restart with a reason: `bin/restart.sh "why"`.
The Android client is xUmbra. Source copy on the user's PC: `C:\DEV\Python\UmbraServer`.

## Agents (the pixel rooms in xUmbra → Agents → 🏠 Rooms)

An **agent** is one of the user's background services: something that runs on a schedule or
on its own (a ping, a backup, a sync…). Each agent gets a pixel room in the app. Its character
sleeps while the agent is idle, walks to the desk so he sits down right when a run starts,
shows ✅ or ❌ when it ends, and the room turns red with ⚠ when the agent is unhealthy.
Umbra's own internals (heartbeat, note-taker, chat summarizer) are **not** agents
(`store/workers.py` HIDDEN).

### Adding an agent: two steps, no Umbra code changes

**1. The service exposes `GET /agent`** (any port, any machine Umbra can reach) in this shape:

```json
{
  "agent":   {"id": "claude-session-primer", "name": "Session Primer",
              "description": "One or two plain sentences: what it does and when.", "host": "rockpi-4b"},
  "version": 32,                         // bump on EVERY change (state, last_run, next_run…)
  "state":   "sleeping",                 // "sleeping" | "working" | "error" — nothing else
  "healthy": true,                       // false → room goes red, ⚠ bubble
  "checks":  {"scheduler": {"ok": true, "detail": "last beat 17s ago"}},   // failing ones are shown
  "task":    {"name": "ping", "reason": "scheduled 04:00", "started_at": "2026-10-08T04:00:00-03:00"},  // only while working, else null
  "last_run": {"at": "2026-10-07T19:05:11-03:00", "ok": true, "reason": "auto-restart",
               "duration_s": 6.9, "message": "ok"},
  "next_run": "2026-10-08T04:00:00-03:00",   // ISO time (or epoch seconds), null if unknown
  "next_auto_restart": null,                 // optional second "next" time; the earlier one is used
  "history": [ /* last ~10 last_run objects */ ],
  "started_at": "…", "now": "…"
}
```

It **must support long-polling**: `GET /agent?wait=N&since=V` returns as soon as `version > V`,
or after N seconds (N ≤ 60). Umbra polls with `wait=55`. Without this, a run that takes a few
seconds is missed and the character never goes to the desk.

Working example: `/home/pi/MaiconErick/ClaudeUsage/usage_server.py`. See its docstring and the
`/agent` handler in `do_GET`, plus the small `agent` state object it keeps (snapshot() and
wait(since, timeout) on a threading.Condition, with version += 1 + notify_all on each change).

Rules:
- `state` is "working" for the whole run, and only during the run.
- Set `next_run` whenever you know it. The room's countdown and the character's walk timing depend on it.
- Times are ISO 8601 with an offset (or epoch seconds).
- Keep `description` short and non-technical. The user reads it in the app.

**2. List it in `data/settings/agents.json`** on the Pi:

```json
[
  {"id": "primer", "url": "http://127.0.0.1:8767/agent", "emoji": "⚡"},
  {"id": "backup", "url": "http://192.168.1.50:9000/agent", "emoji": "💾"}
]
```

- `id` is the room's id. Never change it: the room layout and settings are saved under it in
  `data/settings/rooms.json`.
- `emoji` shows next to the room's name in the app.
- `store/agents_bridge.py` re-reads this file every 30 s, so no restart is needed. A new room
  appears in the app (pull it open again if it was already showing).

**Check it:**

```
curl -s 'http://<host>:<port>/agent' | python3 -m json.tool
TOKEN=...   # Umbra API token, from data/settings/app_settings.json (api_token)
curl -s -H "Authorization: Bearer $TOKEN" localhost:8765/api/v2/workers
```

The new id should be listed there with `next_run`, `last_run` and `ok`.

### Agents that live inside Umbra itself

Code running inside the Umbra process can report directly, without HTTP:

```python
from store import workers as W
W.register("internet", "Internet watcher", "Pings 1.1.1.1 every 30 s…", every=30, emoji="🌐")
W.scheduled("internet", time.time() + 30)       # when the next run is
with W.run("internet") as r:                    # marks it working; records duration + outcome
    r.ok, r.note = ok, "online"
```

Example: the internet watcher in `store/lifecycle.py`.

### Current agents

| id | what | where |
|---|---|---|
| internet | pings 1.1.1.1 every 30 s, tells Telegram after an outage | inside Umbra (`store/lifecycle.py`) |
| netwatch | who's on the home network: passive ARP listening + gentle arp-scan every 5 min; posts new/gone devices in the 🛜 Network chat (persona `netwatch`, tool `network`) | inside Umbra (`store/netwatch.py`, data in `data/netwatch/`) |
| primer | Claude Session Primer, starts the 5-hour windows (04:00 + auto-restart) | me-claude-usage :8767 (`~/MaiconErick/ClaudeUsage`) |
