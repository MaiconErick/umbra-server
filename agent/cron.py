"""
Umbra cron scheduler — APScheduler-backed recurring "agents".

Each agent is one persistent job with:
  id        — uuid hex
  name      — human label ("Daily disk check")
  trigger   — cron expression OR interval seconds
  action    — { type: "shell" | "python" | "prompt", ... }
  enabled   — bool
  sprite    — optional path to bundled or generated sprite image
  history   — last N run records (kept inline, capped)
  state     — derived: "running" | "idle" | "sleeping" (computed)

State persisted to memory/cron_agents.json. Live state events broadcast to a
module-level subscriber so api/server.py can push them on the existing WS.
"""
from __future__ import annotations

import json, os, threading, uuid, time, subprocess, logging
from datetime import datetime
from typing import Optional, Callable, Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from store import paths as _P
CRON_FILE = os.path.join(_P.SETTINGS, "cron_agents.json")
SPRITES_DIR = os.path.join(_P.SETTINGS, "cron_sprites")
os.makedirs(SPRITES_DIR, exist_ok=True)

MAX_HISTORY = 50
_lock = threading.RLock()
_scheduler: Optional[BackgroundScheduler] = None
_event_sink: Optional[Callable[[str, dict], None]] = None
_running_ids: set[str] = set()

def set_event_sink(fn: Callable[[str, dict], None]) -> None:
    """api/server.py registers a callback so cron events ride the chat WS."""
    global _event_sink
    _event_sink = fn

def _emit(event_type: str, **data) -> None:
    if _event_sink:
        try: _event_sink(event_type, data)
        except Exception: pass

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

def _load_all() -> list[dict]:
    if not os.path.exists(CRON_FILE):
        return []
    try:
        with open(CRON_FILE) as f:
            return json.load(f)
    except Exception:
        return []

def _save_all(agents: list[dict]) -> None:
    os.makedirs(os.path.dirname(CRON_FILE), exist_ok=True)
    tmp = CRON_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(agents, f, indent=2)
    os.replace(tmp, CRON_FILE)

def list_agents() -> list[dict]:
    return _load_all()

def get_agent(aid: str) -> Optional[dict]:
    return next((a for a in _load_all() if a["id"] == aid), None)

def _new_id() -> str:
    return uuid.uuid4().hex[:12]

def _validate(agent: dict) -> None:
    if not agent.get("name"):
        raise ValueError("name is required")
    trig = agent.get("trigger") or {}
    if trig.get("type") not in ("cron", "interval"):
        raise ValueError("trigger.type must be 'cron' or 'interval'")
    if trig["type"] == "cron":
        if not trig.get("expr"):
            raise ValueError("trigger.expr required for cron")
        # Validate by attempting to parse via APScheduler
        try:
            CronTrigger.from_crontab(trig["expr"])
        except Exception as e:
            raise ValueError(f"invalid cron expr: {e}")
    if trig["type"] == "interval":
        s = int(trig.get("seconds", 0))
        if s < 30:
            raise ValueError("interval.seconds must be >= 30")
    act = agent.get("action") or {}
    if act.get("type") not in ("shell", "python", "prompt"):
        raise ValueError("action.type must be 'shell', 'python', or 'prompt'")

def _next_ts(aid: str):
    try:
        job = _scheduler.get_job(aid) if _scheduler else None
        return job.next_run_time.timestamp() if job and job.next_run_time else None
    except Exception:
        return None


def _build_trigger(agent: dict):
    trig = agent["trigger"]
    if trig["type"] == "cron":
        return CronTrigger.from_crontab(trig["expr"])
    return IntervalTrigger(seconds=int(trig["seconds"]))

def _run_action(agent: dict) -> dict:
    """Execute one tick of an agent. Returns a history record."""
    aid = agent["id"]
    started = _now_iso()
    started_ts = time.time()
    _running_ids.add(aid)
    _emit("cron.start", id=aid, name=agent["name"], started=started)
    try:
        from store import workers as W
        W.started(f"cron:{aid}")
    except Exception:
        pass

    record = {"started": started, "ok": False, "duration_sec": 0.0, "output": ""}
    try:
        action = agent.get("action", {})
        atype = action.get("type")
        if atype == "shell":
            cmd = action.get("command", "")
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               text=True, timeout=action.get("timeout", 120))
            record["ok"] = r.returncode == 0
            record["output"] = (r.stdout + r.stderr)[:4000]
        elif atype == "python":
            code = action.get("code", "")
            try:
                # Exec in restricted namespace
                ns = {"__name__": f"cron_agent_{aid}"}
                exec(code, ns)
                record["ok"] = True
                record["output"] = "(python executed)"
            except Exception as e:
                record["output"] = repr(e)[:4000]
        elif atype == "prompt":
            # Send a prompt to a (possibly new) session via the agent core.
            # If push=true, the reply is also enqueued as a proactive
            # notification — but ONLY if the target session has explicitly
            # allowed proactive messages (chat_meta.allow_proactive).
            try:
                from agent.core import UmbraAgent
                from store.manager import create_session, get_chat_meta
                target_sid = action.get("session_id")
                push       = bool(action.get("push", False))
                if not target_sid:
                    if push:
                        # Push to a brand-new chat would create an empty,
                        # unread chat the user never opened. Refuse.
                        record["output"] = ("push=true requires an existing "
                                            "session_id (the chat where the "
                                            "topic was discussed).")
                        _emit("cron.skipped", id=aid, reason="push_without_sid")
                    else:
                        target_sid = create_session(f"[cron] {agent['name']}")
                if target_sid and push and not get_chat_meta(target_sid).get("allow_proactive"):
                    record["output"] = (
                        f"Skipped: chat {target_sid} has allow_proactive=false. "
                        f"User must opt in via the chat menu."
                    )
                    _emit("cron.skipped", id=aid, sid=target_sid,
                          reason="proactive_disabled")
                    record["session_id"] = target_sid
                elif target_sid:
                    # Run through the app hub when the API is up: the turn uses
                    # the chat's own agent (history, model, persona), the app
                    # watches it live, and the reply comes back without the
                    # Telegram token footer.
                    try:
                        from api.hub import HUB
                        rec = HUB.run_blocking(target_sid, action.get("prompt", ""),
                                               tools=action.get("tools") or [],
                                               source=f"cron:{aid}") or {}
                        reply = rec.get("text") or ""
                    except ImportError:
                        ag = UmbraAgent()
                        ag.bind_session(target_sid)
                        reply = ag.chat(action.get("prompt", ""),
                                        selected_tools=action.get("tools") or [])
                    record["ok"] = True
                    record["output"] = reply[:4000]
                    record["session_id"] = target_sid

                    if push and reply:
                        from store import notifications as _notifs
                        nrec = _notifs.enqueue(
                            sid=target_sid,
                            text=reply,
                            agent_id=aid,
                        )
                        _emit("notification.enqueued",
                              id=nrec["id"], sid=target_sid, agent_id=aid)
                        record["notification_id"] = nrec["id"]
            except Exception as e:
                record["output"] = f"prompt agent error: {e}"
    except Exception as e:
        record["output"] = f"exec error: {e}"

    record["duration_sec"] = round(time.time() - started_ts, 3)
    _running_ids.discard(aid)
    _emit("cron.end", id=aid, name=agent["name"], **record)
    try:
        from store import workers as W
        W.finished(f"cron:{aid}", ok=bool(record.get("ok")), note=str(record.get("output") or "")[:120],
                   ms=int(record["duration_sec"] * 1000), next_run=_next_ts(aid))
    except Exception:
        pass

    # Persist history
    with _lock:
        agents = _load_all()
        for a in agents:
            if a["id"] == aid:
                hist = a.setdefault("history", [])
                hist.append(record)
                a["history"] = hist[-MAX_HISTORY:]
                a["last_run"] = record["started"]
                a["last_ok"]  = record["ok"]
                break
        _save_all(agents)
    return record

def _schedule_one(agent: dict) -> None:
    if not _scheduler:
        return
    aid = agent["id"]
    # Drop the old job first: a disabled (or edited) agent must stop firing.
    if _scheduler.get_job(aid):
        _scheduler.remove_job(aid)
    if not agent.get("enabled", True):
        return
    trigger = _build_trigger(agent)
    _scheduler.add_job(_run_action, trigger,
                       args=[agent],
                       id=aid,
                       replace_existing=True,
                       misfire_grace_time=60,
                       coalesce=True)
    try:
        from store import workers as W
        trig = agent.get("trigger") or {}
        act = agent.get("action") or {}
        W.register(f"cron:{aid}", agent.get("name") or aid,
                   f"Your scheduled agent — {act.get('type', '?')}: " +
                   str(act.get("prompt") or act.get("command") or act.get("code") or "")[:140],
                   every=float(trig["seconds"]) if trig.get("type") == "interval" else None, emoji="⏰")
        W.scheduled(f"cron:{aid}", _next_ts(aid))
    except Exception:
        pass

def _reschedule_all() -> None:
    if not _scheduler:
        return
    for job in list(_scheduler.get_jobs()):
        _scheduler.remove_job(job.id)
    for a in _load_all():
        try:
            _schedule_one(a)
        except Exception as e:
            logger.warning(f"cron schedule failed for {a.get('id')}: {e}")

def start_scheduler() -> None:
    """Called once at daemon boot."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.start()
    _reschedule_all()
    logger.info(f"cron scheduler started with {len(_load_all())} agents")

def stop_scheduler() -> None:
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None

def create_agent(payload: dict) -> dict:
    _validate(payload)
    with _lock:
        agents = _load_all()
        agent = {
            "id":       _new_id(),
            "name":     payload["name"],
            "trigger":  payload["trigger"],
            "action":   payload["action"],
            "enabled":  payload.get("enabled", True),
            "sprite":   payload.get("sprite") or "bundled-default",
            "tint":     payload.get("tint") or "#00E5FF",
            "created":  _now_iso(),
            "last_run": None,
            "last_ok":  None,
            "history":  [],
        }
        agents.append(agent)
        _save_all(agents)
    _schedule_one(agent)
    _emit("cron.created", id=agent["id"], name=agent["name"])
    return agent

def update_agent(aid: str, payload: dict) -> dict:
    with _lock:
        agents = _load_all()
        target = next((a for a in agents if a["id"] == aid), None)
        if not target:
            raise KeyError(aid)
        for k in ("name", "trigger", "action", "enabled", "sprite", "tint"):
            if k in payload:
                target[k] = payload[k]
        _validate(target)
        _save_all(agents)
    _schedule_one(target)
    _emit("cron.updated", id=aid, name=target["name"])
    return target

def delete_agent(aid: str) -> None:
    try:
        from store import workers as W
        W.unregister(f"cron:{aid}")
    except Exception:
        pass
    with _lock:
        agents = [a for a in _load_all() if a["id"] != aid]
        _save_all(agents)
    if _scheduler and _scheduler.get_job(aid):
        _scheduler.remove_job(aid)
    _emit("cron.deleted", id=aid)

def run_now(aid: str) -> dict:
    a = get_agent(aid)
    if not a:
        raise KeyError(aid)
    return _run_action(a)

def get_next_run(aid: str) -> Optional[str]:
    if not _scheduler:
        return None
    job = _scheduler.get_job(aid)
    if not job or not job.next_run_time:
        return None
    return job.next_run_time.isoformat()

def get_state(aid: str) -> str:
    """Computed state for the pixel-art sprite."""
    if aid in _running_ids:
        return "working"
    a = get_agent(aid)
    if not a or not a.get("enabled", True):
        return "sleeping"
    nxt = get_next_run(aid)
    if nxt is None:
        return "sleeping"
    try:
        dt = datetime.fromisoformat(nxt) - datetime.now().astimezone(
            datetime.fromisoformat(nxt).tzinfo)
        secs = dt.total_seconds()
    except Exception:
        secs = 999
    if secs < 5:
        return "starting"
    if secs < 60:
        return "walking"
    return "idle"

def snapshot() -> list[dict]:
    """Full status snapshot for /api/cron/agents listing."""
    out = []
    for a in _load_all():
        out.append({**a,
                    "next_run": get_next_run(a["id"]),
                    "state": get_state(a["id"])})
    return out
