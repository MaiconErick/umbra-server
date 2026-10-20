"""
xUmbra hub (API v2) — chat turns that survive the phone.

Problems with v1 (/api/ws/chat) this fixes:
  * A turn lived inside its WebSocket: when Android paused the app or Wi-Fi
    blipped, the socket closed, the turn's task was cancelled and the reply
    never reached the phone (it only showed up after a manual reload).
  * Each chat needed its own socket, so the chat list had no idea a chat was
    busy and notifications needed a 15-minute WorkManager poll.
  * Callbacks were process-wide: two chats at once mixed their tool output.

Here every turn runs on its own thread, owned by the hub, and every event gets
a global sequence number and is kept in a per-chat ring buffer. Clients hold
ONE WebSocket (/api/v2/ws) and may disconnect at any time; on reconnect they
ask for the chat state (stored messages + the running turn's partial reply)
and then for events after the seq they already have.

Wire format (server → client), one JSON object per frame:
    {"seq": 812, "sid": "0053" | null, "type": "...", "data": {...}, "ts": 1789..}
Global events (sid may be set but every client receives them):
    hello, turn_state, message, session, notification, cron.*, queue
Chat events (only for clients watching that sid):
    assistant_delta, status, tool_start, tool_end, tool_progress, side_message,
    media_added, llm_request, llm_response, llm_error, turn.*, mem.*, ...
Client → server ops:
    {"op":"ping"} · {"op":"watch","sids":[..],"after":{sid:seq}} · {"op":"unwatch","sids":[..]}
    {"op":"send","sid","text","media_ids","client_id","tools","keep","include"}
    {"op":"cancel","sid"}
"""
from __future__ import annotations

import asyncio, collections, json, logging, os, sqlite3, threading, time, uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from api.registry import REGISTRY, set_chat_model, clear_chat_model, get_chat_model
from store import messages as msg_store
from store.manager import (
    create_session, update_session, get_session, get_chat_meta, set_chat_meta,
    load_chat_meta, delete_session_data, get_summary, get_token_usage,
)

logger = logging.getLogger(__name__)

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEDIA_DIR = os.path.join(BASE_DIR, "temp", "media")
API_TOKEN  = os.getenv("XUMBRA_API_TOKEN", "").strip()
# The tunnel's own key is accepted too, so a client that learned one secret can
# use it at home and away (api/remote.py does the same on the tunnel side).
REMOTE_KEY = os.getenv("UMBRA_REMOTE_KEY", "").strip()
VERSION   = "2.0"
BOOT      = uuid.uuid4().hex[:12]   # changes on every server start: clients resync

# Payload keys that are huge (whole prompt, tool schema) — kept in the
# per-session debug log on disk, never pushed over the socket.
_HEAVY_KEYS = ("system_text", "messages", "tools_schema", "segments")
_GLOBAL_TYPES = {"hello", "turn_state", "message", "session", "notification",
                 "queue", "session_deleted", "cron", "message_deleted", "worker"}


def _token_ok(tok: Optional[str]) -> bool:
    if not tok:
        return False
    return tok == API_TOKEN or (bool(REMOTE_KEY) and tok == REMOTE_KEY)


def check_token(tok: Optional[str]) -> bool:
    return not API_TOKEN or _token_ok(tok)


def require_auth(authorization: Optional[str]):
    if not API_TOKEN:
        return
    if not authorization or not authorization.startswith("Bearer ") or not _token_ok(authorization[7:].strip()):
        raise HTTPException(401, "invalid token")


class _Client:
    def __init__(self, name: str):
        self.id = uuid.uuid4().hex[:8]
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
        self.watching: set[str] = set()
        self.dead = False


class _Turn:
    def __init__(self, sid: str, text: str, source: str):
        self.id = "t-" + uuid.uuid4().hex[:10]
        self.sid = sid
        self.source = source
        self.text = text
        self.started = time.time()
        self.partial = ""          # reply text streamed since the last model call
        self.status = "Thinking"
        self.tool: Optional[str] = None
        self.tools: list[dict] = []
        self.agent = None

    def snapshot(self) -> dict:
        return {"id": self.id, "sid": self.sid, "source": self.source,
                "started": self.started, "partial": self.partial,
                "status": self.status, "tool": self.tool, "tools": self.tools[-30:],
                "elapsed": round(time.time() - self.started, 1)}


class Hub:
    def __init__(self):
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.lock = threading.RLock()
        self.seq = 0
        self.buffers: dict[str, collections.deque] = {}
        self.clients: set[_Client] = set()
        self.turns: dict[str, _Turn] = {}
        self.queues: dict[str, list[dict]] = {}
        self.dropped: dict[str, int] = {}   # newest seq evicted from each buffer

    # ── publishing ────────────────────────────────────────────────────
    def publish(self, sid: Optional[str], typ: str, data: Optional[dict] = None):
        data = dict(data or {})
        for k in _HEAVY_KEYS:
            data.pop(k, None)
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "sid": sid, "type": typ, "data": data, "ts": time.time()}
            if sid:
                buf = self.buffers.get(sid)
                if buf is None:
                    buf = self.buffers[sid] = collections.deque(maxlen=4000)
                if len(buf) == buf.maxlen:
                    self.dropped[sid] = buf[0]["seq"]
                buf.append(ev)
            targets = [c for c in self.clients
                       if not c.dead and (typ in _GLOBAL_TYPES or (sid and sid in c.watching))]
        if not targets or not self.loop:
            return ev
        try:
            payload = json.dumps(ev, ensure_ascii=False, default=str)
        except Exception:
            payload = json.dumps({**ev, "data": {"unserializable": True}})
        def _deliver():
            for c in targets:
                try:
                    c.queue.put_nowait(payload)
                except asyncio.QueueFull:
                    c.dead = True   # it stopped reading: drop it, it will resync
        try:
            self.loop.call_soon_threadsafe(_deliver)
        except RuntimeError:
            pass
        return ev

    def replay(self, client: _Client, sid: str, after: int) -> bool:
        """Queue buffered events of `sid` with seq > after. False = gap (resync)."""
        with self.lock:
            buf = list(self.buffers.get(sid) or [])
            if after < self.dropped.get(sid, 0):
                return False
        for ev in buf:
            if ev["seq"] > after:
                try:
                    client.queue.put_nowait(json.dumps(ev, ensure_ascii=False, default=str))
                except asyncio.QueueFull:
                    client.dead = True
                    break
        return True

    # ── turns ─────────────────────────────────────────────────────────
    def busy(self) -> dict:
        with self.lock:
            return {sid: t.snapshot() for sid, t in self.turns.items()}

    def turn_state(self, sid: str) -> Optional[dict]:
        with self.lock:
            t = self.turns.get(sid)
            return t.snapshot() if t else None

    def send(self, sid: str, text: str, media_ids: Optional[list] = None,
             client_id: Optional[str] = None, tools: Optional[list] = None,
             keep: bool = True, include: Optional[list] = None,
             source: str = "app", resume: bool = False) -> dict:
        """Start (or queue) a turn. Idempotent per client_id."""
        text = text or ""
        media_ids = [m for m in (media_ids or []) if m and "/" not in m and ".." not in m]
        if not text.strip() and not media_ids:
            raise ValueError("empty message")
        if not get_session(sid):
            raise KeyError(sid)
        if client_id:
            prev = msg_store.find_client_id(sid, client_id)
            if prev:
                return {"status": "duplicate", "message": prev}
            with self.lock:
                if any(q.get("client_id") == client_id for q in self.queues.get(sid, [])):
                    return {"status": "queued"}
                t = self.turns.get(sid)
                if t and getattr(t, "client_id", None) == client_id:
                    return {"status": "running", "turn": t.snapshot()}
        job = {"text": text, "media_ids": media_ids, "client_id": client_id,
               "tools": tools, "keep": keep, "include": include, "source": source,
               "resume": bool(resume)}
        with self.lock:
            if sid in self.turns:
                self.queues.setdefault(sid, []).append(job)
                self.publish(sid, "queue", {"sid": sid, "queued": list(self.queues[sid])})
                return {"status": "queued", "position": len(self.queues[sid])}
            turn = self._start(sid, job)
        return {"status": "started", "turn": turn.snapshot()}

    def cancel(self, sid: str, clear_queue: bool = True) -> bool:
        with self.lock:
            t = self.turns.get(sid)
            if clear_queue and self.queues.get(sid):
                self.queues[sid] = []
                self.publish(sid, "queue", {"sid": sid, "queued": []})
        if t and t.agent is not None:
            t.agent.request_cancel()
            t.status = "Stopping"
            self.publish(sid, "status", {"text": "Stopping…"})
            return True
        return False

    def unqueue(self, sid: str, client_id: str) -> bool:
        with self.lock:
            q = self.queues.get(sid) or []
            keep = [j for j in q if j.get("client_id") != client_id]
            if len(keep) == len(q):
                return False
            self.queues[sid] = keep
            self.publish(sid, "queue", {"sid": sid, "queued": keep})
            return True

    def _start(self, sid: str, job: dict) -> _Turn:
        # caller holds self.lock
        turn = _Turn(sid, job["text"], job.get("source") or "app")
        turn.client_id = job.get("client_id")
        self.turns[sid] = turn
        self.publish(sid, "turn_state", {"sid": sid, "busy": True, "turn": turn.snapshot()})
        th = threading.Thread(target=self._run, args=(turn, job), daemon=True,
                              name=f"turn-{sid}")
        th.start()
        return turn

    def run_blocking(self, sid: str, text: str, tools: Optional[list] = None,
                     source: str = "cron", timeout: float = 900) -> dict:
        """For cron prompt agents: wait for the chat to be free, run a turn
        through the hub (so the app sees it live) and return the reply record."""
        deadline = time.time() + timeout
        while True:
            with self.lock:
                if sid not in self.turns:
                    turn = _Turn(sid, text, source)
                    turn.client_id = None
                    self.turns[sid] = turn
                    self.publish(sid, "turn_state", {"sid": sid, "busy": True,
                                                    "turn": turn.snapshot()})
                    break
            if time.time() > deadline:
                raise TimeoutError("chat stayed busy")
            time.sleep(1.0)
        job = {"text": text, "media_ids": [], "client_id": None, "tools": tools or [],
               "keep": True, "include": None, "source": source}
        return self._run(turn, job) or {}

    def _run(self, turn: _Turn, job: dict) -> Optional[dict]:
        sid = turn.sid
        # Route "claude" persona to the CLI proxy agent instead of the normal LLM pipeline
        meta = get_chat_meta(sid) if sid else {}
        if (meta.get("persona") or "").strip().lower() == "claude":
            from agent.claude_cli import ClaudeCliAgent
            agent = ClaudeCliAgent()
            agent.bind_session(sid)
        else:
            agent = REGISTRY.get(sid)
        turn.agent = agent
        result: dict = {}

        def on_event(ev: dict):
            typ = ev.get("type") or "event"
            data = ev.get("data") or {}
            if typ == "assistant_delta":
                turn.partial += data.get("text", "")
            elif typ == "llm_request":
                turn.partial = ""
                turn.status = "Thinking"
            elif typ == "tool_start":
                turn.tool = data.get("tool")
                turn.status = f"Running {turn.tool}"
                turn.tools.append({"name": turn.tool, "args": data.get("args"),
                                   "call_id": data.get("call_id"), "note": data.get("note")})
            elif typ == "tool_end":
                turn.tool = None
                turn.status = "Thinking"
                for t in reversed(turn.tools):
                    if t.get("call_id") == data.get("call_id"):
                        t["result"] = str(data.get("result", ""))[:300]
                        break
            elif typ == "final":
                result["message"] = data.get("message")
                result["text"] = data.get("text")
            elif typ == "message":
                rec = data.get("record") or {}
                self.publish(sid, "message", {"sid": sid, "message": rec})
                return
            elif typ in ("session_updated", "session_created", "chat_categorized",
                         "mem.summary.refresh:done"):
                self.publish(sid, "session", {"sid": sid, **session_row(sid)})
            self.publish(sid, typ, data)

        def on_status(text: str):
            turn.status = (text or "").split("\n")[0][:120]
            self.publish(sid, "status", {"text": text})

        def on_image(d: dict):
            self.publish(sid, "image_progress", {"sid": sid, **d})

        def on_output(line: str):
            self.publish(sid, "tool_progress", {"line": line})

        def on_send(text: str):
            self.publish(sid, "side_message", {"text": text})

        agent.set_event_callback(on_event)
        agent.set_status_callback(on_status)
        agent.set_send_callback(on_send)
        agent.set_file_callback(None)   # media arrives via the media_added event
        agent.turn_client_id = job.get("client_id")
        agent.turn_source = job.get("source") or "app"
        try:
            from agent import tools as tools_mod
            tools_mod.set_thread_output_callback(on_output)
            tools_mod.set_image_progress(on_image)
        except Exception:
            pass

        paths = [os.path.join(MEDIA_DIR, m) for m in job.get("media_ids") or []
                 if os.path.exists(os.path.join(MEDIA_DIR, m))]
        include = set(job["include"]) if job.get("include") else None
        chat_kwargs = dict(include=include,
                           keep_in_memory=bool(job.get("keep", True)),
                           media_paths=paths or None,
                           selected_tools=job.get("tools"))
        # A resumed turn re-runs a message that is already in the history:
        # storing it again would show the same user bubble twice.
        if job.get("resume") and type(agent).__name__ != "ClaudeCliAgent":
            chat_kwargs["store_user"] = False
        try:
            with REGISTRY.lock_for(sid):
                agent.chat(job["text"], **chat_kwargs)
        except Exception as e:
            logger.exception(f"turn failed in {sid}")
            try:
                rec = msg_store.append(sid, {"role": "error",
                                             "text": f"Something broke on the server: {type(e).__name__}: {e}"})
                self.publish(sid, "message", {"sid": sid, "message": rec})
            except Exception:
                pass
        finally:
            agent.set_event_callback(None)
            agent.set_status_callback(None)
            agent.set_send_callback(None)
            agent.turn_client_id = None
            try:
                from agent import tools as tools_mod
                tools_mod.set_thread_output_callback(None)
                tools_mod.set_image_progress(None)
            except Exception:
                pass
            nxt = None
            with self.lock:
                self.turns.pop(sid, None)
                q = self.queues.get(sid) or []
                if q:
                    nxt = q.pop(0)
                    self.publish(sid, "queue", {"sid": sid, "queued": list(q)})
                self.publish(sid, "turn_state", {"sid": sid, "busy": False,
                                                 "turn_id": turn.id,
                                                 "elapsed": round(time.time() - turn.started, 1)})
                self.publish(sid, "session", {"sid": sid, **session_row(sid)})
                if nxt:
                    self._start(sid, nxt)
            # A restart requested during the turn runs now that nothing is
            # being written. If a queued turn just started, this is a no-op
            # and that turn's own finally block calls us again.
            try:
                from store import restart as restart_mod
                restart_mod.run_if_idle(self.is_busy)
            except Exception:
                logger.warning("deferred restart check failed", exc_info=True)
        return result.get("message")

    # ── surviving a restart ───────────────────────────────────────────
    def is_busy(self) -> bool:
        with self.lock:
            return bool(self.turns) or any(self.queues.values())

    def flush_partials(self) -> int:
        """Persist the reply text of every running turn.

        Called on shutdown: without it the answer the user is already reading
        is erased by the restart, because a partial reply only lives in memory.
        The record is flagged `interrupted` so a later resume knows the turn
        never finished its own message.
        """
        with self.lock:
            turns = list(self.turns.values())
        saved = 0
        for t in turns:
            text = (t.partial or "").strip()
            if not text:
                continue
            try:
                rec = msg_store.append(t.sid, {"role": "assistant", "text": text,
                                               "interrupted": True,
                                               "source": t.source or "app"})
                self.publish(t.sid, "message", {"sid": t.sid, "message": rec})
                saved += 1
            except Exception:
                logger.warning("could not save partial reply for %s", t.sid, exc_info=True)
        if saved:
            logger.info("saved %d partial reply(ies) before shutdown", saved)
        return saved

    def save_queues(self) -> None:
        """Snapshot queued messages. They are not in the history yet (a turn
        stores its own user message when it starts), so a restart would eat
        them without this."""
        try:
            with self.lock:
                snap = {sid: list(q) for sid, q in self.queues.items() if q}
            path = os.path.join(BASE_DIR, "data", "chats", "queue_snapshot.json")
            if not snap:
                try:
                    os.remove(path)
                except OSError:
                    pass
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"at": time.time(), "queues": snap}, f, default=str)
        except Exception:
            logger.warning("could not snapshot the queue", exc_info=True)

    def restore_queues(self, max_age: float = 6 * 3600) -> int:
        """Re-queue what `save_queues` captured, then start the first job."""
        path = os.path.join(BASE_DIR, "data", "chats", "queue_snapshot.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            os.remove(path)
        except Exception:
            return 0
        if time.time() - float(data.get("at") or 0) > max_age:
            return 0
        started = 0
        for sid, jobs in (data.get("queues") or {}).items():
            jobs = [j for j in jobs if isinstance(j, dict) and (j.get("text") or "").strip()]
            if not jobs or not get_session(sid):
                continue
            with self.lock:
                if sid in self.turns:
                    self.queues.setdefault(sid, []).extend(jobs)
                    continue
                self._start(sid, jobs[0])
                if jobs[1:]:
                    self.queues[sid] = jobs[1:]
                started += 1
        if started:
            logger.info("restored %d queued message(s) from before the restart", started)
        return started

    def resume_unanswered(self, max_age: float = 6 * 3600,
                          max_sessions: int = 3, max_attempts: int = 2) -> list:
        """Re-run the last message of any chat whose turn died with the server.

        A message is only written to the history when its turn starts, so a
        chat whose last row is still a user message had a turn that never
        answered — a restart killed it. Without this the user has to type
        "continue" after every restart by hand.
        """
        msgs_dir = os.path.join(BASE_DIR, "sessions", "msgs")
        try:
            names = sorted((n for n in os.listdir(msgs_dir) if n.endswith(".jsonl")),
                           key=lambda n: os.path.getmtime(os.path.join(msgs_dir, n)),
                           reverse=True)[:15]
        except OSError:
            return []
        resumed = []
        for name in names:
            if len(resumed) >= max_sessions:
                break
            sid = name[:-len(".jsonl")]
            try:
                if sid in self.turns:
                    continue
                meta = get_chat_meta(sid) or {}
                if (meta.get("persona") or "").strip().lower() == "claude":
                    continue
                items = msg_store.list_messages(sid)
                last = None
                for r in reversed(items):
                    if r.get("interrupted"):
                        continue        # a draft saved at shutdown, not an answer
                    last = r
                    break
                if not last or last.get("role") != "user":
                    continue
                if str(last.get("source") or "").startswith("cron"):
                    continue            # a cron prompt is not a chat message
                if time.time() - float(last.get("ts") or 0) > max_age:
                    continue
                text = (last.get("text") or "").strip()
                if not text:
                    continue
                attempts = int(last.get("resume_attempts") or 0)
                if attempts >= max_attempts:
                    continue
                msg_store.update(sid, last["id"], resume_attempts=attempts + 1)
                self.send(sid, text, media_ids=last.get("media") or None,
                          source="resume", resume=True)
                logger.info("resuming unfinished %s (attempt %d)", sid, attempts + 1)
                resumed.append(sid)
            except Exception:
                logger.warning("resume check failed for %s", sid, exc_info=True)
        return resumed


HUB = Hub()


# ──────────────────────────────────────────────────────────────────────
#  Session rows
# ──────────────────────────────────────────────────────────────────────
def _cost_by_sid() -> dict:
    try:
        from store.usage import USAGE_DB
        c = sqlite3.connect(USAGE_DB)
        rows = c.execute("SELECT sid, SUM(input_cost+output_cost+cache_read_cost+cache_write_cost),"
                         " SUM(CASE WHEN COALESCE(category,'chat')='chat' THEN 1 ELSE 0 END),"
                         " SUM(input_tokens+cache_read_tokens+cache_write_tokens), SUM(output_tokens)"
                         " FROM turns GROUP BY sid").fetchall()
        c.close()
        return {r[0]: {"cost_usd": round(r[1] or 0, 6), "turns": r[2] or 0,
                       "tokens_in": int(r[3] or 0), "tokens_out": int(r[4] or 0)} for r in rows}
    except Exception:
        return {}


def _privacy(meta: dict) -> dict:
    """secret = only in the vault; vanish = messages vanish in the app (persona option or per-chat switch)."""
    opts = {}
    try:
        if (meta.get("persona") or "").strip():
            from agent import personas as _pm
            opts = (_pm.get_persona(meta["persona"].strip()) or {}).get("options") or {}
    except Exception:
        opts = {}
    return {"secret": bool(opts.get("secret") or meta.get("secret")),
            "vanish": bool(meta["vanish"]) if meta.get("vanish") is not None else bool(opts.get("vanish"))}


def session_row(sid: str, meta: Optional[dict] = None, costs: Optional[dict] = None) -> dict:
    s = get_session(sid) or {}
    meta = meta if meta is not None else get_chat_meta(sid)
    last = None
    n = 0
    try:
        items = msg_store.list_messages(sid)
        n = len(items)
        for r in reversed(items):
            if r.get("role") in ("user", "assistant", "error"):
                last = r
                break
    except Exception:
        pass
    read_ts = float(meta.get("read_ts") or 0)
    unread = 0
    if n:
        for r in reversed(items):
            if float(r.get("ts", 0)) <= read_ts:
                break
            if r.get("role") == "assistant":
                unread += 1
    ts = (last or {}).get("ts") or _iso_ts(s.get("updated") or s.get("created"))
    row = {
        "id": sid,
        "summary": s.get("summary") or "",
        "status": s.get("status") or "INCOMPLETE",
        "created": _iso_ts(s.get("created")),
        "ts": ts,
        "count": n,
        "unread": unread,
        "last": ({"role": last.get("role"), "text": (last.get("text") or "")[:160],
                  "media": len(last.get("media") or []), "ts": last.get("ts")} if last else None),
        "starred": bool(meta.get("starred")),
        "hidden": bool(meta.get("hidden")),
        "persona": meta.get("persona") or None,
        "group": meta.get("group") or None,
        "tags": meta.get("tags") or [],
        "custom_icon": meta.get("custom_icon") or None,
        "proactive": bool(meta.get("allow_proactive")),
        "model": get_chat_model(sid),
        **_privacy(meta),
    }
    if costs is None:
        costs = _cost_by_sid()
    row.update(costs.get(sid) or {"cost_usd": 0, "turns": 0})
    with HUB.lock:
        t = HUB.turns.get(sid)
        row["busy"] = bool(t)
        row["queued"] = len(HUB.queues.get(sid) or [])
    return row


def _iso_ts(v) -> float:
    if not v:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(v).replace("Z", "")).timestamp()
    except Exception:
        return 0.0


def chat_settings(sid: str) -> dict:
    m = get_chat_meta(sid)
    s = get_session(sid) or {}
    return {
        "summary": s.get("summary") or "",
        "status": s.get("status") or "INCOMPLETE",
        "persona": m.get("persona") or None,
        "model": get_chat_model(sid),
        "reasoning": m.get("reasoning_effort") or None,
        "image_model": m.get("default_image_model") or None,
        "tools": m.get("enabled_tools") or [],
        "loaded_tools": m.get("loaded_tools") or [],
        "sys_primary": m.get("sys_primary") or "",
        "sys_secondary": m.get("sys_secondary") or "",
        "pinned_topics": m.get("pinned_topics") or [],
        "proactive": bool(m.get("allow_proactive")),
        "starred": bool(m.get("starred")),
        "hidden": bool(m.get("hidden")),
        "group": m.get("group") or None,
        "tags": m.get("tags") or [],
        "custom_icon": m.get("custom_icon") or None,
        "show_shell": bool(m.get("show_shell", True)),
        "read_ts": float(m.get("read_ts") or 0),
        "effective_model": effective_model(sid, m),
        "web_search_model": m.get("web_search_model") or None,
        "search_direct": m.get("search_direct"),
        "reddit_mode": m.get("reddit_mode") or None,
        "vanish": _privacy(m)["vanish"],
        # The task list the todo tool keeps for this chat.
        "todos": m.get("todos") or [],
        **_image_state(sid, m),
    }


def _last_image(sid: str) -> dict | None:
    """The newest generate_image call in this chat: {prompt, model}."""
    try:
        items = msg_store.list_messages(sid)
    except Exception:
        return None
    img = lambda n: n.rsplit(".", 1)[-1].lower() in ("png", "jpg", "jpeg", "webp", "gif", "bmp")
    for i in range(len(items) - 1, -1, -1):
        r = items[i]
        for t in reversed(((r.get("turn") or {}).get("tools") or [])):
            if t.get("name") == "generate_image":
                a = t.get("args") or {}
                if not a.get("prompt"):
                    continue
                # the picture(s) that call worked from, so "paint again" keeps the reference
                how = (a.get("reference") or "").lower()
                how = {"attached": "edit", "last_result": "edit", "previous": "original"}.get(how, how)
                ref_names = [n for n in (a.get("ref_media") or []) if os.path.exists(os.path.join(MEDIA_DIR, n))]
                if not ref_names and how != "none" and "ref_media" not in a:
                    role = "user" if how == "original" else None
                    for k in range(i - 1, max(-1, i - 20), -1):
                        if (role is None or items[k].get("role") == role) and any(img(n) for n in items[k].get("media") or []):
                            ref_names = [n for n in items[k]["media"] if img(n)]
                            break
                        if how == "" and items[k].get("role") == "user":
                            break   # old calls without the arg: only the user's own message counted
                return {"prompt": a["prompt"], "model": a.get("model") or (r.get("turn") or {}).get("image_model"),
                        "ref_names": ref_names}
    return None


def _image_state(sid: str, m: dict) -> dict:
    last = _last_image(sid)
    return {"image_chat": bool((m.get("persona") or "") == "image" or m.get("default_image_model") or last),
            "last_image_prompt": (last or {}).get("prompt")}


def effective_model(sid: str, meta: Optional[dict] = None) -> dict:
    """Which model actually answers in this chat, and why."""
    meta = meta if meta is not None else get_chat_meta(sid)
    cm = get_chat_model(sid)
    if cm:
        return {**cm, "why": "chat"}
    pname = (meta.get("persona") or "").strip()
    if pname:
        try:
            from agent import personas as personas_mod
            raw = ((personas_mod.get_persona(pname) or {}).get("model") or "").strip()
            if "/" in raw:
                p, m = raw.split("/", 1)
                return {"provider": p, "model": m, "why": "persona"}
        except Exception:
            pass
    from agent.providers import get_current
    cur = get_current() or {}
    return {"provider": cur.get("provider", ""), "model": cur.get("model", ""), "why": "default"}


# ──────────────────────────────────────────────────────────────────────
#  REST
# ──────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/api/v2")


class SendBody(BaseModel):
    text: str = ""
    media_ids: list[str] = []
    client_id: Optional[str] = None
    tools: Optional[list[str]] = None
    keep: bool = True
    include: Optional[list[str]] = None


class ModelRef(BaseModel):
    provider: str
    model: str


class SettingsBody(BaseModel):
    summary: Optional[str] = None
    status: Optional[str] = None
    persona: Optional[str] = None          # "" clears
    model: Optional[ModelRef] = None
    clear_model: Optional[bool] = None
    reasoning: Optional[str] = None        # "" clears
    image_model: Optional[str] = None      # "" clears
    tools: Optional[list[str]] = None
    loaded_tools: Optional[list[str]] = None
    sys_primary: Optional[str] = None
    sys_secondary: Optional[str] = None
    pinned_topics: Optional[list[str]] = None
    proactive: Optional[bool] = None
    starred: Optional[bool] = None
    hidden: Optional[bool] = None
    group: Optional[str] = None
    tags: Optional[list[str]] = None
    custom_icon: Optional[str] = None
    show_shell: Optional[bool] = None
    web_search_model: Optional[ModelRef] = None
    clear_web_search_model: Optional[bool] = None
    search_direct: Optional[bool] = None
    clear_search_direct: Optional[bool] = None
    reddit_mode: Optional[str] = None          # auto | always | never | "" = app default
    vanish: Optional[bool] = None              # messages vanish word by word in the app
    # Claude CLI persona config (only used when persona == "claude")
    claude_host: Optional[str] = None          # "pi" | "pc"
    claude_provider: Optional[str] = None      # "subscription" | "openrouter" | "ghostcli"
    claude_model: Optional[str] = None         # model id for the CLI --model flag


class CreateBody(SettingsBody):
    first_message: Optional[str] = None


def _apply_settings(sid: str, b: SettingsBody):
    if b.summary is not None or b.status is not None:
        update_session(sid, (b.summary or "").strip() or None, b.status)
        if b.summary is not None:
            # a hand-picked title is never overwritten by the summary worker
            set_chat_meta(sid, title_locked=bool(b.summary.strip()))
    if b.persona is not None:
        from agent import personas as personas_mod
        val = b.persona.strip()
        if val and personas_mod._persona_dir(val) is None:
            raise HTTPException(404, "persona not found")
        set_chat_meta(sid, persona=val)
        if val:
            pm = personas_mod._persona_meta(val) or {}
            if pm.get("needs_proactive"):
                set_chat_meta(sid, allow_proactive=True)
            want = pm.get("enable_tools") or []
            if want and b.tools is None:
                cur = get_chat_meta(sid).get("enabled_tools") or []
                set_chat_meta(sid, enabled_tools=sorted(set(cur) | set(want)))
    if b.clear_model:
        clear_chat_model(sid)
        REGISTRY.get(sid).set_model_override(None, None)
    elif b.model is not None:
        set_chat_model(sid, b.model.provider, b.model.model)
        REGISTRY.get(sid).set_model_override(b.model.provider, b.model.model)
    if b.reasoning is not None:
        v = b.reasoning.strip().lower()
        if v and v not in ("minimal", "low", "medium", "high"):
            raise HTTPException(400, "reasoning must be minimal|low|medium|high")
        set_chat_meta(sid, reasoning_effort=v)
    if b.image_model is not None:
        set_chat_meta(sid, default_image_model=b.image_model.strip())
    fields: dict[str, Any] = {}
    if b.tools is not None: fields["enabled_tools"] = list(b.tools)
    if b.loaded_tools is not None: fields["loaded_tools"] = list(b.loaded_tools)
    if b.sys_primary is not None: fields["sys_primary"] = b.sys_primary
    if b.sys_secondary is not None: fields["sys_secondary"] = b.sys_secondary
    if b.pinned_topics is not None: fields["pinned_topics"] = list(b.pinned_topics)
    if b.proactive is not None: fields["allow_proactive"] = bool(b.proactive)
    if b.starred is not None: fields["starred"] = bool(b.starred)
    if b.hidden is not None: fields["hidden"] = bool(b.hidden)
    if b.group is not None: fields["group"] = b.group.strip()
    if b.tags is not None: fields["tags"] = [t.strip() for t in b.tags if t.strip()]
    if b.custom_icon is not None: fields["custom_icon"] = b.custom_icon.strip()
    if b.show_shell is not None: fields["show_shell"] = bool(b.show_shell)
    if b.web_search_model is not None:
        fields["web_search_model"] = {"provider": b.web_search_model.provider, "model": b.web_search_model.model}
    if b.search_direct is not None: fields["search_direct"] = bool(b.search_direct)
    if b.reddit_mode is not None:
        fields["reddit_mode"] = b.reddit_mode if b.reddit_mode in ("auto", "always", "never") else ""
    if b.vanish is not None:
        fields["vanish"] = bool(b.vanish)
    # Claude CLI persona config
    if getattr(b, "claude_host", None) is not None:
        fields["claude_host"] = b.claude_host.strip()
    if getattr(b, "claude_provider", None) is not None:
        fields["claude_provider"] = b.claude_provider.strip()
    if getattr(b, "claude_model", None) is not None:
        fields["claude_model"] = b.claude_model.strip()
    if b.clear_web_search_model or b.clear_search_direct:
        from store.manager import _load_chat_meta, _save_chat_meta, _lock as _mlock
        with _mlock:
            d = _load_chat_meta(); cm = d.get(sid, {})
            if b.clear_web_search_model: cm.pop("web_search_model", None)
            if b.clear_search_direct: cm.pop("search_direct", None)
            d[sid] = cm; _save_chat_meta(d)
    if fields:
        set_chat_meta(sid, **fields)


@router.get("/info")
def info(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return {"version": VERSION, "boot": BOOT, "seq": HUB.seq, "busy": HUB.busy(),
            "time": time.time(), "auth": bool(API_TOKEN)}


@router.get("/sessions")
def sessions(include_hidden: bool = False, vault: bool = False, authorization: Optional[str] = Header(None)):
    """vault=false: the normal list (no secret chats). vault=true: only the secret ones."""
    require_auth(authorization)
    from store.manager import _load_sessions_index
    idx = _load_sessions_index()
    meta_all = load_chat_meta()
    costs = _cost_by_sid()
    rows = []
    for sid in idx.keys():
        meta = meta_all.get(sid, {})
        if meta.get("hidden") and not include_hidden:
            continue
        row = session_row(sid, meta, costs)
        if row["secret"] != vault:
            continue
        rows.append(row)
    rows.sort(key=lambda r: (0 if r["starred"] else 1, -(r["ts"] or 0)))
    return {"sessions": rows, "seq": HUB.seq}


@router.post("/sessions")
def sessions_create(body: CreateBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    sid = create_session((body.summary or "").strip() or "new chat")
    body.summary = None
    _apply_settings(sid, body)
    row = session_row(sid)
    HUB.publish(sid, "session", {"sid": sid, **row})
    if body.first_message and body.first_message.strip():
        HUB.send(sid, body.first_message, client_id="first-" + uuid.uuid4().hex[:8])
    return {"session": row, "settings": chat_settings(sid)}


@router.get("/sessions/{sid}/state")
def session_state(sid: str, limit: int = 80, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    if not get_session(sid):
        raise HTTPException(404, "session not found")
    with HUB.lock:
        seq = HUB.seq
        turn = HUB.turns.get(sid)
        snap = turn.snapshot() if turn else None
        queued = list(HUB.queues.get(sid) or [])
    items = msg_store.list_messages(sid)
    return {
        "seq": seq,
        "boot": BOOT,
        "session": session_row(sid),
        "settings": chat_settings(sid),
        "messages": items[-limit:] if limit else items,
        "has_more": limit > 0 and len(items) > limit,
        "turn": snap,
        "queued": queued,
        "summary_text": get_summary(sid, silent=True) or "",
    }


@router.get("/sessions/{sid}/messages")
def session_messages(sid: str, before: Optional[float] = None, limit: int = 80,
                     authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    items = msg_store.list_messages(sid, before=before)
    more = len(items) > limit
    return {"messages": items[-limit:], "has_more": more}


@router.get("/fx")
def fx_rate(authorization: Optional[str] = Header(None)):
    """USD -> BRL, so the app can print "(R$ 1.35)" next to every dollar."""
    require_auth(authorization)
    from store import fx
    return fx.rate()


@router.post("/sessions/{sid}/send")
def session_send(sid: str, body: SendBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    try:
        return HUB.send(sid, body.text, body.media_ids, body.client_id, body.tools,
                        body.keep, body.include)
    except KeyError:
        raise HTTPException(404, "session not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/sessions/{sid}/cancel")
def session_cancel(sid: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return {"cancelled": HUB.cancel(sid)}


@router.delete("/sessions/{sid}/queue/{client_id}")
def session_unqueue(sid: str, client_id: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return {"removed": HUB.unqueue(sid, client_id)}


@router.post("/sessions/{sid}/read")
def session_read(sid: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    set_chat_meta(sid, read_ts=time.time())
    row = session_row(sid)
    HUB.publish(sid, "session", {"sid": sid, **row})
    return row


@router.patch("/sessions/{sid}/settings")
def session_settings(sid: str, body: SettingsBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    if not get_session(sid):
        raise HTTPException(404, "session not found")
    _apply_settings(sid, body)
    row = session_row(sid)
    HUB.publish(sid, "session", {"sid": sid, **row})
    return {"session": row, "settings": chat_settings(sid)}


@router.delete("/sessions/{sid}")
def session_delete(sid: str, wipe: bool = False, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    HUB.cancel(sid)
    if wipe:
        delete_session_data(sid)
        msg_store.wipe(sid)
        try:
            os.remove(os.path.join(BASE_DIR, "sessions", "events", f"{sid}.jsonl"))
        except OSError:
            pass
        REGISTRY.drop(sid)
    else:
        set_chat_meta(sid, hidden=True)
    HUB.publish(sid, "session_deleted", {"sid": sid, "wiped": wipe})
    return {"ok": True}


@router.delete("/sessions/{sid}/messages/{mid}")
def message_delete(sid: str, mid: str, authorization: Optional[str] = Header(None)):
    """Removes a message from the app history and from what the model sees next
    (the agent is rebuilt from the store). The legacy .txt log is untouched."""
    require_auth(authorization)
    with HUB.lock:
        if sid in HUB.turns:
            raise HTTPException(409, "chat is busy")
    ok = msg_store.delete(sid, mid)
    if ok:
        REGISTRY.drop(sid)
        HUB.publish(sid, "message_deleted", {"sid": sid, "id": mid})
    return {"ok": ok}


@router.post("/sessions/{sid}/retry")
def session_retry(sid: str, authorization: Optional[str] = Header(None)):
    """Re-send the last user message (after an error or a bad reply). The old
    reply/error stays in the history so nothing is lost."""
    require_auth(authorization)
    items = msg_store.list_messages(sid)
    last_user = next((r for r in reversed(items) if r.get("role") == "user"), None)
    if not last_user:
        raise HTTPException(400, "nothing to retry")
    return HUB.send(sid, last_user.get("text") or "", last_user.get("media") or [],
                    client_id="retry-" + uuid.uuid4().hex[:8],
                    keep=not last_user.get("ephemeral"))


@router.get("/img-proxy")
def img_proxy(u: str, authorization: Optional[str] = Header(None)):
    """Pictures the model put in a reply (Steam/Wikipedia…), fetched with a proper
    User-Agent and cached on the Pi so the phone never hotlinks."""
    require_auth(authorization)
    import hashlib, requests
    from fastapi.responses import FileResponse
    if not u.startswith(("https://", "http://")):
        raise HTTPException(400, "bad url")
    d = os.path.join(BASE_DIR, "temp", "imgcache")
    os.makedirs(d, exist_ok=True)
    f = os.path.join(d, hashlib.sha1(u.encode()).hexdigest())
    if not os.path.exists(f):
        r = requests.get(u, timeout=15, headers={"User-Agent": "UmbraBot/1.0 (personal assistant on a Raspberry Pi)"})
        ct = r.headers.get("content-type", "")
        if r.status_code != 200 or not ct.startswith("image/") or len(r.content) > 15_000_000:
            raise HTTPException(502, f"upstream {r.status_code} {ct}")
        with open(f + ".tmp", "wb") as fh:
            fh.write(r.content)
        with open(f + ".ct", "w") as fh:
            fh.write(ct)
        os.replace(f + ".tmp", f)
    ct = open(f + ".ct").read() if os.path.exists(f + ".ct") else "image/jpeg"
    return FileResponse(f, media_type=ct, headers={"Cache-Control": "max-age=604800"})


@router.get("/sessions/{sid}/memory-log")
def memory_log(sid: str, authorization: Optional[str] = Header(None)):
    """What was written to the memory files during this chat (by Umbra or the note-taker)."""
    require_auth(authorization)
    from store import notebook as NB
    return {"changes": NB.history(sid)}


@router.get("/workers")
def workers_list(authorization: Optional[str] = Header(None)):
    """Background agents and what they're doing right now (the app's pixel rooms)."""
    require_auth(authorization)
    from store import workers as W
    return {"workers": W.snapshot(), "now": time.time()}


_ROOMS = os.path.join(BASE_DIR, "data", "settings", "rooms.json")


def _rooms() -> dict:
    try:
        return json.load(open(_ROOMS, encoding="utf-8"))
    except Exception:
        return {}


@router.get("/rooms")
def rooms_get(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return {"rooms": _rooms()}


@router.put("/rooms/{wid}")
def rooms_put(wid: str, body: dict, authorization: Optional[str] = Header(None)):
    """One room's layout: {"items": [{"type","x","y","rot"}], "style": {...}} — or {} to go back to the default."""
    require_auth(authorization)
    r = _rooms()
    if body:
        r[wid] = body
    else:
        r.pop(wid, None)
    os.makedirs(os.path.dirname(_ROOMS), exist_ok=True)
    with open(_ROOMS + ".tmp", "w", encoding="utf-8") as f:
        json.dump(r, f)
    os.replace(_ROOMS + ".tmp", _ROOMS)
    return {"ok": True}


@router.get("/image-models")
def image_models(authorization: Optional[str] = Header(None)):
    """Image models for the chat's picker bar (≈ cost per picture + real average when known)."""
    require_auth(authorization)
    from agent import imagegen
    return {"models": imagegen.catalog(), "default": _default_image_model()}


def _default_image_model() -> str:
    """What the server uses when a chat has no image model picked."""
    try:
        from agent.model_roles import get_role
        r = get_role("image_gen") or {}
        if r.get("model"):
            return r["model"]
    except Exception:
        pass
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(BASE_DIR, "config.ini"))
    return cfg.get("image", "model", fallback="gpt-image-1")


class DefaultImageBody(BaseModel):
    id: str


@router.post("/image-models/default")
def image_model_default(body: DefaultImageBody, authorization: Optional[str] = Header(None)):
    """Make one image model the default for every chat without a pick."""
    require_auth(authorization)
    from agent import imagegen
    from agent.model_roles import set_role
    m = next((x for x in imagegen.catalog() if x["id"] == body.id), None)
    if not m:
        raise HTTPException(404, "unknown image model")
    set_role("image_gen", "openrouter" if m["provider"] == "code" else m["provider"], m["id"])
    return {"default": _default_image_model()}


@router.get("/image-samples/{name}")
def image_sample(name: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    from fastapi.responses import FileResponse
    from agent import imagegen
    f = os.path.join(imagegen.SAMPLES_DIR, os.path.basename(name))
    if not os.path.exists(f):
        raise HTTPException(404, "no sample")
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})


class RegenBody(BaseModel):
    model: Optional[str] = None    # default: the chat's image model
    prompt: Optional[str] = None   # default: the last image prompt in this chat


@router.post("/sessions/{sid}/regen-image")
def regen_image(sid: str, body: RegenBody, authorization: Optional[str] = Header(None)):
    """Same picture prompt, another model — straight to the image API, no chat-model call."""
    require_auth(authorization)
    last = _last_image(sid) or {}
    prompt = (body.prompt or "").strip() or (last.get("prompt") or "")
    refs = [os.path.join(MEDIA_DIR, n) for n in (last.get("ref_names") or []) if not body.prompt]
    if not prompt:
        raise HTTPException(400, "no image prompt in this chat yet")
    model = (body.model or "").strip() or (get_chat_meta(sid).get("default_image_model") or "")
    with HUB.lock:
        if sid in HUB.turns:
            raise HTTPException(409, "chat is busy")
        turn = _Turn(sid, prompt, "app")
        turn.client_id = None
        turn.status = "Painting"
        turn.tool = "generate_image"
        HUB.turns[sid] = turn
        HUB.publish(sid, "turn_state", {"sid": sid, "busy": True, "turn": turn.snapshot()})
    threading.Thread(target=_regen_run, args=(sid, turn, prompt, model, refs), daemon=True, name=f"regen-{sid}").start()
    return {"ok": True, "turn_id": turn.id}


def _regen_run(sid: str, turn, prompt: str, model: str, refs: list | None = None):
    import shutil
    from agent import tools as T
    t0 = time.time()
    try:
        HUB.publish(sid, "status", {"text": f"Painting with {model or 'the default image model'}"})
        T.set_current_sid(sid)
        T.set_image_progress(lambda d: HUB.publish(sid, "image_progress", {"sid": sid, **d}))
        path = T.tool_generate_image(prompt, model=model or None, references=refs or None)
        T._img_done(path)
        meta = T.pop_last_image_meta() or {}
        T.set_image_progress(None)
        T.set_current_sid(None)
        ok = bool(path) and os.path.exists(str(path))
        media = []
        if ok:
            os.makedirs(MEDIA_DIR, exist_ok=True)
            name = uuid.uuid4().hex + ".png"
            shutil.copy2(path, os.path.join(MEDIA_DIR, name))
            media = [name]
        cost = float(meta.get("cost") or 0)
        label = meta.get("model") or model
        try:
            from agent import imagegen
            nice = next((m["name"] for m in imagegen.catalog() if m["model"] == label or m["id"] == label), label)
        except Exception:
            nice = label
        text = (f"🎨 Same prompt, new model: **{nice}**" if ok else f"⚠️ {path}")
        if ok and meta.get("note"):
            text += "\n\n⚠️ " + meta["note"]
        rec = msg_store.append(sid, {
            "role": "assistant", "text": text, "media": media or None, "source": "app",
            "turn": {"provider": meta.get("provider") or "", "model": label,
                     "duration_sec": round(time.time() - t0, 2), "input_tokens": 0, "output_tokens": 0,
                     "cost_usd": round(cost, 6), "image_cost_usd": round(cost, 6) or None,
                     "regen": True, "image_model": label, "image_note": meta.get("note"),
                     "tools": [{"name": "generate_image", "args": {"prompt": prompt, "model": label,
                                                                     "reference": "edit" if refs else "none",
                                                                     "ref_media": [os.path.basename(r) for r in refs or []]},
                                "result": "ok" if ok else str(path)[:300],
                                "ms": int((time.time() - t0) * 1000)}]}})
        HUB.publish(sid, "message", {"sid": sid, "message": rec})
        REGISTRY.drop(sid)   # the model sees the new picture's note next turn
    except Exception as e:
        logger.exception("regen failed")
        HUB.publish(sid, "status", {"text": f"Image failed: {e}"})
    finally:
        nxt = None
        with HUB.lock:
            HUB.turns.pop(sid, None)
            q = HUB.queues.get(sid) or []
            if q:
                nxt = q.pop(0)
                HUB.publish(sid, "queue", {"sid": sid, "queued": list(q)})
            HUB.publish(sid, "turn_state", {"sid": sid, "busy": False, "turn_id": turn.id,
                                            "elapsed": round(time.time() - turn.started, 1)})
            HUB.publish(sid, "session", {"sid": sid, **session_row(sid)})
            if nxt:
                HUB._start(sid, nxt)


@router.get("/claude/ping")
def claude_ping(authorization: Optional[str] = Header(None)):
    """Live Pi↔PC latency for the Claude persona host selector."""
    require_auth(authorization)
    from agent.claude_cli import get_ping, start_ping_thread
    start_ping_thread()
    return get_ping()


@router.get("/search")
def search(q: str, limit: int = 60, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    q = (q or "").strip().lower()
    if not q:
        return {"hits": []}
    from store.manager import _load_sessions_index
    hits = []
    for sid, s in _load_sessions_index().items():
        if q in (s.get("summary") or "").lower():
            hits.append({"sid": sid, "id": None, "role": "title", "ts": _iso_ts(s.get("updated")),
                         "snippet": s.get("summary")})
        for r in msg_store.list_messages(sid):
            t = r.get("text") or ""
            i = t.lower().find(q)
            if i >= 0:
                hits.append({"sid": sid, "id": r.get("id"), "role": r.get("role"),
                             "ts": r.get("ts"),
                             "snippet": t[max(0, i - 60): i + 100].replace("\n", " ")})
    hits.sort(key=lambda h: -(h["ts"] or 0))
    return {"hits": hits[:limit]}


class PersonaCreate(BaseModel):
    name: str                      # folder slug: a-z 0-9 - _
    display_name: Optional[str] = None
    copy_from: Optional[str] = None


@router.post("/personas")
def persona_create(body: PersonaCreate, authorization: Optional[str] = Header(None)):
    """New persona folder with a starter README.md + PERSONA.md (or a copy of another)."""
    require_auth(authorization)
    import re, shutil
    from agent import personas as personas_mod
    slug = re.sub(r"[^a-z0-9_-]+", "-", (body.name or "").strip().lower()).strip("-")
    if not slug or not re.match(r"^[a-z0-9][a-z0-9_-]*$", slug):
        raise HTTPException(400, "name must use a-z, 0-9, - or _")
    dest = os.path.join(personas_mod.PERSONAS_DIR, slug)
    if os.path.exists(dest):
        raise HTTPException(409, "a persona with that name exists")
    src = personas_mod._persona_dir(body.copy_from) if body.copy_from else None
    if src:
        shutil.copytree(src, dest)
    else:
        os.makedirs(dest)
        title = (body.display_name or slug.replace("-", " ").title()).strip()
        with open(os.path.join(dest, "README.md"), "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n*One line about who {title} is.*\n\n"
                    f"**Best for** what you'd use this persona for.\n\n"
                    f"## Traits\n- First trait\n- Second trait\n")
        with open(os.path.join(dest, "PERSONA.md"), "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\nYou are {title}. Describe the voice, what you know, and how you answer.\n")
    return personas_mod.get_persona(slug) or {"name": slug}


@router.delete("/personas/{name}")
def persona_delete(name: str, authorization: Optional[str] = Header(None)):
    """Moves the persona folder to backups/ (never hard-deletes)."""
    require_auth(authorization)
    import shutil
    from agent import personas as personas_mod
    d = personas_mod._persona_dir(name)
    if not d:
        raise HTTPException(404, "persona not found")
    bdir = os.path.join(BASE_DIR, "backups", "personas")
    os.makedirs(bdir, exist_ok=True)
    shutil.move(d, os.path.join(bdir, f"{name}.{int(time.time())}"))
    return {"ok": True}



# ──────────────────────────────────────────────────────────────────────
#  Money, quotas, context, tools
# ──────────────────────────────────────────────────────────────────────
def _spend_by_provider() -> dict:
    """What Umbra itself recorded, per provider: today / 7 days / 30 days."""
    out: dict = {}
    try:
        from store.usage import USAGE_DB
        c = sqlite3.connect(USAGE_DB)
        now = time.time()
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        for label, since in (("today", midnight), ("week", now - 7 * 86400), ("month", now - 30 * 86400)):
            for prov, cost, n in c.execute(
                    "SELECT provider, SUM(input_cost+output_cost+cache_read_cost+cache_write_cost), COUNT(*) "
                    "FROM turns WHERE ts>=? GROUP BY provider", (since,)):
                out.setdefault(prov or "?", {})[label] = round(cost or 0, 6)
                out[prov or "?"][label + "_replies"] = n
        c.close()
    except Exception as e:
        logger.warning(f"spend query failed: {e}")
    return out


@router.get("/balances")
def balances(authorization: Optional[str] = Header(None)):
    """OpenRouter credits/limits (live) + what Umbra spent per provider."""
    require_auth(authorization)
    import requests as rq
    out: dict = {"spend": _spend_by_provider(), "openrouter": None}
    key = os.getenv("OPENROUTER_API_KEY", "")
    if key:
        try:
            h = {"Authorization": f"Bearer {key}"}
            cr = rq.get("https://openrouter.ai/api/v1/credits", headers=h, timeout=10).json().get("data", {})
            kd = rq.get("https://openrouter.ai/api/v1/key", headers=h, timeout=10).json().get("data", {})
            out["openrouter"] = {
                "total_credits": cr.get("total_credits"), "total_usage": cr.get("total_usage"),
                "remaining": (cr.get("total_credits") or 0) - (cr.get("total_usage") or 0),
                "key_limit": kd.get("limit"), "key_limit_remaining": kd.get("limit_remaining"),
                "key_limit_reset": kd.get("limit_reset"),
                "usage_daily": kd.get("usage_daily"), "usage_weekly": kd.get("usage_weekly"),
                "usage_monthly": kd.get("usage_monthly"),
            }
        except Exception as e:
            out["openrouter"] = {"error": str(e)[:200]}
    out["notes"] = {
        "openai": "OpenAI has no balance API for normal keys — see platform.openai.com/usage. The numbers here are what Umbra recorded.",
        "gemini": "Free tier: no money, daily request limits per model (see quota).",
    }
    return out


@router.get("/quota")
def quota(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    from store import quota as q
    return q.report()


@router.get("/sessions/{sid}/context")
def session_context(sid: str, authorization: Optional[str] = Header(None)):
    """Like Claude's /context: how full the model's window is and what fills it."""
    require_auth(authorization)
    from store.manager import get_prompt_snapshots
    items = msg_store.list_messages(sid)
    last = next((r for r in reversed(items) if r.get("role") == "assistant" and (r.get("turn") or {}).get("model")), None)
    t = (last or {}).get("turn") or {}
    model = (get_chat_model(sid) or {})
    prov = model.get("provider") or t.get("provider") or ""
    mid = model.get("model") or t.get("model") or ""
    if not (prov and mid):
        from agent.providers import get_current
        cur = get_current() or {}
        prov, mid = prov or cur.get("provider", ""), mid or cur.get("model", "")
    window = t.get("context_window") or 0
    if not window and prov and mid:
        try:
            from store.prices import context_window
            window = context_window(f"{prov}/{mid}")
        except Exception:
            window = 0
    snaps = get_prompt_snapshots(sid)
    breakdown = (snaps[-1].get("breakdown") if snaps else None) or t.get("breakdown") or {}
    used = t.get("context_tokens") or breakdown.get("total_estimated") or 0
    agent = REGISTRY._agents.get(sid)
    costs = _cost_by_sid().get(sid, {})
    return {
        "provider": prov, "model": mid, "window": window, "used": used,
        "percent": round(100.0 * used / window, 1) if window else None,
        "breakdown": breakdown,
        "messages_in_memory": len(agent.conversation) if agent else None,
        "messages_total": len(items),
        "session_cost_usd": costs.get("cost_usd", 0), "session_replies": costs.get("turns", 0),
        "session_tokens_in": costs.get("tokens_in", 0), "session_tokens_out": costs.get("tokens_out", 0),
        "last_reply": {"input_tokens": t.get("input_tokens"), "output_tokens": t.get("output_tokens"),
                        "cache_read_tokens": t.get("cache_read_tokens"), "cost_usd": t.get("cost_usd")},
    }


_TOOL_STATS: dict = {"ts": 0.0, "data": {}}


@router.get("/tools")
def tools_overview(authorization: Optional[str] = Header(None)):
    """Every tool Umbra has: always-on vs on-demand, parameters, how often used, where loaded."""
    require_auth(authorization)
    from agent import toolset
    from store.manager import _load_sessions_index
    if time.time() - _TOOL_STATS["ts"] > 60:
        stats: dict = {}
        for sid in _load_sessions_index().keys():
            for r in msg_store.list_messages(sid):
                for tl in ((r.get("turn") or {}).get("tools") or []):
                    st = stats.setdefault(tl.get("name") or "?", {"uses": 0, "last": 0, "chats": set()})
                    st["uses"] += 1
                    st["last"] = max(st["last"], float(r.get("ts") or 0))
                    st["chats"].add(sid)
        _TOOL_STATS.update(ts=time.time(), data=stats)
    stats = _TOOL_STATS["data"]
    meta_all = load_chat_meta()
    schemas = {t["function"]["name"]: t["function"] for t in toolset.build(set(toolset.LOADABLE))}
    rows = []
    for name, kind in [("memory", "always"), ("web_search", "always"), ("load_tools", "always")] + \
                      [(n, "on demand") for n in toolset.LOADABLE if n != "web_search"]:
        fn = schemas.get(name, {})
        st = stats.get(name, {})
        loaded_in = [sid for sid, m in meta_all.items()
                     if name in toolset.normalize((m.get("loaded_tools") or []) + (m.get("enabled_tools") or []))]
        rows.append({
            "name": name, "kind": kind,
            "description": toolset.LOADABLE.get(name) or (fn.get("description") or "").split("\n")[0],
            "details": fn.get("description", ""),
            "params": list(((fn.get("parameters") or {}).get("properties") or {}).keys()),
            "uses": st.get("uses", 0), "last_used": st.get("last") or None,
            "used_in_chats": len(st.get("chats", ())), "loaded_in": loaded_in,
        })
    return {"tools": rows}



# ──────────────────────────────────────────────────────────────────────
#  Usage drill-down, model names, app-wide settings
# ──────────────────────────────────────────────────────────────────────
CATEGORY_NAMES = {"chat": "Replies", "background": "Background (titles, memory)", "search": "Web search",
                  "voice": "Voice to text", "model_testing": "Model Testing",
                  "image": "Image generation"}


@router.get("/usage/breakdown")
def usage_breakdown(window: str = "today", authorization: Optional[str] = Header(None)):
    """Which chats spent money in the window, and each charge (reply / search / background / test)."""
    require_auth(authorization)
    from store.usage import USAGE_DB
    from store.manager import _load_sessions_index
    now = time.time()
    since = {"today": time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)), "24h": now - 86400,
             "7d": now - 7 * 86400, "30d": now - 30 * 86400}.get(window, 0)
    c = sqlite3.connect(USAGE_DB)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM turns WHERE ts>=? ORDER BY ts DESC", (since,)).fetchall()
    c.close()
    idx = _load_sessions_index()
    chats: dict = {}
    msgs_cache: dict = {}
    for r in rows:
        r = dict(r)
        sid = r.get("sid") or ""
        cost = (r["input_cost"] or 0) + (r["output_cost"] or 0) + (r["cache_read_cost"] or 0) + (r["cache_write_cost"] or 0)
        ch = chats.setdefault(sid, {"sid": sid, "title": (idx.get(sid) or {}).get("summary") or ("Not in a chat" if not sid else f"#{sid} (deleted)"),
                                    "cost": 0.0, "count": 0, "by_category": {}, "items": []})
        cat = r.get("category") or "chat"
        ch["cost"] += cost; ch["count"] += 1
        ch["by_category"][cat] = ch["by_category"].get(cat, 0.0) + cost
        preview = r.get("label") or ""
        if cat == "chat" and sid:
            if sid not in msgs_cache:
                msgs_cache[sid] = [m for m in msg_store.list_messages(sid) if m.get("role") == "assistant"]
            near = min(msgs_cache[sid], key=lambda m: abs(float(m.get("ts", 0)) - r["ts"]), default=None)
            if near and abs(float(near.get("ts", 0)) - r["ts"]) < 30:
                preview = (near.get("text") or "")[:120]
        ch["items"].append({"ts": r["ts"], "category": cat, "category_name": CATEGORY_NAMES.get(cat, cat),
                            "provider": r["provider"], "model": r["model"], "cost": round(cost, 6),
                            "input_tokens": (r["input_tokens"] or 0) + (r["cache_read_tokens"] or 0),
                            "output_tokens": r["output_tokens"] or 0, "preview": preview})
    out = sorted(chats.values(), key=lambda x: -x["cost"])
    totals: dict = {}
    for ch in out:
        for k, v in ch["by_category"].items():
            totals[k] = totals.get(k, 0.0) + v
    return {"window": window, "total": round(sum(x["cost"] for x in out), 6),
            "by_category": [{"category": k, "name": CATEGORY_NAMES.get(k, k), "cost": round(v, 6)} for k, v in sorted(totals.items(), key=lambda kv: -kv[1])],
            "chats": out}


class ModelKeys(BaseModel):
    keys: list[str]


SOURCE_NAMES = {"openrouter": "OpenRouter", "openai": "OpenAI", "gemini": "Google Gemini",
                "anthropic": "Anthropic", "ollama": "Ollama"}


@router.post("/models/info")
def models_info(body: ModelKeys, authorization: Optional[str] = Header(None)):
    """'<provider>/<model>' → {source, name, context, prices} for the app's model labels."""
    require_auth(authorization)
    from store import prices
    sheet_names = {}
    try:
        sheet_names = prices.names()
    except Exception:
        pass
    out = {}
    for k in body.keys[:300]:
        prov, _, model = k.partition("/")
        p = prices.lookup(k) or {}
        cand = model if prov == "openrouter" else (f"google/{model}" if prov == "gemini" else f"{prov}/{model}")
        name = sheet_names.get(cand.split(":")[0]) or model.split("/")[-1]
        name = name.split(": ", 1)[-1] if ": " in name else name
        vendor = (sheet_names.get(cand.split(":")[0]) or "").split(":")[0] if ":" in (sheet_names.get(cand.split(":")[0]) or "") else ""
        out[k] = {"source": SOURCE_NAMES.get(prov, prov), "name": (vendor + " " + name).strip() if vendor and not name.lower().startswith(vendor.lower()) else name,
                  "context": p.get("context") or 0, "input": p.get("input"), "output": p.get("output")}
    return {"models": out}


@router.get("/app-settings")
def app_settings_get(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    from store.manager import get_app_setting
    return {"search_direct": bool(get_app_setting("search_direct", False)),
            "smart_routing": bool(get_app_setting("smart_routing", False)),
            "reddit_mode": get_app_setting("reddit_mode", "auto") or "auto"}


@router.patch("/app-settings")
def app_settings_set(body: dict, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    from store.manager import set_app_setting
    for k in ("search_direct", "smart_routing"):
        if k in body:
            set_app_setting(k, bool(body[k]))
    if body.get("reddit_mode") in ("auto", "always", "never"):
        set_app_setting("reddit_mode", body["reddit_mode"])
    return app_settings_get(authorization)



# ──────────────────────────────────────────────────────────────────────
#  Media info (dimensions, size, length) for the chat bubbles
# ──────────────────────────────────────────────────────────────────────
_MEDIA_INFO: dict = {}


def _media_info(name: str) -> dict:
    if name in _MEDIA_INFO:
        return _MEDIA_INFO[name]
    path = os.path.join(MEDIA_DIR, name)
    info: dict = {"name": name, "exists": os.path.exists(path)}
    if not info["exists"]:
        return info
    ext = name.rsplit(".", 1)[-1].lower()
    info["size"] = os.path.getsize(path)
    info["kind"] = ("image" if ext in ("png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "avif") else
                    "audio" if ext in ("mp3", "ogg", "opus", "wav", "m4a", "aac", "flac") else
                    "video" if ext in ("mp4", "webm", "mov", "mkv", "3gp") else "file")
    info["format"] = ext.upper()
    if info["kind"] == "image":
        try:
            from PIL import Image
            with Image.open(path) as im:
                info["width"], info["height"] = im.size
                info["format"] = (im.format or ext).upper()
                info["frames"] = getattr(im, "n_frames", 1)
        except Exception:
            pass
    if info["kind"] in ("audio", "video"):
        try:
            import subprocess
            out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", path],
                                 capture_output=True, text=True, timeout=10).stdout
            j = json.loads(out or "{}")
            info["duration"] = float((j.get("format") or {}).get("duration") or 0)
            for st in j.get("streams") or []:
                if st.get("codec_type") == "video" and st.get("width"):
                    info["width"], info["height"] = st["width"], st["height"]
                    info["codec"] = st.get("codec_name")
                if st.get("codec_type") == "audio":
                    info["audio_codec"] = st.get("codec_name")
                    info["sample_rate"] = int(st.get("sample_rate") or 0)
                    info["channels"] = st.get("channels")
            br = (j.get("format") or {}).get("bit_rate")
            if br:
                info["bitrate"] = int(br)
        except Exception:
            pass
    _MEDIA_INFO[name] = info
    return info


@router.get("/media/info")
def media_info(names: str, authorization: Optional[str] = Header(None)):
    """names = comma-separated media file names (as in messages[].media)."""
    require_auth(authorization)
    out = {}
    for n in names.split(",")[:60]:
        n = n.strip()
        if n and "/" not in n and ".." not in n:
            out[n] = _media_info(n)
    return {"media": out}


# ──────────────────────────────────────────────────────────────────────
#  WebSocket
# ──────────────────────────────────────────────────────────────────────
@router.websocket("/ws")
async def ws(sock: WebSocket, token: Optional[str] = Query(None),
             client: Optional[str] = Query(None)):
    if not check_token(token):
        # Say why: a refused handshake used to be invisible on the server.
        logger.warning("ws refused: token=%s client=%s peer=%s",
                       "missing" if not token else f"len {len(token)}",
                       client, getattr(sock.client, "host", "?"))
        await sock.close(code=4401)
        return
    await sock.accept()
    if HUB.loop is None:
        HUB.loop = asyncio.get_running_loop()
    c = _Client(client or "app")
    with HUB.lock:
        HUB.clients.add(c)
    await sock.send_text(json.dumps({"seq": HUB.seq, "sid": None, "type": "hello",
                                     "data": {"version": VERSION, "boot": BOOT, "busy": HUB.busy(),
                                              "client": c.id, "time": time.time()}},
                                    default=str))

    async def writer():
        try:
            while not c.dead:
                payload = await c.queue.get()
                await asyncio.wait_for(sock.send_text(payload), timeout=20)
        except Exception:
            pass
        c.dead = True
        try:
            await sock.close()
        except Exception:
            pass

    wtask = asyncio.create_task(writer())
    try:
        while not c.dead:
            raw = await sock.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            op = msg.get("op")
            if op == "ping":
                c.queue.put_nowait(json.dumps({"seq": HUB.seq, "sid": None, "type": "pong",
                                               "data": {"t": msg.get("t"), "time": time.time()}}))
            elif op == "watch":
                after = msg.get("after") or {}
                for sid in msg.get("sids") or []:
                    c.watching.add(sid)
                    if sid in after:
                        if not HUB.replay(c, sid, int(after[sid] or 0)):
                            c.queue.put_nowait(json.dumps({"seq": HUB.seq, "sid": sid,
                                                           "type": "resync", "data": {}}))
            elif op == "unwatch":
                for sid in msg.get("sids") or []:
                    c.watching.discard(sid)
            elif op == "send":
                sid = msg.get("sid")
                try:
                    res = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: HUB.send(sid, msg.get("text") or "", msg.get("media_ids"),
                                               msg.get("client_id"), msg.get("tools"),
                                               bool(msg.get("keep", True)), msg.get("include")))
                    reply = {"type": "ack", "data": {"client_id": msg.get("client_id"), **res}}
                except Exception as e:
                    reply = {"type": "nack", "data": {"client_id": msg.get("client_id"),
                                                      "error": str(e) or type(e).__name__}}
                c.queue.put_nowait(json.dumps({"seq": HUB.seq, "sid": sid, **reply}, default=str))
            elif op == "cancel":
                HUB.cancel(msg.get("sid") or "")
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as e:
        logger.warning(f"v2 ws error: {e}")
    finally:
        c.dead = True
        wtask.cancel()
        with HUB.lock:
            HUB.clients.discard(c)


_GENERIC_TITLES = {"", "new chat", "(no summary)", "task in progress", "nova conversa"}


def backfill_titles() -> int:
    """Chats whose title stayed "New chat" get their rolling summary as title."""
    from store.manager import _load_sessions_index
    n = 0
    for sid, s in _load_sessions_index().items():
        if (s.get("summary") or "").strip().lower() not in _GENERIC_TITLES:
            continue
        if get_chat_meta(sid).get("title_locked"):
            continue
        summ = (get_summary(sid, silent=True) or "").strip().splitlines()
        if summ and summ[0].strip():
            short = summ[0].strip().split(" + ")[0].split(" — ")[0][:40]
            update_session(sid, summary=short)
            n += 1
    return n


def attach(app):
    """Mount the v2 router and capture the server's event loop."""
    app.include_router(router)
    try:
        from api import lab
        app.include_router(lab.router)
        from api import explore
        app.include_router(explore.router)
        from api import story
        app.include_router(story.router)
        from api import brain
        app.include_router(brain.router)
        lab.seed_pinned()
    except Exception as e:
        logger.warning(f"model lab not mounted: {e}")
    try:
        from api import imagetools        # picture viewer: pixel-art fix, post a cropped/edited picture
        app.include_router(imagetools.router)
        from api import sprites           # xSpriteManager's backend (all image work happens on the Pi)
        app.include_router(sprites.router)
    except Exception as e:
        logger.warning(f"image tools not mounted: {e}")
    try:
        from api import assistant         # xAssistant (voice, wake word "umbra"): session keys, tools, timers
        app.include_router(assistant.router)
        assistant.start()
    except Exception as e:
        logger.warning(f"assistant not mounted: {e}")
    try:
        n = backfill_titles()
        if n:
            logger.info(f"hub: gave {n} untitled chats their summary as title")
    except Exception as e:
        logger.warning(f"title backfill failed: {e}")

    @app.on_event("startup")
    async def _hub_loop():
        HUB.loop = asyncio.get_running_loop()
        # Start Claude CLI ping measurement thread (Pi↔PC latency)
        try:
            from agent.claude_cli import start_ping_thread
            start_ping_thread()
        except Exception as e:
            logger.warning(f"claude ping thread not started: {e}")
