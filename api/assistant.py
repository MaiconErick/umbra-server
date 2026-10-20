"""
xAssistant backend: the brain behind the "umbra" wake word on the phone.

The phone (xAssistant app) hears the wake word, then opens an OpenAI Realtime voice session with a
short-lived key minted here, so the real API key never leaves the Pi. Everything the voice model can
DO runs here: the phone forwards each function call to POST /assistant/tool and sends the result back.

    POST /api/v2/assistant/session        -> {"key": "ek_…", "model", "expires_at", "voice"}
    POST /api/v2/assistant/tool           {"name", "arguments": {…} | "json", "session"} -> {"output": {...}}
    POST /api/v2/assistant/log            {"session", "turns": [{"who": "you"|"umbra", "text"}]} (sent turn by turn)
    POST /api/v2/assistant/usage          {"session", "kind": "response"|"transcription", "usage": {OpenAI usage}}
                                          -> priced, added to the conversation + usage.db (category voice)
    POST /api/v2/assistant/end            {"session"} -> AI summary of the conversation (gpt-4.1-mini, background)
    GET  /api/v2/assistant/conversations  -> [{"id", "started", "turns", "tools", "first", "summary", "tokens_in",
                                              "tokens_out", "usd", "usd_voice", "usd_transcribe", "usd_other"}]
    GET  /api/v2/assistant/conversations/{id} -> every line of that conversation (turns, tools, usage, summary)
    Each conversation is data/assistant/conversations/<date>_<time>_<session>.jsonl; log.md keeps the readable summary.
    GET  /api/v2/assistant/prompt         -> {"text" (prompt.md), "is_default", "full" (what a session gets now)}
    PUT  /api/v2/assistant/prompt         {"text"}; POST /prompt/reset -> back to DEFAULT_PROMPT (old one in prompt.prev.md)
    GET  /api/v2/assistant/style          -> {"text" (style.md), "learned_at", "messages", "stats", "running", "stage", "error"}
    POST /api/v2/assistant/style/learn    re-read his WhatsApp (xWapp) and rewrite style.md (also Sundays 04:00)
    GET  /api/v2/assistant/files          -> [{"name", "size", "modified"}]
    GET  /api/v2/assistant/files/{name}   -> {"name", "text"}
    PUT  /api/v2/assistant/files/{name}   {"text"}
    GET  /api/v2/assistant/timers         -> pending timers
    GET  /api/v2/assistant/status         -> what is configured / missing
    PUT  /api/v2/assistant/settings       {"ha_token", "ha_url", "voice"} -> status
    POST /api/v2/assistant/habits/review  run the habit finder now (it also runs nightly at 04:00)
    POST /api/v2/assistant/sample?kind=pos|neg  (WAV body) -> the user's own wake-word recordings
    POST /api/v2/assistant/verify         (WAV of ~2.5 s around a wake) -> {"ok", "text"}: second opinion; rejected
                                          wakes are kept in samples/neg_auto as real-life negatives
                                          (data/assistant/samples/, pulled by the PC trainer)

Plain-text memory (the user can read and edit all of it) lives in memory/assistant/:
    prompt.md    personality + behaviour (editable)  style.md     how he writes, learned from his WhatsApp
    commands.md  what the assistant can do          devices.md   words -> Home Assistant entities
    contacts.md  nicknames -> WhatsApp chats        habits.md    routines seen, suggested, accepted
    rules.md     standing rules                     log.md       every request and what was done
Settings (Home Assistant URL + long-lived token, voice) are in data/settings/assistant.json.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from api.hub import require_auth

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v2/assistant")

BASE = Path(__file__).resolve().parent.parent
FILES = BASE / "memory" / "assistant"
DATA = BASE / "data" / "assistant"
SETTINGS = BASE / "data" / "settings" / "assistant.json"
TIMERS = DATA / "timers.json"
ISLAND = "http://127.0.0.1:8773"
XWAPP_TCP = ("127.0.0.1", 8770)
XWAPP_KEYS = Path.home() / "xwapp" / "keys.env"
MODEL = "gpt-realtime-2.1"  # default; data/settings/assistant.json "model" overrides (app: Voice model)
FILE_NAMES = ("prompt.md", "style.md", "commands.md", "devices.md", "contacts.md", "habits.md", "rules.md", "log.md")
_lock = threading.RLock()

# ---- seed files ---------------------------------------------------------------------------------

_SEED = {
    "commands.md": """# What Umbra can do (voice)

- Home Assistant: read any device's state, turn things on/off, set the AC temperature/mode, light brightness/colour.
- Timers: do something later ("desliga o ar daqui a 5 minutos") or remind me ("me lembra às 18h de ...").
- WhatsApp (xWapp): find a chat by nickname, read the last messages, answer someone (always confirms first).
- Memory: note new nicknames, device names, habits and rules in these files.
""",
    "devices.md": """# Devices (my words -> Home Assistant)

One line per device: words I use -> entity id. Umbra adds lines when it learns a new name.

- ar, ar-condicionado, ar condicionado, AC -> climate.ac
- luz, luzes, luz do quarto -> light.lights
- luz tuya, fita de led -> light.tuya_lights
- tv, televisão -> media_player.55pug7906_78
""",
    "contacts.md": """# Contacts (nicknames -> WhatsApp chats)

One line per person: nicknames -> chat name as it shows in WhatsApp (or the chat id).
Umbra adds lines when it learns who someone is.

""",
    "habits.md": """# Habits

Routines Umbra noticed. Status: [seen] (just counting) / [suggested] (asks you once) / [accepted] (does it or offers it) / [declined] (never asks again).

""",
    "rules.md": """# Rules

- Always read a WhatsApp message back and wait for a yes before sending it.
- Speak Brazilian Portuguese unless I speak another language.
- Keep spoken answers short.
""",
    "log.md": """# Log

Every request and what was done, newest at the bottom.

""",
}


def _ensure_files():
    FILES.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    for name, text in {**_SEED, "prompt.md": DEFAULT_PROMPT + "\n"}.items():
        p = FILES / name
        if not p.exists():
            p.write_text(text, encoding="utf-8")


def _read(name: str) -> str:
    try:
        return (FILES / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _append(name: str, line: str):
    with _lock:
        with open(FILES / name, "a", encoding="utf-8") as f:
            f.write(line.rstrip("\n") + "\n")


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def log_line(text: str):
    _append("log.md", f"- {_now():%Y-%m-%d %H:%M} {text}")


CONVS = DATA / "conversations"


def _conv_path(session: str) -> Optional[Path]:
    if not re.fullmatch(r"[0-9a-f]{6,16}", session or ""):
        return None
    hits = list(CONVS.glob(f"*_{session}.jsonl"))
    return hits[0] if hits else None


def conv_add(session: str, rec: dict):
    """One JSON line per thing that happened in a voice conversation (turns, tool calls), written as it happens."""
    if not re.fullmatch(r"[0-9a-f]{6,16}", session or ""):
        return
    CONVS.mkdir(parents=True, exist_ok=True)
    path = _conv_path(session) or CONVS / f"{_now():%Y%m%d_%H%M%S}_{session}.jsonl"
    rec = {"t": round(time.time(), 1), **rec}
    with _lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _conv_recs(p: Path) -> list:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def conversations(limit: int = 60) -> list:
    out = []
    for p in sorted(CONVS.glob("*.jsonl"), reverse=True)[:limit]:
        recs = _conv_recs(p)
        turns = [r for r in recs if r.get("who") in ("you", "umbra")]
        first = next((r["text"] for r in turns if r["who"] == "you"), "")
        sid = p.stem.split("_")[-1]
        if not turns and not any(r.get("who") == "tool" for r in recs):
            continue  # opened but nothing said or done (early connect on a wake, tests)
        summary = next((r.get("text", "") for r in reversed(recs) if r.get("who") == "summary"), "")
        if not summary and any(r["who"] == "you" for r in turns) and time.time() - (recs[-1].get("t") or 0) > SUMMARY_IDLE:
            summarize_later(sid)  # old or abandoned conversation: filled in for the next refresh
        out.append({"id": sid, "started": recs[0]["t"] if recs else p.stat().st_mtime,
                    "turns": len(turns), "tools": sum(1 for r in recs if r.get("who") == "tool"), "first": first[:120],
                    "summary": summary, **conv_cost(recs)})
    return out


# ---- what a conversation cost -------------------------------------------------------------------

# USD per 1M tokens (OpenAI price page). Realtime bills text and audio separately; cached input is cheap.
REALTIME_PRICES = {
    "gpt-realtime-2.1": {"text_in": 4.0, "text_cached": 0.4, "text_out": 24.0,
                         "audio_in": 32.0, "audio_cached": 0.4, "audio_out": 64.0, "image_in": 5.0},
    "gpt-realtime-2": {"text_in": 4.0, "text_cached": 0.4, "text_out": 24.0,
                       "audio_in": 32.0, "audio_cached": 0.4, "audio_out": 64.0, "image_in": 5.0},
    "gpt-realtime-2.1-mini": {"text_in": 0.6, "text_cached": 0.06, "text_out": 2.4,
                              "audio_in": 10.0, "audio_cached": 0.3, "audio_out": 20.0, "image_in": 0.8},
    "gpt-realtime": {"text_in": 4.0, "text_cached": 0.4, "text_out": 16.0,
                     "audio_in": 32.0, "audio_cached": 0.4, "audio_out": 64.0, "image_in": 5.0},
    "gpt-realtime-mini": {"text_in": 0.6, "text_cached": 0.06, "text_out": 2.4,
                          "audio_in": 10.0, "audio_cached": 0.3, "audio_out": 20.0, "image_in": 0.8},
}
# Speech-to-text (the user's lines in the log, the wake check): per 1M tokens, "per_min" when billed by duration
TRANSCRIBE_PRICES = {
    "gpt-4o-mini-transcribe": {"text_in": 1.25, "audio_in": 3.0, "text_out": 5.0, "per_min": 0.003},
    "gpt-4o-transcribe": {"text_in": 2.5, "audio_in": 6.0, "text_out": 10.0, "per_min": 0.006},
    "whisper-1": {"per_min": 0.006},
}
SUMMARY_MODEL = ("openai", "gpt-4.1-mini")
SUMMARY_IDLE = 240  # seconds of silence after which a conversation without a summary counts as over


def realtime_cost(u: dict, model: str = MODEL) -> dict:
    """Price one Realtime response.done usage block. Returns token counts + usd."""
    pr = REALTIME_PRICES.get(model, REALTIME_PRICES["gpt-realtime"])
    i, o = u.get("input_token_details") or {}, u.get("output_token_details") or {}
    c = i.get("cached_tokens_details") or {}
    t_in, a_in, im_in = int(i.get("text_tokens") or 0), int(i.get("audio_tokens") or 0), int(i.get("image_tokens") or 0)
    t_c, a_c = int(c.get("text_tokens") or 0), int(c.get("audio_tokens") or 0)
    if not c and i.get("cached_tokens"):  # no split given: assume text (the instructions are what gets cached)
        t_c = min(int(i["cached_tokens"]), t_in)
    t_out, a_out = int(o.get("text_tokens") or 0), int(o.get("audio_tokens") or 0)
    usd = ((t_in - t_c) * pr["text_in"] + t_c * pr["text_cached"] + (a_in - a_c) * pr["audio_in"]
           + a_c * pr["audio_cached"] + im_in * pr["image_in"] + t_out * pr["text_out"] + a_out * pr["audio_out"]) / 1_000_000
    return {"in": int(u.get("input_tokens") or t_in + a_in + im_in), "out": int(u.get("output_tokens") or t_out + a_out),
            "cached": t_c + a_c, "audio_in": a_in, "audio_out": a_out, "usd": round(usd, 6)}


def transcribe_cost(u: dict, model: str) -> dict:
    pr = TRANSCRIBE_PRICES.get(model, TRANSCRIBE_PRICES["gpt-4o-mini-transcribe"])
    if u.get("type") == "duration":
        sec = float(u.get("seconds") or 0)
        return {"in": 0, "out": 0, "cached": 0, "seconds": sec, "usd": round(sec / 60 * pr.get("per_min", 0), 6)}
    d = u.get("input_token_details") or {}
    a_in = int(d.get("audio_tokens") or 0)
    t_in = int(d.get("text_tokens") or 0) if d else max(int(u.get("input_tokens") or 0) - a_in, 0)
    out = int(u.get("output_tokens") or 0)
    usd = (t_in * pr.get("text_in", 0) + a_in * pr.get("audio_in", 0) + out * pr.get("text_out", 0)) / 1_000_000
    return {"in": t_in + a_in, "out": out, "cached": 0, "usd": round(usd, 6)}


def record_usage(session: str, kind: str, usage: dict) -> dict:
    """The phone reports what OpenAI said it used; price it, keep it in the conversation and in Umbra's usage.db."""
    if kind == "transcription":
        model = settings()["transcribe_model"]
        cost = transcribe_cost(usage, model)
    else:
        p = _conv_path(session)
        start = next((r for r in _conv_recs(p) if r.get("who") == "start"), {}) if p else {}
        kind, model = "response", start.get("model") or "gpt-realtime"
        cost = realtime_cost(usage, model)
    conv_add(session, {"who": "usage", "kind": kind, "model": model, **cost})
    _bill(model, cost, f"assistant-{kind}")
    return cost


def _bill(model: str, cost: dict, label: str):
    try:
        from store import usage as _u
        _u.record_turn(sid="", provider="openai", model=model, input_tokens=cost["in"] - cost.get("cached", 0),
                       output_tokens=cost["out"], cache_read_tokens=cost.get("cached", 0),
                       actual_cost=cost["usd"], category="voice", label=label)
    except Exception as e:  # noqa: BLE001 - billing must never break a conversation
        logger.warning(f"assistant usage not recorded: {e}")


def conv_cost(recs: list) -> dict:
    """Totals for the app: tokens in/out and dollars, split by what spent them."""
    tot = {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "usd_voice": 0.0, "usd_transcribe": 0.0, "usd_other": 0.0}
    for r in recs:
        if r.get("who") not in ("usage", "summary") or "usd" not in r:
            continue
        tot["tokens_in"] += int(r.get("in") or 0)
        tot["tokens_out"] += int(r.get("out") or 0)
        usd = float(r.get("usd") or 0)
        tot["usd"] += usd
        key = {"response": "usd_voice", "transcription": "usd_transcribe", "wake_check": "usd_transcribe"}.get(
            r.get("kind"), "usd_other")
        tot[key] += usd
    return {k: round(v, 6) if isinstance(v, float) else v for k, v in tot.items()}


# ---- AI summary of a finished conversation ------------------------------------------------------

_SUMMARY_PROMPT = """You summarise one voice conversation between the user and Umbra, his home voice assistant.
Write ONE short English sentence (max 18 words) saying what he wanted and what happened, e.g.
"Turned the AC to 22 °C and set a 30-minute timer to switch it off." or "Asked about the weather; nothing done."
Mention failures ("couldn't reach Home Assistant"). No preamble, no quotes."""
_summarizing: set = set()


def summarize(session: str) -> str:
    p = _conv_path(session)
    if not p:
        return ""
    recs = _conv_recs(p)
    done = next((r.get("text", "") for r in recs if r.get("who") == "summary"), None)
    if done is not None:
        return done
    if not any(r.get("who") == "you" for r in recs):
        return ""
    lines = []
    for r in recs:
        if r.get("who") in ("you", "umbra"):
            lines.append(f"{'User' if r['who'] == 'you' else 'Umbra'}: {r.get('text', '')}")
        elif r.get("who") == "tool":
            lines.append(f"[action {r.get('name')} {json.dumps(r.get('args'), ensure_ascii=False)[:200]}"
                         f" -> {json.dumps(r.get('out'), ensure_ascii=False)[:200]}]")
    from agent.providers import quick_completion
    from store import usage as _u
    r, used = quick_completion(*SUMMARY_MODEL, label="assistant-summary", messages=[
        {"role": "system", "content": _SUMMARY_PROMPT},
        {"role": "user", "content": "\n".join(lines)[-6000:]},
    ], temperature=0.2, max_tokens=80)
    text = (r.choices[0].message.content or "").strip().strip('"')
    u = getattr(r, "usage", None)
    t_in, t_out = int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0)
    pr = _u.price_for(used)
    usd = (t_in * pr.get("input", 0) + t_out * pr.get("output", 0)) / 1_000_000
    conv_add(session, {"who": "summary", "text": text, "model": used, "in": t_in, "out": t_out, "usd": round(usd, 6)})
    return text


def summarize_later(session: str, delay: float = 0):
    """Summary in the background, at most one run per conversation at a time."""
    with _lock:
        if session in _summarizing:
            return
        _summarizing.add(session)

    def run():
        try:
            if delay:
                time.sleep(delay)
            summarize(session)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"assistant summary {session} failed: {e}")
        finally:
            with _lock:
                _summarizing.discard(session)
    threading.Thread(target=run, name=f"assistant-summary-{session}", daemon=True).start()


# ---- settings -----------------------------------------------------------------------------------

def settings() -> dict:
    try:
        s = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    s.setdefault("ha_url", "http://127.0.0.1:8123")
    s.setdefault("ha_token", os.getenv("HA_TOKEN", ""))
    s.setdefault("voice", "cedar")
    s.setdefault("transcribe_model", "gpt-4o-mini-transcribe")
    s.setdefault("model", MODEL)
    if s["model"] not in REALTIME_PRICES:
        s["model"] = MODEL
    return s


# ---- Home Assistant -----------------------------------------------------------------------------

def _ha(method: str, path: str, body: Any = None, timeout=10) -> Any:
    s = settings()
    if not s["ha_token"]:
        raise RuntimeError("Home Assistant token missing (data/settings/assistant.json ha_token)")
    req = urllib.request.Request(
        s["ha_url"].rstrip("/") + path, None if body is None else json.dumps(body).encode(), method=method,
        headers={"Authorization": "Bearer " + s["ha_token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


_ATTRS = ("friendly_name", "temperature", "current_temperature", "hvac_mode", "hvac_modes", "min_temp", "max_temp",
          "fan_mode", "fan_modes", "brightness", "color_mode", "rgb_color", "color_temp_kelvin", "volume_level",
          "media_title", "source", "unit_of_measurement", "device_class")
_DOMAINS = ("climate", "light", "switch", "fan", "media_player", "cover", "lock", "scene", "script", "vacuum",
            "humidifier", "input_boolean", "sensor", "binary_sensor", "weather")


def ha_states(query: str = "", domain: str = "") -> dict:
    rows = _ha("GET", "/api/states")
    q = (query or "").lower().strip()
    aliases = _read("devices.md").lower()
    out = []
    for r in rows:
        eid = r["entity_id"]
        dom = eid.split(".")[0]
        if dom not in _DOMAINS or (domain and dom != domain):
            continue
        name = (r.get("attributes") or {}).get("friendly_name", "")
        hay = f"{eid} {name}".lower()
        hit = not q or q in hay or any(q in line and eid in line for line in aliases.splitlines())
        if not hit:
            continue
        if dom in ("sensor", "binary_sensor") and not q:
            continue  # 150 sensors would drown the answer; ask with a query for them
        attrs = {k: v for k, v in (r.get("attributes") or {}).items() if k in _ATTRS}
        out.append({"entity_id": eid, "state": r["state"], **attrs})
    return {"entities": out[:40], "count": len(out)}


def ha_service(domain: str, service: str, entity_id: str = "", data: Optional[dict] = None) -> dict:
    body = dict(data or {})
    if entity_id:
        body["entity_id"] = entity_id
    changed = _ha("POST", f"/api/services/{domain}/{service}", body)
    states = [{"entity_id": c["entity_id"], "state": c["state"],
               **{k: v for k, v in (c.get("attributes") or {}).items() if k in _ATTRS}} for c in (changed or [])]
    log_line(f"HA {domain}.{service} {entity_id} {json.dumps(data or {}, ensure_ascii=False)}")
    return {"ok": True, "now": states}


# ---- WhatsApp through the xWapp core --------------------------------------------------------------

def _xwapp_token() -> str:
    try:
        for line in XWAPP_KEYS.read_text().splitlines():
            if line.startswith("XWAPP_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return ""


def _xwapp(cmd: str, timeout=20, **args) -> Any:
    """One short-lived connection per call: the assistant is rare enough not to need a resident client."""
    s = socket.create_connection(XWAPP_TCP, timeout=timeout)
    try:
        s.sendall((json.dumps({"auth": _xwapp_token(), "name": "umbra-assistant"}) + "\n").encode())
        s.sendall((json.dumps({"rid": 1, "cmd": cmd, **args}) + "\n").encode())
        f = s.makefile("rb")
        end = time.time() + timeout
        for raw in f:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if m.get("ev") == "auth_failed":
                raise RuntimeError("xWapp auth failed")
            if m.get("rid") == 1:
                if m.get("error"):
                    raise RuntimeError(m["error"])
                return m.get("data")
            if time.time() > end:
                break
        raise RuntimeError(f"xWapp {cmd}: no answer")
    finally:
        s.close()


def _norm(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]+", " ", t).strip()


def whatsapp_find(name: str) -> dict:
    want = _norm(name)
    # nickname lines in contacts.md: "- pai, papai -> Fulano de Tal" (or -> 5562...@s.whatsapp.net)
    targets = {want}
    direct = []
    for line in _read("contacts.md").splitlines():
        if "->" in line:
            left, right = line.lstrip("- ").split("->", 1)
            nicks = [_norm(n) for n in left.split(",")]
            if want in nicks:
                right = right.strip()
                if re.fullmatch(r"[\w.-]+@(s\.whatsapp\.net|g\.us)", right):
                    direct.append(right)  # an exact chat id, valid even before any message exists
                else:
                    targets.add(_norm(right))
    chats = _xwapp("chats", timeout=60) or []
    hits = []
    for c in chats:
        n = _norm(c.get("name", ""))
        if any(t and (t == n or t in n.split() or (len(t) > 3 and t in n)) for t in targets) or c.get("jid") in targets:
            hits.append({"chat": c["jid"], "name": c.get("name"), "group": c.get("isGroup"), "unread": c.get("unread"),
                         "last": (c.get("preview") or "")[:120], "last_ts": c.get("ts")})
    hits.sort(key=lambda h: -(h.get("last_ts") or 0))
    for jid in direct:
        if not any(h["chat"] == jid for h in hits):
            hits.insert(0, {"chat": jid, "name": name, "group": jid.endswith("@g.us"), "last": ""})
    hits = hits[:6]
    if hits:
        try:
            rows = _xwapp("messages", chat=hits[0]["chat"], limit=60) or []
            hits[0]["my_recent"] = [m["text"][:200] for m in rows if m.get("fromMe") and m.get("text")][-8:]
        except Exception as e:  # noqa: BLE001 - the tone hint is optional
            logger.info(f"assistant my_recent skipped: {e}")
    return {"matches": hits}


def whatsapp_read(chat: str, limit: int = 6) -> dict:
    rows = _xwapp("messages", chat=chat, limit=max(1, min(int(limit or 6), 20))) or []
    out = []
    for m in rows[-limit:]:
        text = m.get("text") or (f"[{m.get('kind')}]" if m.get("kind") else "")
        out.append({"from": "me" if m.get("fromMe") else (m.get("senderName") or m.get("sender")), "text": text[:400],
                    "at": dt.datetime.fromtimestamp(m.get("ts", 0)).astimezone().strftime("%d/%m %H:%M")})
    return {"messages": out}


def whatsapp_send(chat: str, text: str, confirmed: bool = False) -> dict:
    if not confirmed:
        return {"ok": False, "error": "Read the message back to the user and only call again with confirmed=true after a yes."}
    _xwapp("send_text", chat=chat, text=text)
    log_line(f"WhatsApp -> {chat}: {text}")
    return {"ok": True}


# ---- timers -------------------------------------------------------------------------------------

def _load_timers() -> list:
    try:
        return json.loads(TIMERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_timers(t: list):
    TIMERS.write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")


def timer_set(label: str, minutes: float = 0, at: str = "", action: Optional[dict] = None) -> dict:
    now = _now()
    if at:
        h, m = (int(x) for x in at.split(":")[:2])
        when = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if when <= now:
            when += dt.timedelta(days=1)
    else:
        when = now + dt.timedelta(minutes=float(minutes or 0))
    t = {"id": uuid.uuid4().hex[:6], "label": label, "due": when.timestamp(), "action": action or {"type": "remind"}}
    with _lock:
        ts = _load_timers()
        ts.append(t)
        _save_timers(ts)
    log_line(f"timer {t['id']} at {when:%H:%M}: {label} {json.dumps(action or {}, ensure_ascii=False)}")
    return {"ok": True, "id": t["id"], "due": when.strftime("%H:%M")}


def timers_list() -> dict:
    return {"timers": [{"id": t["id"], "label": t["label"], "due": dt.datetime.fromtimestamp(t["due"]).astimezone().strftime("%d/%m %H:%M"),
                        "action": t.get("action")} for t in _load_timers()]}


def timer_cancel(id: str) -> dict:
    with _lock:
        ts = _load_timers()
        left = [t for t in ts if t["id"] != id]
        _save_timers(left)
    if len(left) != len(ts):
        log_line(f"timer {id} cancelled")
    return {"ok": len(left) != len(ts)}


def _island(title: str, text: str):
    try:
        urllib.request.urlopen(urllib.request.Request(
            ISLAND + "/message", json.dumps({"title": title, "text": text, "app": "Umbra"}).encode(),
            {"Content-Type": "application/json"}), timeout=3).read()
    except Exception:  # noqa: BLE001 - never let a notice break a timer
        pass


def _fire(t: dict):
    a = t.get("action") or {}
    try:
        if a.get("type") == "ha_service":
            ha_service(a.get("domain", ""), a.get("service", ""), a.get("entity_id", ""), a.get("data"))
            _island("Umbra", f"⏰ {t['label']}: done")
        else:
            _island("Umbra ⏰", t["label"])
            log_line(f"reminder: {t['label']}")
    except Exception as e:  # noqa: BLE001
        _island("Umbra ⏰", f"{t['label']}: failed ({e})")
        log_line(f"timer {t['id']} failed: {e}")


def _timer_loop():
    while True:
        try:
            now = time.time()
            with _lock:
                ts = _load_timers()
                due = [t for t in ts if t["due"] <= now]
                if due:
                    _save_timers([t for t in ts if t["due"] > now])
            for t in due:
                _fire(t)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"assistant timers: {e}")
        time.sleep(5)


# ---- memory files ---------------------------------------------------------------------------------

def memory_note(file: str, text: str) -> dict:
    name = file if file.endswith(".md") else file + ".md"
    if name not in FILE_NAMES or name == "log.md":
        return {"ok": False, "error": f"file must be one of {', '.join(n[:-3] for n in FILE_NAMES if n != 'log.md')}"}
    line = f"- {text.strip()}"
    if name in ("contacts.md", "devices.md") and "->" in text:
        # a nickname points to ONE thing: replace older lines that used any of the same words
        nicks = {_norm(n) for n in text.split("->", 1)[0].split(",") if _norm(n)}
        with _lock:
            kept = []
            for l in _read(name).splitlines():
                if l.startswith("- ") and "->" in l and nicks & {_norm(n) for n in l[2:].split("->", 1)[0].split(",")}:
                    continue
                kept.append(l)
            (FILES / name).write_text("\n".join(kept).rstrip("\n") + "\n" + line + "\n", encoding="utf-8")
    else:
        _append(name, line)
    log_line(f"noted in {name}: {text.strip()}")
    return {"ok": True}


# ---- habits (learning loop) ---------------------------------------------------------------------------

_HABIT_PROMPT = """You maintain habits.md for a voice assistant. Below is its log (what the user asked and what was done)
and the current habits.md. Find routines: the same request (or the same set of actions) repeated on 3+ different days,
usually around the same time or after the same phrase (e.g. "vou dormir" -> lights off + AC off around 23:00).

Rules:
- Keep every existing line whose status is [accepted] or [declined] EXACTLY as it is (the user decided those).
- For each routine you find that is not already listed, add one line:
  - [suggested] <when / trigger> -> <what to do or offer> (seen N×, last YYYY-MM-DD)
- Update the counts / last date of existing [seen]/[suggested] lines. Use [seen] instead of [suggested] while it happened on only 2 days.
- Do not invent routines that the log doesn't show. Write the routine text in English.
- Only routines with ACTIONS (devices, messages, timers). Plain questions ("que horas são") are not habits.
- Every habit line starts with "- " (a markdown bullet).
- Output the full new habits.md and nothing else (keep the title and the explanation line at the top)."""


def habits_review() -> str:
    """Nightly: a small model reads the last 3 weeks of log.md and updates habits.md."""
    from agent.providers import quick_completion
    cutoff = (_now() - dt.timedelta(days=21)).strftime("%Y-%m-%d")
    log = [l for l in _read("log.md").splitlines() if l.startswith("- ") and l[2:12] >= cutoff]
    if len(log) < 6:
        return "not enough log yet"
    cur = _read("habits.md")
    r, used = quick_completion("openai", "gpt-4.1-mini", label="assistant-habits", messages=[
        {"role": "system", "content": _HABIT_PROMPT},
        {"role": "user", "content": "## log.md (last 3 weeks)\n" + "\n".join(log[-600:]) + "\n\n## habits.md now\n" + cur},
    ], temperature=0.2)
    new = (r.choices[0].message.content or "").strip()
    new = re.sub(r"^```(?:markdown|md)?\n|\n```$", "", new).strip()
    if not new.startswith("#"):
        return "model answer ignored (not a habits file)"
    # never lose a decision the user made
    for line in cur.splitlines():
        if ("[accepted]" in line or "[declined]" in line) and line not in new:
            new += "\n" + line
    (DATA / "habits.prev.md").write_text(cur, encoding="utf-8")
    with _lock:
        (FILES / "habits.md").write_text(new + "\n", encoding="utf-8")
    log_line(f"habits reviewed ({used})")
    return "ok"


def habit_answer(habit: str, accepted: bool) -> dict:
    """The user said yes/no to a suggested habit: mark that line [accepted]/[declined]."""
    want = set(_norm(habit).split())
    status = "[accepted]" if accepted else "[declined]"
    with _lock:
        lines = _read("habits.md").splitlines()
        best, score = -1, 0
        for i, line in enumerate(lines):
            if "[suggested]" in line or "[seen]" in line:
                sc = len(set(_norm(line).split()) & want)
                if sc > score:
                    best, score = i, sc
        if best < 0:
            return {"ok": False, "error": "no suggested habit matches"}
        lines[best] = lines[best].replace("[suggested]", status).replace("[seen]", status)
        (FILES / "habits.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    log_line(f"habit {status[1:-1]}: {lines[best]}")
    return {"ok": True, "line": lines[best]}


def _habits_loop():
    last = ""
    while True:
        try:
            now = _now()
            if now.hour == 4 and last != now.strftime("%Y-%m-%d"):
                last = now.strftime("%Y-%m-%d")
                logger.info(f"assistant habits review: {habits_review()}")
                if now.weekday() == 6:
                    style_learn_later()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"assistant habits review failed: {e}")
        time.sleep(600)


# ---- the user's writing style, learned from his WhatsApp ----------------------------------------

STYLE_MODEL = ("openai", "gpt-4.1")
STYLE_STATE = DATA / "style_state.json"
_style_job = {"running": False, "error": "", "stage": ""}
_ABBR = ("vc", "vcs", "tb", "tbm", "pq", "q", "oq", "mt", "mto", "n", "nn", "ss", "blz", "tmj", "vlw", "pfv", "td",
         "tds", "msm", "tlg", "slk", "mds", "aff", "kd", "cmg", "ctg", "hj", "amn", "dps", "agr", "qnd", "qdo", "oxe",
         "ne", "né", "ta", "tá", "to", "tô", "pra", "pro", "cê", "ce", "bjs", "obg", "fds", "sla", "pdp", "dnv", "glr")
_ACCENTS = (("nao", "não"), ("ta", "tá"), ("ja", "já"), ("so", "só"), ("la", "lá"), ("voce", "você"), ("to", "tô"),
            ("tambem", "também"), ("ate", "até"), ("entao", "então"))
_LAUGH_RE = re.compile(r"\b(?:[kj]*k[kj]{2,}|(?:h[aeuh]){2,}h?|rs(?:rs)*|kkk+)\b", re.I)
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_WORD_RE = re.compile(r"[a-záàâãéêíóôõúç]+", re.I)


def _style_stats(mine: list) -> dict:
    """Hard numbers about how he types, so the model's style guide is grounded, not guessed."""
    from collections import Counter
    texts = [m["text"] for m in mine]
    n = len(texts) or 1
    alpha = [t for t in texts if re.search(r"[A-Za-zÀ-ú]", t)]
    lower_start = sum(1 for t in alpha if re.search(r"[A-Za-zÀ-ú]", t).group().islower())
    ends = Counter(("?" if t.rstrip()[-1:] == "?" else "!" if t.rstrip()[-1:] == "!" else "." if t.rstrip()[-1:] == "."
                    else "none") for t in texts if t.strip())
    words = Counter(w.lower() for t in texts for w in _WORD_RE.findall(t))
    laughs = Counter(m.lower() for t in texts for m in _LAUGH_RE.findall(t))
    accents = {f"{a}/{b}": [words[a], words[b]] for a, b in _ACCENTS if words[a] + words[b] >= 3}
    bursts = sum(1 for a, b in zip(mine, mine[1:]) if a["chat"] == b["chat"] and 0 <= b["ts"] - a["ts"] <= 60)
    lens = sorted(len(t.split()) for t in texts)
    return {
        "messages": len(texts),
        "starts_lowercase_pct": round(100 * lower_start / (len(alpha) or 1)),
        "ending_pct": {k: round(100 * v / n) for k, v in ends.items()},
        "all_caps_messages_pct": round(100 * sum(1 for t in alpha if t.isupper() and len(t) > 3) / n),
        "laugh_in_pct_of_messages": round(100 * sum(1 for t in texts if _LAUGH_RE.search(t)) / n),
        "laughs_top": laughs.most_common(8),
        "abbreviations_top": [(a, words[a]) for a in sorted(_ABBR, key=lambda a: -words[a]) if words[a] >= 2][:20],
        "accent_without_vs_with": accents,
        "emoji_in_pct_of_messages": round(100 * sum(1 for t in texts if _EMOJI_RE.search(t)) / n),
        "emoji_top": Counter(e for t in texts for e in _EMOJI_RE.findall(t)).most_common(8),
        "words_per_message_median": lens[len(lens) // 2] if lens else 0,
        "sends_in_bursts_pct": round(100 * bursts / n),
    }


_STYLE_PROMPT = """You study how one person (Brazilian, writes in Portuguese) types on WhatsApp, so an AI can write
messages that look exactly like his. You get hard statistics computed from his messages and a sample of his real messages
(with the other person's previous message as context, and who the chat is with).

Write a markdown file, in English, at most ~50 lines, with these sections:
# How I write on WhatsApp
## Rules
Concrete, checkable rules with numbers from the statistics: capitalisation, end punctuation, accents dropped or kept,
laughs (which exact forms and how often), abbreviations he really uses, emoji (which ones, how often), message length,
splitting thoughts into several short messages, greetings and goodbyes he uses, slang, how he says yes/no/ok.
Only state what the data shows. Never invent habits.
## Words I use
A compact line of his typical words/expressions/laughs, copied exactly as he types them.
## By person
One line per contact where his tone clearly differs (e.g. more formal with a parent, more teasing with a friend).
Use the chat names given. Skip people where nothing stands out.
## Real examples
12-18 of his real messages, copied EXACTLY (keep his typos, lowercase, laughs), short ones, varied situations,
each with a few words of context in parentheses. Never include addresses, documents, passwords, money details or
anything intimate.
Output only the file."""


def style_learn(max_chats: int = 30, per_chat: int = 300) -> dict:
    """Read his recent WhatsApp chats (his own messages + what he replied to) and rewrite style.md."""
    _style_job.update(running=True, error="", stage="reading chats")
    try:
        chats = [c for c in (_xwapp("chats", timeout=60) or [])
                 if not c.get("archived") and not c.get("blocked") and "@broadcast" not in c.get("jid", "")]
        chats.sort(key=lambda c: -(c.get("ts") or 0))
        singles = [c for c in chats if not c.get("isGroup")][:max_chats]
        groups = [c for c in chats if c.get("isGroup")][:6]
        mine, samples, own_jid = [], [], ""
        for i, c in enumerate(singles + groups):
            _style_job["stage"] = f"reading chat {i + 1}/{len(singles) + len(groups)}"
            try:
                rows = _xwapp("messages", chat=c["jid"], limit=per_chat, timeout=40) or []
            except Exception as e:  # noqa: BLE001
                logger.info(f"style: skip {c.get('name')}: {e}")
                continue
            if not own_jid:
                own_jid = next((m.get("sender", "") for m in rows if m.get("fromMe") and "@s.whatsapp.net" in m.get("sender", "")), "")
            if own_jid and c["jid"] == own_jid:
                continue  # notes to himself are not conversation
            prev = ""
            for m in rows:
                text = (m.get("text") or "").strip()
                if not text or m.get("kind") not in (None, "", "text") or re.fullmatch(r"https?://\S+", text):
                    if not m.get("fromMe"):
                        prev = ""
                    continue
                if m.get("fromMe"):
                    mine.append({"chat": c["jid"], "ts": m.get("ts", 0), "text": text})
                    samples.append({"with": c.get("name") or "?", "group": bool(c.get("isGroup")),
                                    "after": prev[:100], "me": text[:240], "ts": m.get("ts", 0)})
                else:
                    prev = text
        if len(mine) < 30:
            raise RuntimeError(f"only {len(mine)} of his text messages found")
        stats = _style_stats(mine)
        _style_job["stage"] = f"studying {len(mine)} messages"
        # newest first, at most ~25 per chat so one long chat can't dominate
        from collections import defaultdict
        per = defaultdict(list)
        for x in sorted(samples, key=lambda x: -x["ts"]):
            if len(per[x["with"]]) < 25:
                per[x["with"]].append(x)
        pick = sorted((x for v in per.values() for x in v), key=lambda x: -x["ts"])[:500]
        sample_txt = "\n".join(f"[{x['with']}{' (group)' if x['group'] else ''}] "
                                + (f"them: {x['after']!r} -> " if x["after"] else "") + f"me: {x['me']!r}" for x in pick)
        from agent.providers import quick_completion
        r, used = quick_completion(*STYLE_MODEL, label="assistant-style", messages=[
            {"role": "system", "content": _STYLE_PROMPT},
            {"role": "user", "content": "## Statistics\n" + json.dumps(stats, ensure_ascii=False, indent=1)
             + "\n\n## Sample of his messages (newest first)\n" + sample_txt},
        ], temperature=0.3)
        new = re.sub(r"^```(?:markdown|md)?\n|\n```$", "", (r.choices[0].message.content or "").strip()).strip()
        if not new.startswith("#"):
            raise RuntimeError("model answer is not a style file")
        old = _read("style.md")
        notes = re.search(r"^## My notes\b.*", old, re.M | re.S)  # the part he writes himself survives a relearn
        new = (f"{new}\n\n" + (notes.group().strip() if notes else
               "## My notes\nAnything you add here is kept when Umbra relearns your style.") + "\n")
        if old:
            (DATA / "style.prev.md").write_text(old, encoding="utf-8")
        with _lock:
            (FILES / "style.md").write_text(new, encoding="utf-8")
        state = {"learned_at": time.time(), "messages": len(mine), "chats": len(per), "model": used, "stats": stats}
        STYLE_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        log_line(f"writing style relearned from {len(mine)} messages in {len(per)} chats ({used})")
        return state
    except Exception as e:  # noqa: BLE001
        _style_job["error"] = str(e)[:300]
        raise
    finally:
        _style_job.update(running=False, stage="")


def style_learn_later():
    with _lock:
        if _style_job["running"]:
            return False
        _style_job["running"] = True

    def run():
        try:
            style_learn()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"assistant style learn failed: {e}")
    threading.Thread(target=run, name="assistant-style", daemon=True).start()
    return True


def style_status() -> dict:
    try:
        st = json.loads(STYLE_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    return {"learned_at": st.get("learned_at", 0), "messages": st.get("messages", 0), "chats": st.get("chats", 0),
            "stats": st.get("stats", {}), "running": _style_job["running"], "stage": _style_job["stage"],
            "error": _style_job["error"]}


# ---- tool registry --------------------------------------------------------------------------------

def _fn(name, description, props, required=()):
    return {"type": "function", "name": name, "description": description,
            "parameters": {"type": "object", "properties": props, "required": list(required)}}


TOOLS = [
    _fn("ha_states", "List Home Assistant devices with their current state (and AC/light attributes). Use before acting "
        "when unsure of the entity or its state. Sensors are only listed when a query is given.",
        {"query": {"type": "string", "description": "word to filter by (entity id, name or a word from devices.md)"},
         "domain": {"type": "string", "description": "optional: climate, light, switch, media_player, sensor…"}}),
    _fn("ha_service", "Call a Home Assistant service, e.g. climate.set_temperature {temperature: 23}, climate.turn_on, "
        "climate.set_hvac_mode {hvac_mode: cool}, light.turn_on {brightness_pct: 30, rgb_color: [255,0,0]}, light.turn_off.",
        {"domain": {"type": "string"}, "service": {"type": "string"}, "entity_id": {"type": "string"},
         "data": {"type": "object", "description": "service data", "additionalProperties": True}},
        ("domain", "service", "entity_id")),
    _fn("timer_set", "Do something later or remind the user. Give minutes from now OR a clock time 'HH:MM'. "
        "action = {type:'ha_service', domain, service, entity_id, data} or {type:'remind'} (default).",
        {"label": {"type": "string", "description": "short description, spoken language"},
         "minutes": {"type": "number"}, "at": {"type": "string", "description": "HH:MM, 24 h"},
         "action": {"type": "object", "additionalProperties": True}}, ("label",)),
    _fn("timers_list", "List pending timers.", {}),
    _fn("timer_cancel", "Cancel a timer by id.", {"id": {"type": "string"}}, ("id",)),
    _fn("whatsapp_find", "Find WhatsApp chats by a person's name or nickname (uses contacts.md nicknames). "
        "Returns chat ids with the last message.", {"name": {"type": "string"}}, ("name",)),
    _fn("whatsapp_read", "Read the latest messages of a WhatsApp chat.",
        {"chat": {"type": "string"}, "limit": {"type": "integer"}}, ("chat",)),
    _fn("whatsapp_send", "Send a WhatsApp text. `text` is the message the user MEANT, written as they would type it "
        "('manda só um oi' -> 'Oi! 😊', 'diz que vou atrasar' -> 'Vou me atrasar um pouco!'), never their spoken "
        "instruction copied word for word. First read that text and the recipient back; call with confirmed=true "
        "only after they say yes.",
        {"chat": {"type": "string"}, "text": {"type": "string"}, "confirmed": {"type": "boolean"}}, ("chat", "text")),
    _fn("memory_note", "Save something learned for next time: a device nickname (devices: 'word -> entity'), "
        "a contact nickname (contacts: 'nick -> chat name'), a rule (rules), or a habit (habits).",
        {"file": {"type": "string", "enum": ["devices", "contacts", "rules", "habits", "commands"]},
         "text": {"type": "string"}}, ("file", "text")),
    _fn("habit_answer", "The user answered a suggested habit (from 'Suggested habits'): accepted=true to make it a "
        "routine, false to never offer it again.",
        {"habit": {"type": "string", "description": "words from the habit line"}, "accepted": {"type": "boolean"}},
        ("habit", "accepted")),
    _fn("end_conversation", "Call when the user is done (thanks, that's all, silence after the task) so the phone stops listening.", {}),
]

_IMPL = {
    "ha_states": ha_states, "ha_service": ha_service, "timer_set": timer_set, "timers_list": timers_list,
    "timer_cancel": timer_cancel, "whatsapp_find": whatsapp_find, "whatsapp_read": whatsapp_read,
    "whatsapp_send": whatsapp_send, "memory_note": memory_note, "habit_answer": habit_answer,
    "end_conversation": lambda: {"ok": True},
}


def run_tool(name: str, args: dict) -> dict:
    fn = _IMPL.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        return fn(**(args or {}))
    except TypeError as e:
        return {"error": f"bad arguments: {e}"}
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}"}
    except Exception as e:  # noqa: BLE001 - the model gets the error and can tell the user
        return {"error": str(e)}


# ---- prompt + session -----------------------------------------------------------------------------

# The personality + behaviour part of the prompt. Seeds memory/assistant/prompt.md, which the user edits in the app;
# instructions() appends the live parts (time, devices, contacts, rules, writing style, habits) after it.
DEFAULT_PROMPT = """You are Umbra, the user's voice assistant on their phone (they call you by saying "Umbra").

## How you talk (most important)
- You're their funny best friend, not an assistant. Brazilian Portuguese, slangy and relaxed: "fechou", "bora", "suave",
  "deixa comigo", "pronto, chefe", "tá na mão". A bit of teasing and a light joke now and then, never cringe, never corporate.
- SHORT. Most answers are 2 to 6 words ("Pronto, ar no 23." / "Feito, mandei." / "Tá na mão."). Two short sentences is the
  absolute maximum, only when you really need to ask something.
- Never repeat back what they just said, never explain what you're about to do, never list options, never say
  "como posso ajudar", "posso te ajudar com mais alguma coisa", "estou aqui para ajudar" or anything that sounds like a call center.
- Everything is spoken aloud: no lists, no markdown, no emojis, never read ids, numbers of chats or JSON.
- If they just say "Umbra" or "oi", answer with a quick hey ("Fala!", "Diz aí.", "Opa!") and wait.
- Speak another language only if they do.

## How you sound (your voice is everything)
- Sound like a real person on a call with a close friend, never a narrator or an announcer: natural rhythm, little
  breaths between phrases, relaxed pace, pitch that moves up and down, words that run together like real speech.
- When something is funny, really laugh out loud in your voice (a short natural laugh, a chuckle, a snort), never say
  "haha" or "kkk" as words. Laugh at his jokes and at your own teasing.
- Let feelings come through: a sigh when something is annoying or failed, "hmm" while you think, "ahh" when it clicks,
  a little gasp at surprises, a soft "eita" at bad news, a smile you can hear when you're happy.
- Match his energy: late at night or sleepy -> softer, slower, almost whispering; excited -> faster and brighter;
  stressed -> calm and warm.
- Small fillers are fine ("é...", "tipo", "ó", "pera", "nossa"), but keep it short: the laugh or sigh is part of the
  2-6 words, not extra talk.
- When you read a WhatsApp message back, read it the way he'd say it out loud; turn "kkk" or emojis into a real laugh or tone.

How to act:
- Understand loose requests ("tá muito quente aqui" means they want the AC on and cooler). Act with the tools, then confirm in a few words.
- If a detail really matters and is missing, ask one quick question (e.g. "quer em quanto graus?"); otherwise use sensible defaults.
- Never claim you did something without a successful tool result. If a tool fails, say so plainly.
- "daqui a X minutos" / "às 18h" -> timer_set with the action to run then.
- WhatsApp: find the chat, read the message and recipient back, send only after a yes (rules below).
- Writing the WhatsApp message: the user tells you WHAT to say, you write the message itself, in first person, exactly
  the way HE types: follow "How the user writes" below (capitalisation, punctuation, laughs, abbreviations like vc/tb/pq)
  and copy the tone of his own recent messages to that person (my_recent in the whatsapp_find result). It must look
  like he typed it, never like an assistant. Words like "só", "manda", "fala que", "diz que", "pergunta se", "avisa que"
  are instructions, never part of the message. Meaning examples (then apply his style):
  "manda só um oi pro Pedro" -> a plain hi; "fala pra minha mãe que eu chego às 8" -> "chego às 8";
  "pergunta se ele vem hoje" -> "vai vir hoje?"; "diz que eu tô atrasado" -> "to atrasado, ja chego".
  Copy their words literally only when they say so ("escreve exatamente ...", "manda literalmente ...", "escreve assim: ...").
  Read back as "Mando pro Pedro: <message>. Pode ser?"; if they want it different, rewrite and read back again.
- If whatsapp_find returns nothing, try variants yourself (pai -> papai, meu pai; mãe -> mae, mamãe) before asking. When a variant works, save it with memory_note (contacts).
- Nicknames: when the user says a name should mean a contact ("quando eu falar Pedro é o X Pacheco", "o Pedro é o contato
  X Pacheco"), the contact is the name THEY said (X Pacheco), never one you found before. Call whatsapp_find with exactly that
  contact name, then memory_note contacts "Pedro -> <the exact chat name found>" and confirm in a few words ("Fechou, Pedro é o X Pacheco.").
  If that search finds nothing or several, ask which one; never pick a different chat on your own.
- When the user teaches you a new name for a device or person, or a standing rule, save it with memory_note.
- When the user says they are going to sleep or leaving, check the AC and lights with ha_states and offer to turn off what is on.
- When the task is done and the user has nothing else, say a short goodbye and call end_conversation.
- Never end answers with "mais alguma coisa?" or similar offers; the phone keeps listening a few seconds by itself."""


def instructions() -> str:
    now = _now()
    habit_lines = _read("habits.md").splitlines()
    habits = "\n".join(l for l in habit_lines if "[accepted]" in l) or "(none yet)"
    suggested = "\n".join(l for l in habit_lines if "[suggested]" in l) or "(none)"
    base = _read("prompt.md").strip() or DEFAULT_PROMPT
    style = _read("style.md").strip()
    return f"""{base}

Now: {now:%A %d/%m/%Y %H:%M} ({now.tzname()}).

## Devices (devices.md)
{_read("devices.md")}
## Contacts (contacts.md)
{_read("contacts.md")}
## Rules (rules.md)
{_read("rules.md")}
## How the user writes (style.md, learned from his WhatsApp) - write every WhatsApp message in this voice
{style or "(not learned yet: write short, casual, lowercase PT-BR)"}

## Accepted habits (do or offer these when they fit)
{habits}
## Suggested habits (when the moment fits, ask ONCE whether they want it as a routine, then call habit_answer)
{suggested}
"""


def mint_session(model: str = "", voice: str = "") -> dict:
    s = settings()
    if model in REALTIME_PRICES:  # one-off overrides (tests, voice samples)
        s["model"] = model
    if voice in VOICES:
        s["voice"] = voice
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        raise HTTPException(500, "OPENAI_API_KEY missing on the Pi")
    body = {
        "expires_after": {"anchor": "created_at", "seconds": 600},
        "session": {
            "type": "realtime",
            "model": s["model"],
            "instructions": instructions(),
            "tools": TOOLS,
            "tool_choice": "auto",
            "audio": {
                "input": {"transcription": {"model": s["transcribe_model"], "language": "pt"},
                          "turn_detection": {"type": "semantic_vad", "eagerness": "high"},
                          "noise_reduction": {"type": "near_field"}},
                "output": {"voice": s["voice"]},
            },
        },
    }
    req = urllib.request.Request("https://api.openai.com/v1/realtime/client_secrets", json.dumps(body).encode(),
                                 {"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"OpenAI: {e.read()[:400].decode('utf-8', 'replace')}")
    sid = uuid.uuid4().hex[:8]
    conv_add(sid, {"who": "start", "voice": s["voice"], "model": s["model"]})
    wc = _last_wake_check
    if wc and time.time() - wc.get("t", 0) < 20:
        conv_add(sid, {"who": "usage", "kind": "wake_check", "model": "gpt-4o-mini-transcribe", **wc["cost"]})
        wc.clear()
    return {"key": res["value"], "expires_at": res.get("expires_at"), "model": s["model"], "voice": s["voice"],
            "session": sid}


# ---- routes -------------------------------------------------------------------------------------

class ToolCall(BaseModel):
    name: str
    arguments: Any = None
    session: str = ""


class LogBody(BaseModel):
    session: str = ""
    turns: list = []


class UsageBody(BaseModel):
    session: str = ""
    kind: str = "response"
    usage: dict = {}


class EndBody(BaseModel):
    session: str = ""
    discard: bool = False  # a wake the Pi turned down: the phone connected early, nothing happened


class PromptBody(BaseModel):
    text: str


class FileBody(BaseModel):
    text: str


@router.post("/session")
def session(model: str = "", voice: str = "", authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return mint_session(model, voice)


@router.post("/tool")
def tool(body: ToolCall, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    args = body.arguments
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            return {"output": {"error": "arguments are not valid JSON"}}
    out = run_tool(body.name, args or {})
    conv_add(body.session, {"who": "tool", "name": body.name, "args": args or {}, "out": out})
    logger.info(f"assistant tool {body.name} {json.dumps(args, ensure_ascii=False)[:200]} -> {json.dumps(out, ensure_ascii=False)[:200]}")
    return {"output": out}


@router.post("/log")
def log_turns(body: LogBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    for t in body.turns:
        who, text = (t.get("who") or "?"), (t.get("text") or "").strip().replace("\n", " ")
        if text:
            log_line(f"[{body.session}] {who}: {text}")
            conv_add(body.session, {"who": who, "text": text})
    return {"ok": True}


@router.post("/usage")
def usage_post(body: UsageBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    if not _conv_path(body.session):
        raise HTTPException(404, "unknown session")
    return record_usage(body.session, body.kind, body.usage or {})


@router.post("/end")
def end(body: EndBody, authorization: Optional[str] = Header(None)):
    """The phone closed the conversation: summarise it (a few seconds later, so the last lines land first)."""
    require_auth(authorization)
    p = _conv_path(body.session)
    if p and body.discard:
        if not any(r.get("who") in ("you", "umbra", "tool") for r in _conv_recs(p)):
            p.unlink(missing_ok=True)
    elif p:
        summarize_later(body.session, delay=4)
    return {"ok": True}


@router.get("/conversations")
def conversations_list(limit: int = 60, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return conversations(limit)


@router.get("/conversations/{session}")
def conversation_get(session: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    p = _conv_path(session)
    if not p:
        raise HTTPException(404)
    return _conv_recs(p)


@router.get("/prompt")
def prompt_get(authorization: Optional[str] = Header(None)):
    """The editable part (prompt.md) and the full prompt a voice session gets right now."""
    require_auth(authorization)
    text = _read("prompt.md")
    return {"text": text, "is_default": text.strip() == DEFAULT_PROMPT.strip(), "full": instructions()}


@router.put("/prompt")
def prompt_put(body: PromptBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    if len(body.text.strip()) < 20:
        raise HTTPException(400, "prompt too short")
    with _lock:
        (DATA / "prompt.prev.md").write_text(_read("prompt.md"), encoding="utf-8")
        (FILES / "prompt.md").write_text(body.text.rstrip() + "\n", encoding="utf-8")
    return prompt_get(authorization)


@router.post("/prompt/reset")
def prompt_reset(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    with _lock:
        (DATA / "prompt.prev.md").write_text(_read("prompt.md"), encoding="utf-8")
        (FILES / "prompt.md").write_text(DEFAULT_PROMPT + "\n", encoding="utf-8")
    return prompt_get(authorization)


@router.get("/style")
def style_get(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return {"text": _read("style.md"), **style_status()}


@router.post("/style/learn")
def style_learn_now(authorization: Optional[str] = Header(None)):
    """Re-read his WhatsApp and rewrite style.md (~1 min, in the background; poll GET /style)."""
    require_auth(authorization)
    return {"started": style_learn_later(), **style_status()}


@router.get("/files")
def files(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return [{"name": n, "size": (FILES / n).stat().st_size, "modified": (FILES / n).stat().st_mtime}
            for n in FILE_NAMES if (FILES / n).exists()]


@router.get("/files/{name}")
def file_get(name: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    if name not in FILE_NAMES:
        raise HTTPException(404)
    return {"name": name, "text": _read(name)}


@router.put("/files/{name}")
def file_put(name: str, body: FileBody, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    if name not in FILE_NAMES:
        raise HTTPException(404)
    with _lock:
        (FILES / name).write_text(body.text, encoding="utf-8")
    return {"ok": True}


@router.post("/sample")
async def sample(request: Request, kind: str = "pos", authorization: Optional[str] = Header(None)):
    """A recording of the user saying "Umbra" (pos) or room sound without it (neg), for training the wake word."""
    require_auth(authorization)
    if kind not in ("pos", "neg"):
        raise HTTPException(400, "kind must be pos or neg")
    data = await request.body()
    if not data.startswith(b"RIFF") or len(data) > 2_000_000:
        raise HTTPException(400, "expected a WAV file")
    d = DATA / "samples" / kind
    d.mkdir(parents=True, exist_ok=True)
    name = f"user_{_now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}.wav"
    (d / name).write_bytes(data)
    return {"ok": True, "name": name, **sample_counts()}


_WAKE_RE = re.compile(r"\b(h?umbra|úmbra|umbrá)\b", re.I)


def verify_wake(wav: bytes) -> dict:
    """Second opinion on a wake: transcribe the ~2.5 s around it and look for the word "Umbra".
    Rejected audio is kept as a real-life negative for the next wake-word training (samples/neg_auto)."""
    import io
    t0 = time.time()
    key = os.getenv("OPENAI_API_KEY", "")
    boundary = uuid.uuid4().hex
    parts = []
    for name, val in (("model", "gpt-4o-mini-transcribe"), ("language", "pt"),
                      ("prompt", "Umbra é o nome da assistente. Ex.: Umbra, liga o ar.")):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{val}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="wake.wav"\r\n'
                 f'Content-Type: audio/wav\r\n\r\n'.encode() + wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request("https://api.openai.com/v1/audio/transcriptions", b"".join(parts),
                                 {"Authorization": "Bearer " + key, "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=8) as r:
        res = json.loads(r.read())
    text = res.get("text", "")
    ok = bool(_WAKE_RE.search(text))
    if res.get("usage"):
        cost = transcribe_cost(res["usage"], "gpt-4o-mini-transcribe")
        _bill("gpt-4o-mini-transcribe", cost, "assistant-wake-check")
        if ok:  # the conversation this wake opens carries its cost
            _last_wake_check.clear()
            _last_wake_check.update(t=time.time(), cost=cost)
    if not ok:
        d = DATA / "samples" / "neg_auto"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"auto_{_now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:4]}.wav").write_bytes(wav)
    return {"ok": ok, "text": text, "ms": int((time.time() - t0) * 1000)}


_last_wake_check: dict = {}


def sample_counts() -> dict:
    return {k: len(list((DATA / "samples" / k).glob("*.wav"))) if (DATA / "samples" / k).exists() else 0
            for k in ("pos", "neg", "neg_auto")}


@router.post("/verify")
async def verify(request: Request, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    data = await request.body()
    if not data.startswith(b"RIFF") or len(data) > 1_000_000:
        raise HTTPException(400, "expected a short WAV")
    try:
        out = verify_wake(data)
    except Exception as e:  # noqa: BLE001 - the phone falls back to trusting its own model
        raise HTTPException(502, f"transcription failed: {e}")
    logger.info(f"assistant wake check: {out}")
    return out


@router.get("/samples")
def samples(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return sample_counts()


VOICES = ("cedar", "marin", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "alloy")


class SettingsBody(BaseModel):
    ha_token: Optional[str] = None
    ha_url: Optional[str] = None
    voice: Optional[str] = None
    model: Optional[str] = None


@router.put("/settings")
def put_settings(body: SettingsBody, authorization: Optional[str] = Header(None)):
    """Home Assistant URL/token and the voice, set from the xAssistant app (the token never goes through chat)."""
    require_auth(authorization)
    try:
        cur = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    if body.model is not None and body.model not in REALTIME_PRICES:
        raise HTTPException(400, f"model must be one of {list(REALTIME_PRICES)}")
    if body.voice is not None and body.voice not in VOICES:
        raise HTTPException(400, f"voice must be one of {list(VOICES)}")
    for k, v in body.dict().items():
        if v is not None:
            cur[k] = v.strip()
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(cur, indent=1), encoding="utf-8")
    os.chmod(SETTINGS, 0o600)
    return status(authorization)


@router.post("/habits/review")
def habits_review_now(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return {"result": habits_review(), "habits": _read("habits.md")}


@router.get("/timers")
def timers(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return timers_list()


@router.get("/status")
def status(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    s = settings()
    ha = "missing token"
    if s["ha_token"]:
        try:
            _ha("GET", "/api/", timeout=5)
            ha = "ok"
        except Exception as e:  # noqa: BLE001
            ha = f"error: {e}"
    return {"openai": bool(os.getenv("OPENAI_API_KEY")), "home_assistant": ha, "voice": s["voice"], "model": s["model"],
            "models": list(REALTIME_PRICES), "voices": list(VOICES),
            "files": str(FILES), "timers": len(_load_timers())}


_started = False


def start():
    """Called once from hub.attach(): seed the files and run the timer thread."""
    global _started
    if _started:
        return
    _started = True
    _ensure_files()
    threading.Thread(target=_timer_loop, name="assistant-timers", daemon=True).start()
    threading.Thread(target=_habits_loop, name="assistant-habits", daemon=True).start()
