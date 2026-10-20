"""
xUmbra API server — FastAPI exposing REST + WebSocket endpoints for the Android app.

Auth: shared bearer token from env XUMBRA_API_TOKEN. If unset, runs open (LAN trust).
"""
import os, re, json, sys, signal, subprocess, time, asyncio, logging, shutil, threading, configparser, uuid, hashlib, platform
from datetime import datetime
from typing import Any, Optional
from concurrent.futures import ThreadPoolExecutor

import requests
from fastapi import (
    FastAPI, HTTPException, Header, WebSocket, WebSocketDisconnect,
    Query, status, UploadFile, File, Form
)
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from api.registry import (
    REGISTRY, get_chat_model, set_chat_model, clear_chat_model,
    _load_chat_models,
)
from agent.providers import (
    get_current, set_model, get_history, fetch_models, PROVIDER_CONFIG
)
from agent import tools as tools_mod
from store.manager import (
    get_all_sessions, get_session_summary, get_session_log, get_memory, get_summary,
    get_rules, add_rule, add_fact, create_session, update_session,
    get_token_usage, log_token_usage,
    get_prompt_snapshots,
    load_chat_meta, get_chat_meta, set_chat_meta, delete_session_data,
    list_topics, load_topic, get_topic_keys, create_topic,
    update_topic_section, update_topic_keys, delete_topic,
    list_languages, load_vocab,
    load_system_prompt, load_rules, load_facts,
    list_sessions,
    get_app_setting, set_app_setting,
    SYSTEM_PROMPT_FILE, RULES_FILE, FACTS_FILE,
    CORE_DIR, TOPICS_DIR, LANG_DIR, SUMMARY_DIR,
)

logger = logging.getLogger(__name__)

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR  = os.path.join(BASE_DIR, "memory")
SESSIONS_DIR= os.path.join(BASE_DIR, "sessions")
BACKUPS_DIR = os.path.join(BASE_DIR, "backups")
LOGS_DIR    = os.path.join(BASE_DIR, "logs")
TEMP_DIR    = os.path.join(BASE_DIR, "temp")
ICONS_DIR   = os.path.join(SESSIONS_DIR, "icons")
MEDIA_DIR   = os.path.join(TEMP_DIR, "media")
from store import paths as _P
CATALOG_FILE= os.path.join(_P.CACHE, "models_catalog.json")
os.makedirs(BACKUPS_DIR, exist_ok=True)
os.makedirs(ICONS_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

API_TOKEN   = os.getenv("XUMBRA_API_TOKEN", "").strip()
REMOTE_KEY  = os.getenv("UMBRA_REMOTE_KEY", "").strip()   # also accepted (tunnel key)

_PROCESS_START = time.time()

# background pool for icon generation, model catalog refresh, etc.
BG_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xumbra-bg")

# ──────────────────────────────────────────────────────────────────────
#  App
# ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="xUmbra API", version="1.0.0")

from api import hub as hub_mod
hub_mod.attach(app)

from api import remote as remote_mod
app.include_router(remote_mod.router)
app.add_middleware(remote_mod.TunnelGate)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────
#  Auth helper
# ──────────────────────────────────────────────────────────────────────
def _token_ok(tok: Optional[str]) -> bool:
    if not tok:
        return False
    return tok == API_TOKEN or (bool(REMOTE_KEY) and tok == REMOTE_KEY)


def _check_auth(authorization: Optional[str]):
    if not API_TOKEN:
        return  # open mode
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if not _token_ok(authorization[7:].strip()):
        raise HTTPException(status_code=401, detail="invalid token")


def _check_ws_token(token: Optional[str]):
    if not API_TOKEN:
        return True
    return _token_ok(token)


# ──────────────────────────────────────────────────────────────────────
#  Schemas
# ──────────────────────────────────────────────────────────────────────
class ModelSelection(BaseModel):
    provider: str
    model: str

class RuleIn(BaseModel):
    rule: str

class FileWrite(BaseModel):
    content: str

class SessionCreate(BaseModel):
    summary: str = "new chat"

class SysPromptPayload(BaseModel):
    primary:   Optional[str] = None
    secondary: Optional[str] = None

class ToolsPayload(BaseModel):
    enabled: list[str]

class StarPayload(BaseModel):
    starred: bool

class DebugPayload(BaseModel):
    level: Optional[str] = None         # "min" | "normal" | "max"
    show_shell: Optional[bool] = None   # keep shell calls visible even at min

class PinnedTopicsPayload(BaseModel):
    pinned: list[str]

class TopicWrite(BaseModel):
    keys:    Optional[list[str]] = None
    content: Optional[str]       = None

class FactPayload(BaseModel):
    fact:     str
    category: str = "general"

class CronTriggerPayload(BaseModel):
    type:    str   # "cron" | "interval"
    expr:    Optional[str] = None     # for cron
    seconds: Optional[int] = None     # for interval

class CronActionPayload(BaseModel):
    type:        str   # "shell" | "python" | "prompt"
    command:     Optional[str] = None
    code:        Optional[str] = None
    prompt:      Optional[str] = None
    session_id:  Optional[str] = None
    tools:       Optional[list[str]] = None
    timeout:     Optional[int] = None
    push:        Optional[bool] = None   # prompt agents: also notify the phone

class CronAgentPayload(BaseModel):
    name:    str
    trigger: CronTriggerPayload
    action:  CronActionPayload
    enabled: bool = True
    sprite:  Optional[str] = None
    tint:    Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
#  Health
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "auth_required": bool(API_TOKEN),
        "time": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────
#  Providers / models
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/providers")
def providers_list(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {
        "providers": list(PROVIDER_CONFIG.keys()) + ["ollama"],
        "current": get_current(),
        "history": get_history(),
    }


@app.get("/api/providers/models")
def providers_models(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return fetch_models()


@app.get("/api/providers/current")
def providers_current(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return get_current() or {}


@app.post("/api/providers/current")
def providers_set(body: ModelSelection, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    set_model(body.provider, body.model)
    return get_current()


# ──────────────────────────────────────────────────────────────────────
#  Sessions
# ──────────────────────────────────────────────────────────────────────
SESSION_RE = re.compile(
    r'\[ID:(\d+)\]\s*\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]\s*\[(INCOMPLETE|COMPLETE)\]\s*(.*)'
)


def _parse_sessions() -> list[dict]:
    raw = get_all_sessions()
    if not raw or raw == "(no sessions yet)":
        return []
    out = []
    for line in raw.splitlines():
        m = SESSION_RE.search(line)
        if not m:
            continue
        sid, ts, st, summary = m.groups()
        out.append({
            "id": sid,
            "timestamp": ts,
            "status": st,
            "summary": summary.strip(),
        })
    out.sort(key=lambda x: x["timestamp"], reverse=True)
    return out


@app.get("/api/sessions")
def sessions_list(include_hidden: bool = False,
                  group: Optional[str] = None,
                  tag: Optional[str] = None,
                  authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    sessions = _parse_sessions()
    chat_models = _load_chat_models()
    tokens = get_token_usage()
    meta_all = load_chat_meta()
    out = []
    for s in sessions:
        meta = meta_all.get(s["id"], {})
        if not include_hidden and meta.get("hidden"):
            continue
        if group and meta.get("group") != group:
            continue
        if tag and tag not in (meta.get("tags") or []):
            continue
        s["model_override"] = chat_models.get(s["id"])
        t = tokens.get(s["id"], {})
        s["tokens"] = t.get("totals", {"input": 0, "output": 0})
        s["last_model"] = t.get("model")
        s["message_count"] = len(t.get("messages", []))
        s["tags"]  = meta.get("tags") or []
        s["group"] = meta.get("group")
        s["custom_icon"] = meta.get("custom_icon")
        s["starred"] = bool(meta.get("starred", False))
        s["persona"] = meta.get("persona") or None
        out.append(s)
    # Sort: starred first (most-recent-among-starred), then non-starred by timestamp.
    out.sort(key=lambda r: (
        0 if r.get("starred") else 1,
        # timestamp is YYYY-MM-DD HH:MM (string) — lex order matches chronological
        # if we invert by sorting reverse on the second key via tuple.
        r.get("timestamp") or "",
    ))
    # Re-sort properly: starred bucket first, each bucket reverse-chronological.
    starred  = sorted([r for r in out if r.get("starred")],
                      key=lambda r: r.get("timestamp") or "", reverse=True)
    normal   = sorted([r for r in out if not r.get("starred")],
                      key=lambda r: r.get("timestamp") or "", reverse=True)
    return {"sessions": starred + normal}


@app.post("/api/sessions")
def sessions_create(body: SessionCreate, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    sid = create_session(body.summary)
    return {"id": sid, "summary": body.summary, "status": "INCOMPLETE"}


@app.get("/api/sessions/{sid}")
def sessions_detail(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    summary_line = get_session_summary(sid)
    if not summary_line:
        raise HTTPException(404, "session not found")
    m = SESSION_RE.search(summary_line)
    info = {"id": sid, "timestamp": None, "status": None, "summary": ""}
    if m:
        _sid, ts, st, summary = m.groups()
        info.update({"timestamp": ts, "status": st, "summary": summary.strip()})
    info["memory"] = get_summary(sid, silent=True)
    info["model_override"] = get_chat_model(sid)
    tokens = get_token_usage(sid)
    info["tokens"] = tokens
    return info


@app.get("/api/sessions/{sid}/messages")
def sessions_messages(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    log = get_session_log(sid)
    if log == "(no log for this session)" or not log:
        return {"messages": []}
    raw = []
    pat = re.compile(r'\[(\d{2}:\d{2}:\d{2})\]\s+\[(USER|UMBRA)\]\s+(.*)')
    current = None
    for line in log.splitlines():
        m = pat.match(line)
        if m:
            if current:
                raw.append(current)
            ts, role, content = m.groups()
            current = {"time": ts, "role": role.lower(), "content": content}
        else:
            if current is not None:
                current["content"] += "\n" + line
    if current:
        raw.append(current)

    # Extract `[MEDIA: filename]` markers so images survive chat reopen.
    media_re = re.compile(r'\[MEDIA:\s*([^\]]+)\]')
    out = []
    for m in raw:
        media = [name.strip() for name in media_re.findall(m["content"])]
        cleaned = media_re.sub("", m["content"]).strip()
        m["content"] = cleaned
        m["media"]   = media
        out.append(m)
    return {"messages": out}


@app.get("/api/settings/summary_model")
def settings_summary_model_get(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    sm = get_app_setting("summary_model") or {}
    return {"provider": sm.get("provider", ""), "model": sm.get("model", "")}


@app.put("/api/settings/summary_model")
def settings_summary_model_set(body: ModelSelection,
                               authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if (body.provider or "").lower() == "ollama":
        raise HTTPException(status_code=400,
            detail="Ollama is refused for summary_model (would heat the Pi).")
    set_app_setting("summary_model", {"provider": body.provider, "model": body.model})
    return {"provider": body.provider, "model": body.model}


@app.get("/api/sessions/{sid}/events")
def sessions_events(sid: str,
                    limit: int = 2000,
                    authorization: Optional[str] = Header(None)):
    """Return the persisted per-session debug-event log so the client can
    replay router/memory/turn bubbles when reopening the chat. Newest-last."""
    _check_auth(authorization)
    path = os.path.join(SESSIONS_DIR, "events", f"{sid}.jsonl")
    if not os.path.exists(path):
        return {"events": []}
    events: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if limit and len(events) > limit:
        events = events[-limit:]
    return {"events": events}


# ──────────────────────────────────────────────────────────────────────
#  Debug event files — inventory + prune (Phase: manage-from-app)
# ──────────────────────────────────────────────────────────────────────
def _events_dir() -> str:
    return os.path.join(SESSIONS_DIR, "events")

def _list_debug_files() -> list[dict]:
    d = _events_dir()
    if not os.path.isdir(d):
        return []
    out: list[dict] = []
    for name in os.listdir(d):
        if not name.endswith(".jsonl"):
            continue
        full = os.path.join(d, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        # Cheap line count so the app can show event counts without paying
        # the cost of parsing each jsonl.
        try:
            with open(full, "rb") as f:
                event_count = sum(1 for _ in f)
        except OSError:
            event_count = 0
        out.append({
            "sid":         os.path.splitext(name)[0],
            "size":        st.st_size,
            "mtime":       st.st_mtime,
            "event_count": event_count,
        })
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


@app.get("/api/debug/files")
def debug_files_list(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    files = _list_debug_files()
    total = sum(f["size"] for f in files)
    return {"files": files, "total_bytes": total}


@app.delete("/api/debug/files")
def debug_files_delete_all(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    d = _events_dir()
    removed = 0
    freed  = 0
    if os.path.isdir(d):
        for name in os.listdir(d):
            if not name.endswith(".jsonl"):
                continue
            full = os.path.join(d, name)
            try:
                freed += os.path.getsize(full)
                os.remove(full)
                removed += 1
            except OSError:
                pass
    return {"removed": removed, "freed_bytes": freed}


@app.delete("/api/debug/files/{sid}")
def debug_files_delete_one(sid: str,
                           authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    # Guard against traversal — sid must be pure basename.
    if "/" in sid or ".." in sid:
        raise HTTPException(400, "invalid sid")
    full = os.path.join(_events_dir(), f"{sid}.jsonl")
    if not os.path.exists(full):
        raise HTTPException(404, "not found")
    freed = os.path.getsize(full)
    os.remove(full)
    return {"sid": sid, "freed_bytes": freed}


@app.get("/api/sessions/{sid}/tokens")
def sessions_tokens(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return get_token_usage(sid)


@app.post("/api/sessions/{sid}/model")
def sessions_set_model(sid: str, body: ModelSelection, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    set_chat_model(sid, body.provider, body.model)
    agent = REGISTRY.get(sid)
    agent.set_model_override(body.provider, body.model)
    return {"sid": sid, "provider": body.provider, "model": body.model}


@app.delete("/api/sessions/{sid}/model")
def sessions_clear_model(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    clear_chat_model(sid)
    agent = REGISTRY.get(sid)
    agent.set_model_override(None, None)
    return {"ok": True}


@app.get("/api/sessions/{sid}/model")
def sessions_get_model(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return get_chat_model(sid) or {}


# ──────────────────────────────────────────────────────────────────────
#  Tokens
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/tokens")
def tokens_all(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    data = get_token_usage()
    total_in = sum(v.get("totals", {}).get("input", 0) for v in data.values())
    total_out = sum(v.get("totals", {}).get("output", 0) for v in data.values())
    return {
        "sessions": data,
        "totals": {"input": total_in, "output": total_out},
    }


# ──────────────────────────────────────────────────────────────────────
#  Rules
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/rules")
def rules_get(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    text = get_rules()
    return {"rules": text}


@app.post("/api/rules")
def rules_add(body: RuleIn, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    add_rule(body.rule)
    return {"ok": True, "rule": body.rule}


# ──────────────────────────────────────────────────────────────────────
#  Memory file CRUD — full umbra root + memory dir + sessions/
# ──────────────────────────────────────────────────────────────────────
ALLOWED_ROOT_FILES = {
    "system_prompt.md", "config.ini", ".env",
}

def _resolve_memory_file(rel: str) -> str:
    """Resolve a memory-editor path. Allow memory/*, sessions/*, and specific root files."""
    rel = rel.lstrip("/")
    # absolute disallowed
    if rel.startswith("/"):
        raise HTTPException(400, "absolute paths not allowed")
    if ".." in rel.split("/"):
        raise HTTPException(400, "path traversal not allowed")

    if rel in ALLOWED_ROOT_FILES:
        return os.path.join(BASE_DIR, rel)
    if rel.startswith(("memory/", "sessions/", "personas/")):
        path = os.path.join(BASE_DIR, rel)
        # second guard
        real = os.path.realpath(path)
        if not (real.startswith(os.path.realpath(MEMORY_DIR))
                or real.startswith(os.path.realpath(SESSIONS_DIR))
                or real.startswith(os.path.realpath(os.path.join(BASE_DIR, "personas")))):
            raise HTTPException(400, "out of bounds")
        return path
    raise HTTPException(400, f"file not allowed: {rel}")


@app.get("/api/memory/files")
def memory_files_list(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)

    out = []
    for fname in ALLOWED_ROOT_FILES:
        p = os.path.join(BASE_DIR, fname)
        if os.path.exists(p):
            st = os.stat(p)
            out.append({
                "path": fname,
                "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                "category": "root",
            })

    if os.path.isdir(MEMORY_DIR):
        # Walk memory/ recursively so subdirs (core, topics, language) appear.
        for root, _dirs, fnames in os.walk(MEMORY_DIR):
            for fname in sorted(fnames):
                if fname.endswith((".pyc", ".py")) or fname == "__init__.py":
                    continue
                full = os.path.join(root, fname)
                rel  = os.path.relpath(full, BASE_DIR).replace("\\", "/")
                st   = os.stat(full)
                # Categorize by subdir under memory/
                sub  = os.path.relpath(root, MEMORY_DIR).replace("\\", "/")
                cat  = "memory"
                if sub == "core":     cat = "memory_core"
                if sub == "topics":   cat = "memory_topics"
                if sub == "traits":   cat = "memory_traits"
                if sub == "language": cat = "memory_language"
                out.append({
                    "path": rel,
                    "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "category": cat,
                })

    if os.path.isdir(SESSIONS_DIR):
        for root, _dirs, fnames in os.walk(SESSIONS_DIR):
            sub = os.path.relpath(root, SESSIONS_DIR).replace("\\", "/")
            if sub == "icons":
                continue  # skip binary SVGs in the inspector
            for fname in sorted(fnames, reverse=True):
                full = os.path.join(root, fname)
                rel  = os.path.relpath(full, BASE_DIR).replace("\\", "/")
                st   = os.stat(full)
                cat  = "session_log"
                if sub == "summary":  cat = "session_summary"
                if sub == "prompts":  cat = "session_prompts"
                out.append({
                    "path": rel,
                    "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).isoformat(),
                    "category": cat,
                })

    return {"files": out}


@app.get("/api/memory/files/{path:path}")
def memory_file_read(path: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    full = _resolve_memory_file(path)
    if not os.path.exists(full):
        raise HTTPException(404, "not found")
    with open(full, encoding="utf-8", errors="replace") as f:
        content = f.read()
    return {"path": path, "content": content, "size": len(content)}


@app.put("/api/memory/files/{path:path}")
def memory_file_write(path: str, body: FileWrite, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    full = _resolve_memory_file(path)
    if os.path.exists(full):
        backup = os.path.join(
            BACKUPS_DIR,
            f"{os.path.basename(full)}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"
        )
        shutil.copy2(full, backup)
    os.makedirs(os.path.dirname(os.path.abspath(full)), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(body.content)
    return {"ok": True, "path": path, "size": len(body.content)}


# ── the memory notebook, as the app shows it ───────────────────────────
class WordsBody(BaseModel):
    words: list[str] = []


@app.get("/api/v2/memory/pages")
def memory_pages(authorization: Optional[str] = Header(None)):
    """Pages of the memory notebook: title, sections and the words that route to each."""
    _check_auth(authorization)
    from store import notebook as nb
    return {"pages": [nb.page_info(pid) for pid in nb.pages()],
            "suggested": [p for p in nb.SUGGESTED if p not in nb.pages()]}


@app.put("/api/v2/memory/pages/{pid:path}/words")
def memory_page_words(pid: str, body: WordsBody, authorization: Optional[str] = Header(None)):
    """Edit the words that make a message load this page."""
    _check_auth(authorization)
    from store import notebook as nb
    if pid not in nb.pages() and pid not in nb.SUGGESTED:
        raise HTTPException(404, "unknown page")
    full = nb._path(pid)
    if os.path.exists(full):
        try:
            shutil.copy2(full, os.path.join(
                BACKUPS_DIR, f"{os.path.basename(full)}.{datetime.now().strftime('%Y%m%d_%H%M%S')}.bak"))
        except Exception:
            pass
    words = nb.set_words(pid, body.words)
    return {"ok": True, "page": pid, "words": words}


# ──────────────────────────────────────────────────────────────────────
#  WebSocket chat
# ──────────────────────────────────────────────────────────────────────
class WSEventBus:
    """Pump events from agent threads into an asyncio queue tied to the WS loop."""
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()

    def push(self, event: dict):
        try:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, event)
        except RuntimeError:
            pass


@app.websocket("/api/ws/chat")
async def ws_chat(ws: WebSocket,
                  sid: Optional[str] = Query(None),
                  token: Optional[str] = Query(None)):
    if not _check_ws_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    loop = asyncio.get_event_loop()
    bus = WSEventBus(loop)

    agent = REGISTRY.get(sid)

    # event callback — agent thread → bus
    def on_event(ev: dict):
        bus.push(ev)

    # shell line streamer: keep last two lines, emit as tool_progress
    state = {"last_lines": [], "tool_id": None}

    def on_output_line(line: str):
        text = (line or "").rstrip()
        if not text:
            return
        state["last_lines"].append(text)
        state["last_lines"] = state["last_lines"][-2:]
        bus.push({
            "type": "tool_progress",
            "data": {
                "last_lines": list(state["last_lines"]),
                "line": text,
            }
        })

    def on_status(text: str):
        bus.push({"type": "status", "data": {"text": text}})

    def on_send(text: str):
        bus.push({"type": "side_message", "data": {"text": text}})

    def on_file(path: str):
        media_url = None
        try:
            ext = path.rsplit(".", 1)[-1][:6]
            media_name = uuid.uuid4().hex + "." + ext
            dest = os.path.join(MEDIA_DIR, media_name)
            shutil.copy2(path, dest)
            media_url = f"/api/media/{media_name}"
        except Exception:
            pass
        bus.push({"type": "file_sent", "data": {"path": path}})
        if media_url:
            bus.push({"type": "media_url", "data": {"url": media_url}})

    agent.set_event_callback(on_event)
    agent.set_status_callback(on_status)
    agent.set_send_callback(on_send)
    agent.set_file_callback(on_file)
    tools_mod.set_output_callback(on_output_line)

    # producer task — flushes events into WS
    async def pump():
        try:
            while True:
                ev = await bus.queue.get()
                await ws.send_json(ev)
        except (WebSocketDisconnect, RuntimeError):
            pass

    pumper = asyncio.create_task(pump())
    busy_lock = REGISTRY.lock_for(sid)
    active: dict[str, asyncio.Task] = {}

    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")

            if kind == "ping":
                await ws.send_json({"type": "pong", "data": {}})

            elif kind == "set_model":
                provider = data.get("provider")
                model    = data.get("model")
                if provider and model:
                    if sid:
                        set_chat_model(sid, provider, model)
                    agent.set_model_override(provider, model)
                    await ws.send_json({"type": "model_changed",
                                         "data": {"provider": provider, "model": model}})

            elif kind == "clear_model":
                if sid:
                    clear_chat_model(sid)
                agent.set_model_override(None, None)
                await ws.send_json({"type": "model_changed", "data": {}})

            elif kind == "cancel":
                t = active.get("chat")
                if t and not t.done():
                    t.cancel()
                    await ws.send_json({"type": "cancelled", "data": {}})

            elif kind == "message":
                text = data.get("text", "")
                media_ids = data.get("media_ids") or []
                if not text.strip() and not media_ids:
                    continue
                if "chat" in active and not active["chat"].done():
                    await ws.send_json({"type": "error",
                                         "data": {"message": "already processing"}})
                    continue

                include_list = data.get("include")
                include = set(include_list) if include_list else None
                keep_in_memory = bool(data.get("keep_in_memory", True))
                tool_names = data.get("tool_names")  # None=all, []=none, [..]=selected

                old_sid = agent.current_sid
                state["last_lines"] = []

                bus.push({"type": "user_message", "data": {
                    "text": text,
                    "keep_in_memory": keep_in_memory,
                }})

                async def run_chat():
                    def _do():
                        paths = [os.path.join(MEDIA_DIR, mid) for mid in media_ids
                                 if os.path.exists(os.path.join(MEDIA_DIR, mid))]
                        with busy_lock:
                            return agent.chat(text, include=include,
                                              keep_in_memory=keep_in_memory,
                                              media_paths=paths or None,
                                              selected_tools=tool_names)
                    try:
                        result = await loop.run_in_executor(None, _do)
                        # if the agent created a session implicitly, rekey
                        if agent.current_sid and agent.current_sid != old_sid:
                            REGISTRY.adopt_sid(old_sid, agent.current_sid)
                        bus.push({
                            "type": "done",
                            "data": {
                                "text": result,
                                "session_id": agent.current_sid,
                            }
                        })
                    except asyncio.CancelledError:
                        bus.push({"type": "cancelled", "data": {}})
                    except Exception as e:
                        logger.exception("chat error")
                        bus.push({"type": "error", "data": {"message": str(e)}})

                active["chat"] = asyncio.create_task(run_chat())

    except WebSocketDisconnect:
        pass
    finally:
        for t in active.values():
            if not t.done():
                t.cancel()
        pumper.cancel()
        # don't permanently nail callbacks to a closed WS
        agent.set_event_callback(None)
        agent.set_status_callback(None)
        agent.set_send_callback(None)
        agent.set_file_callback(None)
        tools_mod.set_output_callback(None)


# ──────────────────────────────────────────────────────────────────────
#  Model catalog with cost / context / description
# ──────────────────────────────────────────────────────────────────────
def _load_catalog() -> dict:
    if not os.path.exists(CATALOG_FILE):
        return {"updated_at": None, "providers": {}}
    try:
        with open(CATALOG_FILE) as f:
            return json.load(f)
    except Exception:
        return {"updated_at": None, "providers": {}}


def _save_catalog(data: dict):
    with open(CATALOG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _fetch_openrouter_catalog() -> list[dict]:
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        return []
    try:
        r = requests.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"}, timeout=15
        )
        items = r.json().get("data", [])
        out = []
        for m in items:
            pricing = m.get("pricing", {}) or {}
            out.append({
                "id": m.get("id"),
                "name": m.get("name") or m.get("id"),
                "description": (m.get("description") or "")[:1200],
                "context_length": m.get("context_length"),
                "input_cost_per_1m": float(pricing.get("prompt", "0") or 0) * 1_000_000,
                "output_cost_per_1m": float(pricing.get("completion", "0") or 0) * 1_000_000,
                "modality": m.get("architecture", {}).get("modality") or "text",
                "top_provider": (m.get("top_provider") or {}).get("name"),
                "tags": m.get("supported_parameters") or [],
            })
        return out
    except Exception as e:
        logger.warning(f"openrouter catalog fetch failed: {e}")
        return []


def _fetch_openai_catalog() -> list[dict]:
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        return []
    try:
        r = requests.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {key}"}, timeout=15
        )
        # OpenAI doesn't expose pricing via API — keep a small hand-curated table
        prices = {
            "gpt-5":           (10.0,  30.0),
            "gpt-5-mini":      (1.0,   4.0),
            "gpt-4o":          (5.0,   15.0),
            "gpt-4o-mini":     (0.15,  0.6),
            "gpt-4.1":         (10.0,  30.0),
            "gpt-4.1-mini":    (0.6,   2.4),
            "o3":              (15.0,  60.0),
            "o4-mini":         (1.1,   4.4),
            "dall-e-3":        (0,     0),
            "tts-1":           (0,     0),
        }
        out = []
        for m in r.json().get("data", []):
            mid = m.get("id", "")
            if not any(k in mid for k in ("gpt", "o3", "o4", "tts", "dall", "whisper")):
                continue
            ip, op = prices.get(mid, (None, None))
            out.append({
                "id": mid, "name": mid, "description": "",
                "context_length": None,
                "input_cost_per_1m": ip,
                "output_cost_per_1m": op,
                "modality": "audio" if "tts" in mid or "whisper" in mid
                            else "image" if "dall" in mid else "text",
                "top_provider": "OpenAI", "tags": [],
            })
        return out
    except Exception as e:
        logger.warning(f"openai catalog fetch failed: {e}")
        return []


def _fetch_gemini_catalog() -> list[dict]:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        return []
    try:
        r = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
            timeout=15
        )
        prices = {
            "gemini-2.5-pro":   (1.25,  10.0),
            "gemini-2.5-flash": (0.075, 0.3),
            "gemini-2.0-flash-001": (0.10, 0.40),
            "gemini-1.5-pro":   (1.25,  5.0),
            "gemini-1.5-flash": (0.075, 0.3),
        }
        out = []
        for m in r.json().get("models", []):
            mid = m["name"].replace("models/", "")
            ip, op = prices.get(mid, (None, None))
            out.append({
                "id": mid,
                "name": m.get("displayName") or mid,
                "description": (m.get("description") or "")[:600],
                "context_length": m.get("inputTokenLimit"),
                "input_cost_per_1m": ip,
                "output_cost_per_1m": op,
                "modality": "text",
                "top_provider": "Google",
                "tags": m.get("supportedGenerationMethods") or [],
            })
        return out
    except Exception as e:
        logger.warning(f"gemini catalog fetch failed: {e}")
        return []


def _refresh_catalog():
    data = {
        "updated_at": datetime.now().isoformat(),
        "providers": {
            "openrouter": _fetch_openrouter_catalog(),
            "openai":     _fetch_openai_catalog(),
            "gemini":     _fetch_gemini_catalog(),
        },
    }
    _save_catalog(data)
    return data


@app.get("/api/models/catalog")
def models_catalog(refresh: bool = False,
                   authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    data = _load_catalog()
    age_ok = False
    if data.get("updated_at"):
        try:
            age = (datetime.now() - datetime.fromisoformat(data["updated_at"])).total_seconds()
            age_ok = age < 6 * 3600
        except Exception:
            age_ok = False
    if refresh or not age_ok:
        data = _refresh_catalog()
    return data


@app.post("/api/models/catalog/refresh")
def models_catalog_refresh(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return _refresh_catalog()


class ModelRolePayload(BaseModel):
    provider: str
    model: str


# IMPORTANT: keep these BEFORE the {provider}/{model_id:path} wildcard
# below — otherwise "roles" gets interpreted as a provider name.
@app.get("/api/models/roles")
def models_roles_list(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent.model_roles import list_roles, KNOWN_ROLES
    return {"roles": list_roles(), "descriptions": KNOWN_ROLES}


@app.put("/api/models/roles/{role}")
def models_role_set(role: str, body: ModelRolePayload,
                    authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent.model_roles import set_role, KNOWN_ROLES
    if role not in KNOWN_ROLES:
        raise HTTPException(400, f"unknown role: {role}")
    return set_role(role, body.provider, body.model)


@app.get("/api/models/{provider}/{model_id:path}")
def model_detail(provider: str, model_id: str,
                 authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    data = _load_catalog()
    for m in data.get("providers", {}).get(provider, []):
        if m["id"] == model_id:
            return m
    raise HTTPException(404, "model not in catalog")


# ──────────────────────────────────────────────────────────────────────
#  Sessions: delete + tags/groups
# ──────────────────────────────────────────────────────────────────────
@app.delete("/api/sessions/{sid}")
def session_delete(sid: str, wipe: bool = False,
                   authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if not wipe:
        # app-only delete: just mark as hidden in chat_meta
        set_chat_meta(sid, hidden=True)
        return {"ok": True, "wiped": False}
    delete_session_data(sid)
    from store import messages as _ms
    _ms.wipe(sid)
    REGISTRY.drop(sid)
    return {"ok": True, "wiped": True}


class TagsPayload(BaseModel):
    tags: Optional[list[str]] = None
    group: Optional[str] = None
    custom_icon: Optional[str] = None  # emoji or hex color
    hidden: Optional[bool] = None


@app.get("/api/sessions/{sid}/meta")
def session_meta(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return get_chat_meta(sid)


@app.post("/api/sessions/{sid}/meta")
def session_meta_set(sid: str, body: TagsPayload,
                     authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    set_chat_meta(sid,
                  tags=body.tags, group=body.group,
                  custom_icon=body.custom_icon, hidden=body.hidden)
    return get_chat_meta(sid)


# ──────────────────────────────────────────────────────────────────────
#  Per-chat configuration (sysprompt, tools, starred, debug level, pinned)
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/sessions/{sid}/sysprompt")
def session_sysprompt_get(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    m = get_chat_meta(sid)
    return {"primary": m.get("sys_primary", ""),
            "secondary": m.get("sys_secondary", "")}


@app.put("/api/sessions/{sid}/sysprompt")
def session_sysprompt_set(sid: str, body: SysPromptPayload,
                           authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    set_chat_meta(sid,
                  sys_primary=body.primary,
                  sys_secondary=body.secondary)
    return session_sysprompt_get(sid, authorization)


@app.get("/api/sessions/{sid}/tools")
def session_tools_get(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"enabled": get_chat_meta(sid).get("enabled_tools", [])}


@app.put("/api/sessions/{sid}/tools")
def session_tools_set(sid: str, body: ToolsPayload,
                       authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    set_chat_meta(sid, enabled_tools=list(body.enabled))
    return session_tools_get(sid, authorization)


@app.patch("/api/sessions/{sid}/star")
def session_star_set(sid: str, body: StarPayload,
                      authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    set_chat_meta(sid, starred=bool(body.starred))
    return {"starred": bool(body.starred)}


class ProactivePayload(BaseModel):
    allow_proactive: bool


@app.get("/api/sessions/{sid}/proactive")
def session_proactive_get(sid: str, authorization: Optional[str] = Header(None)):
    """Whether this chat lets Umbra speak first. Default false."""
    _check_auth(authorization)
    return {"allow_proactive": bool(get_chat_meta(sid).get("allow_proactive", False))}


@app.put("/api/sessions/{sid}/proactive")
def session_proactive_set(sid: str, body: ProactivePayload,
                           authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    set_chat_meta(sid, allow_proactive=bool(body.allow_proactive))
    return {"allow_proactive": bool(body.allow_proactive)}


@app.get("/api/sessions/{sid}/debug")
def session_debug_get(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    m = get_chat_meta(sid)
    # Server-side capture is always at max now; only show_shell remains
    # user-configurable. `level` is kept in the response for old clients.
    return {"level": "max",
            "show_shell": bool(m.get("show_shell", True))}


@app.put("/api/sessions/{sid}/debug")
def session_debug_set(sid: str, body: DebugPayload,
                       authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    # `level` is ignored — capture is always at max. show_shell is still honored.
    set_chat_meta(sid,
                  debug_level="max",
                  show_shell=body.show_shell)
    return session_debug_get(sid, authorization)


@app.get("/api/sessions/{sid}/pinned_topics")
def session_pinned_get(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"pinned": get_chat_meta(sid).get("pinned_topics", [])}


@app.put("/api/sessions/{sid}/pinned_topics")
def session_pinned_set(sid: str, body: PinnedTopicsPayload,
                        authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    set_chat_meta(sid, pinned_topics=list(body.pinned))
    return session_pinned_get(sid, authorization)


PRESET_TAGS = [
    "tech", "code", "system", "automation", "android", "iot", "3d-printing",
    "casual", "music", "movies", "ideas", "shopping", "travel", "research",
    "debug", "scripts", "docs", "image-gen", "voice",
]


@app.get("/api/categories")
def categories(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    meta = load_chat_meta()
    user_tags = set()
    groups = set()
    for v in meta.values():
        for t in v.get("tags") or []:
            user_tags.add(t)
        if v.get("group"):
            groups.add(v["group"])
    return {
        "preset": PRESET_TAGS,
        "user_tags": sorted(user_tags - set(PRESET_TAGS)),
        "groups": sorted(groups),
    }


# ──────────────────────────────────────────────────────────────────────
#  Prompt snapshot inspector
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/sessions/{sid}/prompts")
def session_prompts(sid: str, limit: int = 5,
                    authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    snaps = get_prompt_snapshots(sid)
    return {"snapshots": snaps[-limit:]}


# ──────────────────────────────────────────────────────────────────────
#  Topic packs (memory/topics/*.txt)
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/memory/topics")
def memory_topics_list(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    out = []
    for name in list_topics():
        path = os.path.join(TOPICS_DIR, f"{name}.txt")
        try:
            st   = os.stat(path)
            size = st.st_size
            mtime = datetime.fromtimestamp(st.st_mtime).isoformat()
        except Exception:
            size, mtime = 0, None
        out.append({
            "name":     name,
            "keys":     get_topic_keys(name),
            "size":     size,
            "modified": mtime,
        })
    return {"topics": out}


@app.get("/api/memory/topics/{name}")
def memory_topic_read(name: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if name not in list_topics():
        raise HTTPException(404, "topic not found")
    content = load_topic(name)
    return {
        "name":    name,
        "keys":    get_topic_keys(name),
        "content": content,
        "size":    len(content),
    }


@app.put("/api/memory/topics/{name}")
def memory_topic_write(name: str, body: TopicWrite,
                       authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    name = name.strip().lower()
    keys    = body.keys if body.keys is not None else []
    content = body.content or ""
    if name not in list_topics():
        try:
            create_topic(name, keys, content=content, source="user")
        except Exception as e:
            raise HTTPException(400, str(e))
        return memory_topic_read(name, authorization)
    # Existing — update KEYS line (if provided) and content body.
    try:
        if body.keys is not None:
            update_topic_keys(name, keys, source="user")
        if body.content is not None:
            # Replace entire content (keeping KEYS line). Easiest: rewrite file.
            path = os.path.join(TOPICS_DIR, f"{name}.txt")
            current_keys = get_topic_keys(name)
            keys_line = "KEYS: " + ", ".join(current_keys)
            with open(path, "w") as f:
                f.write(keys_line + "\n\n" + content.strip() + "\n")
    except Exception as e:
        raise HTTPException(400, str(e))
    return memory_topic_read(name, authorization)


@app.delete("/api/memory/topics/{name}")
def memory_topic_delete(name: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if name not in list_topics():
        raise HTTPException(404, "topic not found")
    delete_topic(name, source="user")
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────
#  Vocab / language files
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/memory/languages")
def memory_languages_list(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    out = []
    for lang in list_languages():
        path = os.path.join(LANG_DIR, f"vocab-{lang}.txt")
        try:
            st   = os.stat(path)
            size = st.st_size
            mtime = datetime.fromtimestamp(st.st_mtime).isoformat()
        except Exception:
            size, mtime = 0, None
        out.append({"lang": lang, "size": size, "modified": mtime})
    return {"languages": out}


@app.get("/api/memory/languages/{lang}")
def memory_language_read(lang: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    content = load_vocab(lang)
    if not content:
        raise HTTPException(404, "language not found")
    return {"lang": lang, "content": content, "size": len(content)}


# ──────────────────────────────────────────────────────────────────────
#  Core memory (rules.txt, facts.txt) read/write conveniences
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/memory/core")
def memory_core_read(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {
        "system_prompt": load_system_prompt(),
        "rules":         load_rules(),
        "facts":         load_facts(),
    }


@app.post("/api/memory/facts")
def memory_facts_add(body: FactPayload, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    add_fact(body.fact, category=body.category, source="user")
    return {"ok": True, "fact": body.fact}


# ──────────────────────────────────────────────────────────────────────
#  Tool catalog (used by app's tool selector dropdown)
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/tools/catalog")
def tools_catalog(authorization: Optional[str] = Header(None)):
    """Tools a chat can have (memory + load_tools are always on)."""
    _check_auth(authorization)
    from agent import toolset
    return {"tools": toolset.catalog(), "always_on": ["memory", "load_tools"]}


# ──────────────────────────────────────────────────────────────────────
#  Personas — chat personas loaded from /home/pi/MaiconErick/Umbra/personas/
# ──────────────────────────────────────────────────────────────────────
_PERSONA_IMAGE_MIME = {
    "png":  "image/png",
    "jpg":  "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "avif": "image/avif",
    "gif":  "image/gif",
}

class PersonaMarkdownPayload(BaseModel):
    content: str

class PersonaAssignPayload(BaseModel):
    name: Optional[str] = None  # None / "" clears the persona


def _decode_upload_ext(file: UploadFile) -> str:
    """Extract a supported image extension from an upload's filename or mime."""
    ext = (os.path.splitext(file.filename or "")[1] or "").lstrip(".").lower()
    from agent import personas as personas_mod
    if ext in personas_mod.IMAGE_EXTS:
        return ext
    mime = (file.content_type or "").lower()
    for e, m in _PERSONA_IMAGE_MIME.items():
        if mime == m:
            return e
    return ""


@app.get("/api/personas")
def personas_list(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    return {"personas": personas_mod.list_personas()}


@app.get("/api/personas/{name}")
def persona_detail(name: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    p = personas_mod.get_persona(name)
    if not p:
        raise HTTPException(404, "persona not found")
    return p


@app.get("/api/personas/{name}/persona.md")
def persona_md_get(name: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    if personas_mod._persona_dir(name) is None:
        raise HTTPException(404, "persona not found")
    return Response(content=personas_mod.read_persona_md(name),
                    media_type="text/markdown; charset=utf-8")


@app.put("/api/personas/{name}/persona.md")
def persona_md_put(name: str, body: PersonaMarkdownPayload,
                   authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    if personas_mod._persona_dir(name) is None:
        raise HTTPException(404, "persona not found")
    if not personas_mod.write_persona_md(name, body.content):
        raise HTTPException(500, "write failed")
    return {"ok": True, **(personas_mod.get_persona(name) or {})}


@app.get("/api/personas/{name}/readme.md")
def persona_readme_get(name: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    if personas_mod._persona_dir(name) is None:
        raise HTTPException(404, "persona not found")
    return Response(content=personas_mod.read_readme_md(name),
                    media_type="text/markdown; charset=utf-8")


@app.put("/api/personas/{name}/readme.md")
def persona_readme_put(name: str, body: PersonaMarkdownPayload,
                       authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    if personas_mod._persona_dir(name) is None:
        raise HTTPException(404, "persona not found")
    if not personas_mod.write_readme_md(name, body.content):
        raise HTTPException(500, "write failed")
    return {"ok": True, **(personas_mod.get_persona(name) or {})}


@app.get("/api/personas/{name}/icon")
def persona_icon_get(name: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    path = personas_mod.find_icon_path(name)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "icon not found")
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return FileResponse(path, media_type=_PERSONA_IMAGE_MIME.get(ext, "application/octet-stream"))


@app.post("/api/personas/{name}/icon")
async def persona_icon_post(name: str, file: UploadFile = File(...),
                            authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    if personas_mod._persona_dir(name) is None:
        raise HTTPException(404, "persona not found")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty upload")
    ext = _decode_upload_ext(file)
    if ext not in personas_mod.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image type (need {','.join(personas_mod.IMAGE_EXTS)})")
    saved = personas_mod.save_icon(name, raw, ext)
    if not saved:
        raise HTTPException(500, "save failed")
    return {"ok": True, "path": saved, **(personas_mod.get_persona(name) or {})}


@app.get("/api/personas/{name}/background")
def persona_background_get(name: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    path = personas_mod.find_background_path(name)
    if not path or not os.path.isfile(path):
        raise HTTPException(404, "background not found")
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return FileResponse(path, media_type=_PERSONA_IMAGE_MIME.get(ext, "application/octet-stream"))


@app.post("/api/personas/{name}/background")
async def persona_background_post(name: str, file: UploadFile = File(...),
                                  authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    if personas_mod._persona_dir(name) is None:
        raise HTTPException(404, "persona not found")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "empty upload")
    ext = _decode_upload_ext(file)
    if ext not in personas_mod.IMAGE_EXTS:
        raise HTTPException(400, f"unsupported image type (need {','.join(personas_mod.IMAGE_EXTS)})")
    saved = personas_mod.save_background(name, raw, ext)
    if not saved:
        raise HTTPException(500, "save failed")
    return {"ok": True, "path": saved, **(personas_mod.get_persona(name) or {})}


# ──────────────────────────────────────────────────────────────────────
#  Prompt caching + usage stats
# ──────────────────────────────────────────────────────────────────────
class CachingConfigPayload(BaseModel):
    enabled: bool


def _read_config_flag(section: str, key: str, fallback: bool = False) -> bool:
    cfg = configparser.ConfigParser()
    cfg.read(os.path.join(BASE_DIR, "config.ini"))
    return cfg.getboolean(section, key, fallback=fallback)


def _write_config_flag(section: str, key: str, value: bool):
    cfg_path = os.path.join(BASE_DIR, "config.ini")
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    if not cfg.has_section(section):
        cfg.add_section(section)
    cfg.set(section, key, "true" if value else "false")
    with open(cfg_path, "w") as f:
        cfg.write(f)


@app.get("/api/caching/status")
def caching_status(authorization: Optional[str] = Header(None)):
    """Per-provider summary: whether caching is supported and how. Lets the
    phone tell the user 'caching is on for Anthropic but unused for Ollama'."""
    _check_auth(authorization)
    from agent.core import _model_caching_status, _caching_enabled
    cur = get_current() or {}
    providers = {
        "openai":     _model_caching_status("openai", "gpt-4o"),
        "openrouter": "mixed (auto for OpenAI/Gemini, explicit for Anthropic)",
        "anthropic":  _model_caching_status("anthropic", "claude-sonnet-4"),
        "google":     _model_caching_status("google", "gemini-2.5-pro"),
        "ollama":     _model_caching_status("ollama", "any"),
    }
    current_mode = _model_caching_status(cur.get("provider", ""), cur.get("model", ""))
    return {
        "enabled": _caching_enabled(),
        "current": {"provider": cur.get("provider"),
                    "model":    cur.get("model"),
                    "mode":     current_mode,
                    "active":   current_mode == "auto"
                                or (current_mode == "explicit" and _caching_enabled())},
        "providers": providers,
        "note": ("'auto' = cached automatically by the provider for prompts >1024 tokens; "
                 "'explicit' = needs the enable_prompt_caching flag so Umbra sends "
                 "cache_control hints; 'unsupported' = no provider-side caching."),
    }


@app.get("/api/caching/config")
def caching_config_get(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent.core import _caching_enabled
    return {"enabled": _caching_enabled()}


@app.put("/api/caching/config")
def caching_config_set(body: CachingConfigPayload,
                        authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    _write_config_flag("general", "enable_prompt_caching", bool(body.enabled))
    return {"enabled": bool(body.enabled)}


@app.get("/api/usage/stats")
def usage_stats(window: str = "all", sid: Optional[str] = None,
                authorization: Optional[str] = Header(None)):
    """Aggregate stats. window = 'all' | '24h' | '7d' | '30d'."""
    _check_auth(authorization)
    from store import usage as usage_mod
    since = None
    if window == "24h":
        since = time.time() - 24*3600
    elif window == "7d":
        since = time.time() - 7*24*3600
    elif window == "30d":
        since = time.time() - 30*24*3600
    return usage_mod.stats(since_sec=since, sid=sid)


@app.get("/api/usage/recent")
def usage_recent(limit: int = 50, sid: Optional[str] = None,
                  authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from store import usage as usage_mod
    return {"turns": usage_mod.recent(limit=limit, sid=sid)}


@app.get("/api/usage/pricing")
def usage_pricing(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from store import usage as usage_mod
    return usage_mod._load_pricing()


# ──────────────────────────────────────────────────────────────────────
#  Image models — dropdown source for the persona/chat config sheets
# ──────────────────────────────────────────────────────────────────────
_IMAGE_MODELS_CACHE: dict = {"ts": 0.0, "data": None}
_IMAGE_MODELS_TTL = 300.0  # seconds

# Approximate output tokens for a single 1024×1024 image on multimodal chat
# models. Used to convert OpenRouter's per-token `image_output` price into an
# approximate USD-per-image. Real values vary by model / resolution / prompt
# but this is close enough for a "which is cheaper" comparison in the picker.
_IMAGE_TOKENS_PER_STANDARD_IMAGE = 1290

# Published per-image cost (USD) for direct-API models. Approximate: OpenAI
# gpt-image-1 charges per resolution × quality; DALL·E 3 charges per resolution;
# Gemini direct charges per image token (~$0.03/1290 tokens for flash-image).
# Values here are the "standard" defaults; the actual bill may differ if the
# operator changes size or quality via config.ini.
_HARDCODED_IMAGE_MODELS = [
    {"id": "gpt-image-1",                "provider": "openai",
     "label": "gpt-image-1 (OpenAI)",             "source": "openai",
     "cost_per_image_usd": 0.040},
    {"id": "dall-e-3",                   "provider": "openai",
     "label": "DALL·E 3 (OpenAI)",                "source": "openai",
     "cost_per_image_usd": 0.040},
    {"id": "gemini-2.5-flash-image",     "provider": "gemini",
     "label": "Gemini 2.5 Flash Image · nano banana (Gemini direct)",
     "source": "gemini",
     "cost_per_image_usd": 0.039},
    {"id": "gemini-3.1-flash-image",     "provider": "gemini",
     "label": "Gemini 3.1 Flash Image (Gemini direct)",
     "source": "gemini",
     "cost_per_image_usd": 0.077},
    {"id": "gemini-3-pro-image",         "provider": "gemini",
     "label": "Gemini 3 Pro Image (Gemini direct)",
     "source": "gemini",
     "cost_per_image_usd": 0.155},
    {"id": "imagen-4.0-generate-001",    "provider": "gemini",
     "label": "Imagen 4 (Gemini direct)",          "source": "gemini",
     "cost_per_image_usd": 0.040},
    {"id": "imagen-4.0-fast-generate-001","provider": "gemini",
     "label": "Imagen 4 Fast (Gemini direct)",     "source": "gemini",
     "cost_per_image_usd": 0.020},
]


def _openrouter_per_image_cost(pricing: dict) -> float | None:
    """OpenRouter reports per-token image_output cost. Convert to approx per
    1024×1024 image so users can compare models at a glance. -1 = variable."""
    if not pricing:
        return None
    for key in ("image_output", "image"):
        v = pricing.get(key)
        if not v:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f < 0:
            return None
        return round(f * _IMAGE_TOKENS_PER_STANDARD_IMAGE, 5)
    return None


def _fetch_openrouter_image_models(timeout: int = 8) -> list[dict]:
    """Live-fetch OpenRouter's model list and keep only image-output models."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return []
    try:
        import requests
        r = requests.get("https://openrouter.ai/api/v1/models",
                         headers={"Authorization": f"Bearer {key}"},
                         timeout=timeout)
        if not r.ok:
            return []
        out = []
        for m in r.json().get("data", []):
            mods = (m.get("architecture") or {}).get("output_modalities") or []
            if "image" not in mods:
                continue
            mid = m.get("id") or ""
            if not mid or "auto" in mid.lower():
                continue
            cost = _openrouter_per_image_cost(m.get("pricing") or {})
            out.append({
                "id":       mid,
                "provider": "openrouter",
                "label":    f"{mid} (OpenRouter)",
                "source":   "openrouter",
                "cost_per_image_usd": cost,
            })
        out.sort(key=lambda x: x["id"])
        return out
    except Exception:
        return []


class ImageModelPayload(BaseModel):
    model: Optional[str] = None  # None / "" clears (falls back to config default)


class ReasoningEffortPayload(BaseModel):
    # None / "" clears (falls back to persona default, else "low")
    effort: Optional[str] = None


@app.get("/api/sessions/{sid}/reasoning-effort")
def session_reasoning_get(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"effort": get_chat_meta(sid).get("reasoning_effort") or None}


@app.put("/api/sessions/{sid}/reasoning-effort")
def session_reasoning_set(sid: str, body: ReasoningEffortPayload,
                          authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    val = (body.effort or "").strip().lower()
    if val and val not in ("minimal", "low", "medium", "high"):
        raise HTTPException(400, "invalid effort — use: minimal, low, medium, high")
    set_chat_meta(sid, reasoning_effort=val)
    return {"effort": val or None}


@app.get("/api/sessions/{sid}/image-model")
def session_image_model_get(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"model": get_chat_meta(sid).get("default_image_model") or None}


@app.put("/api/sessions/{sid}/image-model")
def session_image_model_set(sid: str, body: ImageModelPayload,
                            authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    val = (body.model or "").strip()
    set_chat_meta(sid, default_image_model=val)
    return {"model": val or None}


@app.get("/api/image-models")
def list_image_models(refresh: bool = False,
                     authorization: Optional[str] = Header(None)):
    """Combined list of image-capable models the operator can pick:

    - Hardcoded OpenAI + Gemini-direct entries (for the paid keys in .env)
    - Live OpenRouter image models (cached ~5 min; ?refresh=true busts cache)

    Each entry: {id, provider, label, source}. The client renders a dropdown.
    """
    _check_auth(authorization)
    now = time.time()
    if (refresh or _IMAGE_MODELS_CACHE["data"] is None
            or now - _IMAGE_MODELS_CACHE["ts"] > _IMAGE_MODELS_TTL):
        openrouter = _fetch_openrouter_image_models()
        _IMAGE_MODELS_CACHE["data"] = list(_HARDCODED_IMAGE_MODELS) + openrouter
        _IMAGE_MODELS_CACHE["ts"] = now
    return {
        "models": _IMAGE_MODELS_CACHE["data"] or [],
        "cached_age_sec": round(now - _IMAGE_MODELS_CACHE["ts"], 1),
    }


# Per-session persona assignment
@app.get("/api/sessions/{sid}/persona")
def session_persona_get(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"name": get_chat_meta(sid).get("persona") or None}


@app.put("/api/sessions/{sid}/persona")
def session_persona_set(sid: str, body: PersonaAssignPayload,
                        authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from agent import personas as personas_mod
    val = (body.name or "").strip()
    if val and personas_mod._persona_dir(val) is None:
        raise HTTPException(404, "persona not found")
    # set_chat_meta drops None, so we use "" to mean "no persona" (falsy downstream).
    set_chat_meta(sid, persona=val)
    # Personas can declare that the chat needs certain settings to be useful:
    #   needs_proactive: true         -> flip allow_proactive on
    #   enable_tools: read_file, ...  -> preload chat_meta.enabled_tools so the
    #                                    model can actually call those tools
    #                                    (Umbra sends [] by default)
    if val:
        meta = personas_mod._persona_meta(val) or {}
        if meta.get("needs_proactive"):
            set_chat_meta(sid, allow_proactive=True)
        wanted_tools = meta.get("enable_tools") or []
        if wanted_tools:
            existing = get_chat_meta(sid).get("enabled_tools") or []
            merged = sorted(set(existing) | set(wanted_tools))
            if merged != sorted(existing):
                set_chat_meta(sid, enabled_tools=merged)
    return {"name": val or None}


# ──────────────────────────────────────────────────────────────────────
#  Icon generation (lightweight deterministic SVG)
# ──────────────────────────────────────────────────────────────────────
_ICON_KEYWORDS = [
    (("movie", "filme", "film", "cinema"),   "🎬"),
    (("music", "song", "album", "musica"),    "🎵"),
    (("book", "read", "livro"),               "📚"),
    (("code", "script", "build", "compile"),  "💻"),
    (("server", "deploy", "docker", "linux"), "🖥"),
    (("phone", "android", "apk"),             "📱"),
    (("3d", "print", "stl"),                  "🖨"),
    (("iot", "esp32", "arduino", "sensor"),   "🔌"),
    (("image", "photo", "render"),            "🖼"),
    (("voice", "tts", "audio"),               "🎙"),
    (("home", "casa", "automation"),          "🏠"),
    (("travel", "viagem"),                    "✈"),
    (("shop", "buy"),                         "🛒"),
    (("food", "recipe", "comida"),            "🍳"),
    (("debug", "error", "bug"),               "🐛"),
    (("idea", "brain"),                       "💡"),
    (("note", "todo", "task"),                "📝"),
]

_ICON_PALETTE = [
    "#00E5FF", "#FF2EC4", "#C6FF00", "#FFE500", "#9B4BFF",
    "#00FF88", "#FF7A00", "#FF3355", "#3D7EFF",
]


def _icon_glyph_and_color(sid: str, summary: str) -> tuple[str, str]:
    s = (summary or "").lower()
    glyph = "✦"
    for keys, emoji in _ICON_KEYWORDS:
        if any(k in s for k in keys):
            glyph = emoji
            break
    color = _ICON_PALETTE[abs(hash(sid)) % len(_ICON_PALETTE)]
    return glyph, color


def _render_icon_svg(sid: str, summary: str, custom: str | None = None) -> str:
    if custom and custom.startswith("#") and len(custom) == 7:
        color = custom
        glyph = "✦"
    elif custom and len(custom) <= 4:
        glyph = custom
        color = _ICON_PALETTE[abs(hash(sid)) % len(_ICON_PALETTE)]
    else:
        glyph, color = _icon_glyph_and_color(sid, summary)

    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{color}" stop-opacity="0.25"/>
      <stop offset="100%" stop-color="#000000" stop-opacity="0.05"/>
    </linearGradient>
  </defs>
  <rect width="96" height="96" rx="22" fill="#0A0A0A"/>
  <rect width="96" height="96" rx="22" fill="url(#g)"/>
  <rect x="1" y="1" width="94" height="94" rx="21" fill="none" stroke="{color}" stroke-width="1.5"/>
  <text x="48" y="60" text-anchor="middle" font-size="48" font-family="Apple Color Emoji, Segoe UI Emoji, Noto Color Emoji, sans-serif">{glyph}</text>
</svg>'''


@app.get("/api/sessions/{sid}/icon")
def session_icon(sid: str, refresh: bool = False,
                 authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    path = os.path.join(ICONS_DIR, f"{sid}.svg")
    summary_line = get_session_summary(sid)
    summary = ""
    m = SESSION_RE.search(summary_line) if summary_line else None
    if m:
        summary = m.group(4).strip()
    custom = get_chat_meta(sid).get("custom_icon")
    if not os.path.exists(path) or refresh:
        svg = _render_icon_svg(sid, summary, custom)
        with open(path, "w") as f:
            f.write(svg)
    return Response(content=open(path).read(), media_type="image/svg+xml")


@app.post("/api/sessions/{sid}/icon/refresh")
def session_icon_refresh(sid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    path = os.path.join(ICONS_DIR, f"{sid}.svg")
    if os.path.exists(path):
        os.remove(path)
    return {"ok": True}


# ──────────────────────────────────────────────────────────────────────
#  Media upload/serve
# ──────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────
#  Audio transcription (Whisper)
# ──────────────────────────────────────────────────────────────────────
@app.post("/api/audio/transcribe")
async def audio_transcribe(file: UploadFile = File(...),
                            language: Optional[str] = Form(None),
                            provider: Optional[str] = Form(None),
                            authorization: Optional[str] = Header(None)):
    """provider = gemini | openai (default openai). Returns text + which engine did it."""
    _check_auth(authorization)
    ext = (file.filename or "m4a").split(".")[-1][:6].lower()
    tmp_path = os.path.join(TEMP_DIR, f"audio_{uuid.uuid4().hex}.{ext}")
    with open(tmp_path, "wb") as f:
        f.write(await file.read())
    mime = file.content_type or {"m4a": "audio/mp4", "mp4": "audio/mp4", "ogg": "audio/ogg",
                                 "opus": "audio/ogg", "mp3": "audio/mpeg", "wav": "audio/wav"}.get(ext, "audio/mp4")
    try:
        from agent.transcribe import transcribe
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: transcribe(
            tmp_path, mime, (provider or "openai").lower(), language or None))
    except Exception as e:
        raise HTTPException(500, f"transcribe failed: {e}")
    finally:
        try: os.remove(tmp_path)
        except Exception: pass


# Image formats the LLM providers actually accept. Anything else (HEIC from
# iPhones, BMP, TIFF, AVIF, raw camera formats) gets normalised to PNG before
# we save — otherwise the chat completion call fails with
# "unsupported image format".
_LLM_SUPPORTED_IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}


def _normalize_image_bytes(raw: bytes, src_ext: str) -> tuple[bytes, str]:
    """Return (bytes, ext) — the input as-is if its CONTENT is already in a
    supported format, otherwise a PNG re-encoding.

    We don't trust the extension. Android's share-sheet often renames HEIC
    photos to `.png` or `.jpg` without converting the bytes, so we sniff the
    magic numbers and ask Pillow for the verdict.
    """
    ext = src_ext.lower().lstrip(".")
    try:
        from PIL import Image
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except Exception:
            pass
        import io
        img = Image.open(io.BytesIO(raw))
        actual = (img.format or "").lower()
        # Pillow reports "jpeg" / "png" / "gif" / "webp" / "heif" etc.
        if actual in _LLM_SUPPORTED_IMAGE_EXTS or (
                actual == "jpeg" and "jpg" in _LLM_SUPPORTED_IMAGE_EXTS):
            return raw, "jpg" if actual == "jpeg" else actual
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        logger.info(f"image converted: {actual or ext} → png ({len(buf.getvalue())} bytes)")
        return buf.getvalue(), "png"
    except Exception as e:
        logger.warning(f"image normalize failed (claimed ext=.{ext}): {e}")
        return raw, ext


@app.post("/api/media/upload")
async def upload_media_global(file: UploadFile = File(...),
                              authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    media_id = uuid.uuid4().hex
    raw_ext = (file.filename or "bin").split(".")[-1][:6]
    raw = await file.read()
    data, ext = _normalize_image_bytes(raw, raw_ext)
    rel = f"{media_id}.{ext}"
    full = os.path.join(MEDIA_DIR, rel)
    with open(full, "wb") as f:
        f.write(data)
    return {"id": media_id, "filename": rel, "url": f"/api/media/{rel}",
            "converted": ext != raw_ext.lower().lstrip(".")}


@app.post("/api/sessions/{sid}/media")
async def upload_media(sid: str, file: UploadFile = File(...),
                       authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    media_id = uuid.uuid4().hex
    raw_ext = (file.filename or "bin").split(".")[-1][:6]
    raw = await file.read()
    data, ext = _normalize_image_bytes(raw, raw_ext)
    rel = f"{media_id}.{ext}"
    full = os.path.join(MEDIA_DIR, rel)
    with open(full, "wb") as f:
        f.write(data)
    set_chat_meta(sid, last_media=rel)
    return {"id": media_id, "filename": rel, "url": f"/api/media/{rel}",
            "converted": ext != raw_ext.lower().lstrip(".")}


@app.get("/api/media/{name:path}")
def get_media(name: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad name")
    p = os.path.join(MEDIA_DIR, name)
    if not os.path.exists(p):
        raise HTTPException(404, "not found")
    return FileResponse(p)


# ──────────────────────────────────────────────────────────────────────
#  Cross-chat content search
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/search")
def search(q: str, limit: int = 30,
           authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    q = (q or "").strip()
    if not q:
        return {"matches": []}
    needle = q.lower()
    out = []
    if os.path.isdir(SESSIONS_DIR):
        for fname in os.listdir(SESSIONS_DIR):
            if not fname.endswith(".txt"):
                continue
            sid = fname[:-4]
            path = os.path.join(SESSIONS_DIR, fname)
            try:
                with open(path, errors="replace") as f:
                    text = f.read()
            except Exception:
                continue
            lo = text.lower()
            if needle not in lo:
                continue
            # find first match snippet
            idx = lo.find(needle)
            snippet = text[max(0, idx-80): idx+120].replace("\n", " ")
            out.append({"sid": sid, "snippet": snippet.strip(),
                        "hits": lo.count(needle)})
    # also search session summaries
    for s in _parse_sessions():
        if needle in s["summary"].lower():
            out.append({"sid": s["id"], "snippet": f"[summary] {s['summary']}", "hits": 1})
    out.sort(key=lambda x: -x["hits"])
    return {"matches": out[:limit]}


# ──────────────────────────────────────────────────────────────────────
#  Live system stats (cheap, no extra deps)
# ──────────────────────────────────────────────────────────────────────
_LAST_CPU = {"total": 0, "idle": 0}

def _cpu_percent() -> float:
    try:
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        nums = [int(p) for p in parts]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        total = sum(nums)
        d_total = total - _LAST_CPU["total"]
        d_idle  = idle - _LAST_CPU["idle"]
        _LAST_CPU["total"] = total
        _LAST_CPU["idle"]  = idle
        if d_total <= 0:
            return 0.0
        return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))
    except Exception:
        return 0.0


def _mem_stats() -> dict:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, rest = line.partition(":")
                v = rest.strip().split()[0]
                info[k] = int(v)
        total = info.get("MemTotal", 1) * 1024
        avail = info.get("MemAvailable", 0) * 1024
        used = max(0, total - avail)
        return {"total": total, "used": used, "available": avail,
                "percent": (used * 100.0 / total) if total else 0.0}
    except Exception:
        return {"total": 0, "used": 0, "available": 0, "percent": 0.0}


def _disk_stats(path: str = "/") -> dict:
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        return {"total": total, "used": used, "free": free,
                "percent": (used * 100.0 / total) if total else 0.0}
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0}


def _cpu_temp() -> float:
    candidates = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/thermal/thermal_zone1/temp",
    ]
    for p in candidates:
        try:
            with open(p) as f:
                raw = int(f.read().strip())
            return raw / 1000.0 if raw > 1000 else float(raw)
        except Exception:
            continue
    return 0.0


def _uptime_secs() -> float:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return 0.0


@app.get("/api/system/stats")
def system_stats(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {
        "cpu_percent": _cpu_percent(),
        "mem": _mem_stats(),
        "disk": _disk_stats("/"),
        "load": list(os.getloadavg()),
        "temp_c": _cpu_temp(),
        "uptime_s": _uptime_secs(),
        "ts": datetime.now().isoformat(),
    }


# ──────────────────────────────────────────────────────────────────────
#  User facts inspector
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/user_facts")
def user_facts_get(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from store.manager import get_user_facts
    return {"facts": get_user_facts()}


# ──────────────────────────────────────────────────────────────────────
#  Process control + system info
# ──────────────────────────────────────────────────────────────────────
def _proc_mem_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0
    except Exception:
        pass
    return 0.0


@app.get("/api/process/status")
def process_status(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {
        "pid": os.getpid(),
        "uptime_s": time.time() - _PROCESS_START,
        "memory_mb": _proc_mem_mb(),
        "cpu_percent": _cpu_percent(),
        "status": "running",
    }


@app.get("/api/process/logs")
def process_logs(lines: int = 300, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    log_path = os.path.join(LOGS_DIR, "umbra.log")
    if os.path.exists(log_path):
        try:
            with open(log_path, errors="replace") as f:
                all_lines = f.readlines()
            return {"lines": [l.rstrip() for l in all_lines[-lines:]]}
        except Exception:
            pass
    # fallback: journald
    try:
        r = subprocess.run(
            ["journalctl", "-u", "umbra", "-n", str(lines),
             "--no-pager", "--output=short"],
            capture_output=True, text=True, timeout=5
        )
        return {"lines": r.stdout.splitlines()}
    except Exception:
        return {"lines": []}


# ──────────────────────────────────────────────────────────────────────
#  Cron agents
# ──────────────────────────────────────────────────────────────────────
from agent import cron as cron_mod

_CRON_SUBSCRIBERS: list[Any] = []   # appended bus pushers

def _cron_event_sink(event_type: str, data: dict):
    """Fan out cron events to every active WS subscriber (v1 cron socket and
    every v2 hub client — notifications reach the phone instantly)."""
    for buf in list(_CRON_SUBSCRIBERS):
        try:
            buf.push({"type": event_type, "data": data})
        except Exception:
            pass
    try:
        if event_type == "notification.enqueued":
            from store import notifications as _n
            rec = next((r for r in _n.recent(20) if r.get("id") == data.get("id")), None)
            hub_mod.HUB.publish(data.get("sid"), "notification", rec or data)
        else:
            hub_mod.HUB.publish(None, "cron", {"event": event_type, **data})
    except Exception as e:
        logger.warning(f"hub cron publish failed: {e}")

cron_mod.set_event_sink(_cron_event_sink)


@app.get("/api/cron/agents")
def cron_list(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    return {"agents": cron_mod.snapshot()}


@app.post("/api/cron/agents")
def cron_create(body: CronAgentPayload,
                authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    payload = body.dict()
    # FastAPI gives us None for unspecified optional fields → drop them.
    payload["trigger"] = {k: v for k, v in payload["trigger"].items() if v is not None}
    payload["action"]  = {k: v for k, v in payload["action"].items()  if v is not None}
    try:
        return cron_mod.create_agent(payload)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/cron/agents/{aid}")
def cron_get(aid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    a = cron_mod.get_agent(aid)
    if not a:
        raise HTTPException(404, "agent not found")
    return {**a,
            "next_run": cron_mod.get_next_run(aid),
            "state":    cron_mod.get_state(aid)}


@app.put("/api/cron/agents/{aid}")
def cron_update(aid: str, body: CronAgentPayload,
                 authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    payload = body.dict()
    payload["trigger"] = {k: v for k, v in payload["trigger"].items() if v is not None}
    payload["action"]  = {k: v for k, v in payload["action"].items()  if v is not None}
    try:
        return cron_mod.update_agent(aid, payload)
    except KeyError:
        raise HTTPException(404, "agent not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/cron/agents/{aid}")
def cron_delete(aid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    cron_mod.delete_agent(aid)
    return {"ok": True}


@app.post("/api/cron/agents/{aid}/run-now")
def cron_run_now(aid: str, wait: bool = False, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    if not cron_mod.get_agent(aid):
        raise HTTPException(404, "agent not found")
    if wait:
        return {"ok": True, "record": cron_mod.run_now(aid)}
    # Don't hold the HTTP request for a run that may take minutes: the result
    # arrives as cron.end on the hub socket and lands in the agent's history.
    threading.Thread(target=cron_mod.run_now, args=(aid,), daemon=True).start()
    return {"ok": True, "started": True}


@app.get("/api/cron/agents/{aid}/history")
def cron_history(aid: str, limit: int = 20,
                 authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    a = cron_mod.get_agent(aid)
    if not a:
        raise HTTPException(404, "agent not found")
    return {"history": (a.get("history") or [])[-limit:]}


@app.websocket("/api/ws/cron")
async def ws_cron(ws: WebSocket, token: Optional[str] = Query(None)):
    if not _check_ws_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    loop = asyncio.get_event_loop()
    bus = WSEventBus(loop)
    _CRON_SUBSCRIBERS.append(bus)
    # Initial snapshot
    bus.push({"type": "cron.snapshot",
              "data": {"agents": cron_mod.snapshot()}})
    try:
        async def pump():
            while True:
                ev = await bus.queue.get()
                await ws.send_json(ev)
        task = asyncio.create_task(pump())
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            task.cancel()
    finally:
        try: _CRON_SUBSCRIBERS.remove(bus)
        except ValueError: pass


@app.post("/api/process/logs/clear")
def process_logs_clear(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    cleared = []
    # Truncate the project log file (where systemd-less startups write).
    for candidate in (os.path.join(LOGS_DIR, "umbra.log"), "/tmp/umbra.log"):
        if os.path.exists(candidate):
            try:
                with open(candidate, "w") as f:
                    f.truncate(0)
                cleared.append(candidate)
            except Exception as e:
                logger.warning(f"clear log {candidate}: {e}")
    # Re-open any matching FileHandler so future writes go to the truncated
    # file at offset 0 instead of being silently dropped past the old EOF.
    for h in list(logging.getLogger().handlers):
        if isinstance(h, logging.FileHandler):
            try:
                h.close()
                h.stream = h._open()
            except Exception as e:
                logger.warning(f"reopen handler {h.baseFilename}: {e}")
    return {"ok": True, "cleared": cleared}


RESTART_STATUS_FILE = os.path.join(LOGS_DIR, "restart_status.json")


def _write_restart_status(step: str, **extra):
    """Append a step record to restart_status.json. Survives the execv
    boundary so the phone can see why a restart failed."""
    try:
        rec = {"step": step, "ts": time.time(), "pid": os.getpid(), **extra}
        os.makedirs(os.path.dirname(RESTART_STATUS_FILE), exist_ok=True)
        with open(RESTART_STATUS_FILE, "w") as f:
            json.dump(rec, f)
    except Exception:
        pass


@app.post("/api/process/restart")
def process_restart(authorization: Optional[str] = Header(None)):
    """Restart the server — but never in the middle of a turn.

    Killing the process mid-turn erases the reply the user is already reading
    and any queued message, so while a turn is running the restart is queued
    and the hub runs it the moment the chat goes idle.
    """
    _check_auth(authorization)
    from store import restart as restart_mod
    restart_mod.request("api")
    busy = hub_mod.HUB.is_busy()
    restart_mod.run_if_idle(hub_mod.HUB.is_busy)
    return {"ok": True, "pid": os.getpid(), "deferred": busy,
            "message": "restart queued until the running turn finishes" if busy
                       else "restarting"}


# ──────────────────────────────────────────────────────────────────────
#  Outbound notifications — polled by xUmbra's WorkManager to surface
#  proactive Umbra messages as native Android notifications.
# ──────────────────────────────────────────────────────────────────────
@app.get("/api/notifications/pending")
def notifications_pending(since: float = 0.0,
                          authorization: Optional[str] = Header(None)):
    """Returns the list of un-acked notifications with ts > since.
    The phone passes the timestamp of its last seen notif so it doesn't
    re-display ones it already showed even if not yet acked."""
    _check_auth(authorization)
    from store import notifications as notifs
    return {"notifications": notifs.pending(since_ts=since)}


@app.post("/api/notifications/{nid}/ack")
def notifications_ack(nid: str, authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from store import notifications as notifs
    return {"ok": notifs.ack(nid)}


@app.delete("/api/notifications")
def notifications_clear(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from store import notifications as notifs
    return {"cleared": notifs.clear_all()}


@app.get("/api/notifications/recent")
def notifications_recent(limit: int = 50,
                          authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    from store import notifications as notifs
    return {"notifications": notifs.recent(limit=limit)}


@app.get("/api/process/restart_status")
def process_restart_status(authorization: Optional[str] = Header(None)):
    """Returns the last restart step, used by the phone's Daemon tab to
    diagnose failures. Compare `pid` (when the step fired) with `current_pid`
    to tell whether the new process is alive (step=restored, pid==current_pid)
    or the old one is still up."""
    _check_auth(authorization)
    out = {"step": "idle", "ts": 0, "pid": os.getpid(),
           "current_pid": os.getpid(),
           "uptime_s": round(time.time() - _PROCESS_START, 2)}
    if os.path.exists(RESTART_STATUS_FILE):
        try:
            with open(RESTART_STATUS_FILE) as f:
                out.update(json.load(f))
                out["current_pid"] = os.getpid()
                out["uptime_s"]    = round(time.time() - _PROCESS_START, 2)
        except Exception as e:
            out["read_error"] = str(e)
    return out


@app.post("/api/process/stop")
def process_stop(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    def _do():
        _write_restart_status("stop_requested")
        time.sleep(0.6)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=_do, daemon=True).start()
    return {"ok": True, "message": "stopping"}


@app.get("/api/system/info")
def system_info(authorization: Optional[str] = Header(None)):
    _check_auth(authorization)
    cpu_freq_mhz = 0.0
    for freq_path in (
        "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq",
        "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq",
    ):
        try:
            with open(freq_path) as f:
                cpu_freq_mhz = int(f.read().strip()) / 1000.0
            break
        except Exception:
            continue
    return {
        "hostname": os.uname().nodename,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count() or 0,
        "cpu_freq_mhz": cpu_freq_mhz,
        "python_version": sys.version,
    }


# ──────────────────────────────────────────────────────────────────────
#  Standalone run
# ──────────────────────────────────────────────────────────────────────
@app.on_event("startup")
def _on_startup():
    try:
        cron_mod.start_scheduler()
    except Exception as e:
        logger.warning(f"cron scheduler failed to start: {e}")

    # USD -> BRL for the R$ line the app shows next to every dollar amount:
    # checked at 07:00 daily and every 20 minutes after that.
    try:
        from store import fx
        fx.start()
    except Exception as e:
        logger.warning(f"fx worker failed to start: {e}")

    # Pick up what the restart interrupted: queued messages first (their text
    # never reached the history), then chats whose turn died with the process.
    def _recover():
        time.sleep(4)          # let the agent registry settle first
        try:
            restored = hub_mod.HUB.restore_queues()
            resumed = hub_mod.HUB.resume_unanswered()
            if restored or resumed:
                logger.info("startup recovery: %d queued, %d resumed",
                            restored, len(resumed))
        except Exception:
            logger.warning("startup recovery failed", exc_info=True)

    threading.Thread(target=_recover, name="startup-recovery", daemon=True).start()


@app.on_event("shutdown")
def _on_shutdown():
    # Everything a turn has written so far lives only in memory: store the
    # partial reply and the queue before the process is replaced.
    try:
        hub_mod.HUB.flush_partials()
        hub_mod.HUB.save_queues()
    except Exception:
        logger.warning("could not save turn state on shutdown", exc_info=True)
    try:
        cron_mod.stop_scheduler()
    except Exception:
        pass


def run(host: str = "0.0.0.0", port: int = 8765):
    import uvicorn
    logger.info(f"Starting xUmbra API on {host}:{port} (auth={'yes' if API_TOKEN else 'open'})")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
