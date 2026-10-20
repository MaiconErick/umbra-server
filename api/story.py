"""
The debug screen's "story": what Umbra did for each message and why, in plain words.

Raw events (sessions/events/<sid>.jsonl) are grouped per message, oldest first, into
sections — Your message, Reading what Umbra knows, Preparing the question,
Web search, Making a picture, Writing the reply, After the reply — each with a short
explanation, and every step with its time (hh:mm:ss), how long it took and a
one-line detail (which file, which line, what the AI answered…).
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from fastapi import APIRouter, Header

router = APIRouter(prefix="/api/v2")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GROUPS = {
    "message": ("📥", "Your message",
                "Umbra received your message and saved it to this chat's history file, so the conversation survives restarts."),
    "memory": ("🧠", "Reading what Umbra knows",
               "Before answering, Umbra collects its instructions, the rules about how to talk to you, facts about the Pi "
               "and the memory pages that match your message. It only reads files — no AI involved — so it takes milliseconds."),
    "prompt": ("📦", "Preparing the question for the AI",
               "Everything from the step above, the chat so far and the list of tools the AI may use are packed into one text "
               "(the prompt). The AI only sees this text. More tokens = a little slower and pricier."),
    "search": ("🔎", "Web search",
               "The AI model can't browse by itself, so it takes two calls: in the first, the AI reads your message and only "
               "decides \"I need to search for X\". Then the search engine (Perplexity) runs that search and returns an answer "
               "with sources. In the second call, the AI reads that result and writes the reply in Umbra's voice."),
    "find": ("🖼", "Finding real pictures",
             "The AI asked for real pictures to show in its reply. Umbra looks them up on Steam, Wikipedia or Openverse "
             "(no AI here) and hands the links back to the AI, which places them in the answer."),
    "picture": ("🎨", "Making a picture",
                "The AI described the picture in a prompt and an image model drew it. The picture goes straight to the chat; "
                "then the AI writes a short message about it."),
    "memwrite": ("💾", "Writing to memory",
                 "The AI itself wrote something into its memory files — usually because you asked it to remember, or it opened "
                 "a memory page it needed."),
    "tools": ("🧰", "Using tools",
              "The AI asked to run a tool (shell, files, browser…) and waited for its result before continuing."),
    "reply": ("✍️", "Writing the reply",
              "With everything above in hand, the AI writes the answer you see. This is usually the slowest part: the text "
              "is generated word by word."),
    "after": ("🌙", "After the reply (in the background)",
              "These run after you already had the answer, so you never wait for them: a note-taker checks whether you said "
              "something worth remembering, and the chat's summary/title is refreshed. That's why they show up a few seconds later."),
    "error": ("⚠️", "Problems", "Something failed during this message."),
}

_TOOL_GROUP = {"web_search": "search", "find_images": "find", "generate_image": "picture", "memory": "memwrite",
               "read_memory": "memwrite"}

_FRIENDLY_FILES = {
    "system_prompt.md": "Umbra's instructions (system prompt)",
    "rules.txt": "Permanent rules (old file)",
    "system.txt": "Facts about the Pi (IPs, paths)",
}

_PROMPT_PARTS = {
    "system_prompt": "instructions", "about_me": "about you", "rules": "rules", "system_info": "Pi facts",
    "notebook_map": "memory map", "notes": "your memories", "conversation": "chat so far", "tools_schema": "tool list",
    "summary_rules": "summary rules", "proactive_mode": "reminders setting", "date": "today's date",
    "summary": "chat summary", "reco_hint": "recommendation hint", "show_hint": "'show me' hint", "table_hint": "table hint",
}


def _hms(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _load(sid: str) -> list[dict]:
    p = os.path.join(BASE_DIR, "sessions", "events", f"{sid}.jsonl")
    out = []
    if not os.path.exists(p):
        return out
    for line in open(p, encoding="utf-8"):
        try:
            e = json.loads(line)
            ts = e.get("ts")
            e["ts"] = float(ts) if not isinstance(ts, (int, float)) else ts
            out.append(e)
        except Exception:
            continue
    return out


def _raw(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in ("title", "reason", "_level", "_wall_gap_ms", "_dt_ms",
                                                   "system_text", "messages", "tools_schema", "prompt_breakdown")}


def _tool_detail(name: str, args: dict) -> str:
    if name == "web_search":
        return f"Search: “{args.get('query', '')}”" + (" · with Reddit" if args.get("reddit") else "")
    if name == "find_images":
        qs = args.get("queries") or []
        return f"Looking for: {', '.join(map(str, qs))[:300]}" + (f" ({args.get('kind')})" if args.get("kind") else "")
    if name == "generate_image":
        bits = [f"Prompt: “{str(args.get('prompt', ''))[:400]}”"]
        if args.get("reference") and args.get("reference") != "none":
            bits.append(f"working from: {args.get('reference')} picture")
        if args.get("model"):
            bits.append(f"model asked: {args['model']}")
        return " · ".join(bits)
    if name == "memory":
        op = args.get("op")
        if op == "read":
            return f"Opened memory page {args.get('page') or args.get('topic')}" + (f" [{args['section']}]" if args.get("section") else "")
        return f"{op}: {args.get('page', '')} [{args.get('section', '')}] {args.get('text') or args.get('rule') or ''}".strip()
    return ", ".join(f"{k}={str(v)[:120]}" for k, v in (args or {}).items())[:400]


def build(sid: str, limit_turns: int = 40) -> dict:
    events = _load(sid)
    # A new card starts at every "turn.start". The agent's turn counter restarts when
    # Umbra restarts, so a later event goes to the most recent card with its counter.
    cards: list[dict] = []

    def new_turn(idx: int, ts: float) -> dict:
        t = {"idx": idx, "start": ts, "user": "", "groups": [], "_g": {}, "end": None,
             "total_ms": None, "model": "", "tokens": None}
        cards.append(t)
        return t

    def turn_for(idx: int) -> Optional[dict]:
        for t in reversed(cards):
            if t["idx"] == idx:
                return t
        return cards[-1] if cards else None

    def group(t: dict, key: str, sub: str = "") -> dict:
        gk = key + sub
        g = t["_g"].get(gk)
        if g is None:
            emoji, title, why = GROUPS[key]
            g = {"key": key, "emoji": emoji, "title": title + (f" {sub}" if sub else ""), "why": why, "steps": []}
            t["_g"][gk] = g
            t["groups"].append(g)
        return g

    def step(g: dict, ts: float, title: str, detail: str = "", ms: Optional[int] = None, raw: Optional[dict] = None):
        g["steps"].append({"at": ts, "time": _hms(ts), "title": title, "detail": detail, "ms": ms,
                           "raw": raw or {}})

    pending_req: dict[int, float] = {}       # loop → request ts
    pending_resp: dict[int, dict] = {}       # loop → llm_response data
    tool_start: dict[str, tuple] = {}        # call_id → (ts, name, args, group)
    summary_start: Optional[float] = None
    calls_in_turn: dict[int, int] = {}

    for e in events:
        typ, d, ts = e.get("type", ""), e.get("data") or {}, e["ts"]
        idx = int(d.get("_turn_idx") or 0)
        if typ == "turn.start":
            t = new_turn(idx, ts)
        else:
            t = turn_for(idx)
            if t is None:
                continue
        if typ == "turn.start":
            t["user"] = d.get("user_message_preview", "")
            t["start"] = ts
            step(group(t, "message"), ts, "Got your message", f"“{t['user']}”", raw=_raw(d))
        elif typ == "mem.session:log_append":
            who = "your message" if d.get("role") == "user" else "Umbra's reply"
            g = group(t, "message" if d.get("role") == "user" else "reply")
            step(g, ts, f"Saved {who} in the chat history", f"File {d.get('file')} · {d.get('bytes', 0)} bytes",
                 ms=d.get("_dt_ms"), raw=_raw(d))
        elif typ == "mem.memory:read":
            f = os.path.basename(d.get("file", ""))
            name = "this chat's summary" if "/summary/" in d.get("file", "") else _FRIENDLY_FILES.get(f, f)
            empty = not d.get("bytes")
            step(group(t, "memory"), ts, f"Read {name}",
                 f"{d.get('file')} · " + ("empty — nothing in it yet" if empty else f"{d.get('tokens', 0):,} tokens"),
                 ms=d.get("_dt_ms"), raw=_raw(d))
        elif typ == "mem.notebook":
            picked = d.get("picked") or {}
            det = ("; ".join(f"{p} → {', '.join(s)}" for p, s in picked.items())
                   if picked else "Nothing in your memory files matched this message, so none were added.")
            step(group(t, "memory"), ts, "Looked for memory pages that match your message",
                 det + " (matching is instant: titles, nicknames and page words — no AI)", ms=d.get("_dt_ms"), raw=_raw(d))
        elif typ == "prompt.assembled":
            bd = d.get("breakdown") or {}
            parts = [f"{_PROMPT_PARTS.get(k, k.split(':')[-1])} {v:,}" for k, v in bd.items()
                     if k != "total_estimated" and v]
            step(group(t, "prompt"), ts, f"Packed the prompt: {bd.get('total_estimated', 0):,} tokens",
                 " · ".join(parts), ms=d.get("_dt_ms"), raw=_raw(d))
        elif typ == "tools.filter":
            on = list(d.get("system_tools") or []) + list(d.get("enabled") or [])
            step(group(t, "prompt"), ts, "Tools the AI may use",
                 ", ".join(dict.fromkeys(on + ["web_search", "find_images"])) + " (others load only when needed)",
                 raw=_raw(d))
        elif typ == "llm_request":
            pending_req[int(d.get("loop") or 1)] = ts
        elif typ == "llm_response":
            pending_resp[int(d.get("loop") or 1)] = {**d, "_ts": ts}
        elif typ == "llm_output":
            loop = int(d.get("loop") or 1)
            req = pending_req.pop(loop, None)
            resp = pending_resp.pop(loop, {}) or {}
            ms = int((resp.get("_ts", ts) - req) * 1000) if req else None
            t["_calls"] = t.get("_calls", 0) + 1
            n = t["_calls"]
            tok = f"read {resp.get('input_tokens', 0):,} tokens, wrote {resp.get('output_tokens', 0):,}"
            if resp.get("cache_read_tokens"):
                tok += f" ({resp['cache_read_tokens']:,} reused from cache — cheaper)"
            tcs = d.get("tool_calls") or []
            if tcs:
                for tc in tcs:
                    name = tc.get("name", "")
                    try:
                        args = json.loads(tc.get("args") or "{}")
                    except Exception:
                        args = {}
                    gk = _TOOL_GROUP.get(name, "tools")
                    what = {"web_search": "decided to search the web", "find_images": "asked for real pictures",
                            "generate_image": "asked for a picture", "memory": f"used its memory files ({args.get('op', '')})",
                            "load_tools": "asked to load more tools"}.get(name, f"asked to run {name}")
                    step(group(t, gk), ts, f"AI call {n}: {what}",
                         _tool_detail(name, args) + f" · {d.get('model')}, {tok}", ms=ms, raw=_raw(d))
            else:
                step(group(t, "reply"), ts, f"AI call {n}: wrote the reply",
                     (d.get("text") or "(empty)") + f"\n— {d.get('model')}, {tok}", ms=ms, raw=_raw(d))
        elif typ == "tool_start":
            name = d.get("tool", "")
            gk = _TOOL_GROUP.get(name, "tools")
            tool_start[d.get("call_id") or name] = (ts, name, d.get("args") or {}, gk)
        elif typ == "tool_end":
            key = d.get("call_id") or d.get("tool")
            st = tool_start.pop(key, None)
            name = d.get("tool", "")
            g = group(t, st[3] if st else _TOOL_GROUP.get(name, "tools"))
            ms = int((ts - st[0]) * 1000) if st else None
            res = str(d.get("result") or "")
            if name == "web_search":
                engine = res.split("]", 1)[0].replace("[web search via ", "") if res.startswith("[web search") else "search engine"
                body = res.split("\n", 1)[-1]
                step(g, ts, f"Search engine answered ({engine})", body[:700], ms=ms, raw=_raw(d))
            elif name == "generate_image":
                im = d.get("image") or {}
                det = f"{im.get('model', '?')} ({im.get('provider', '?')})"
                if im.get("cost"):
                    det += f" · ${im['cost']:.4f}"
                if im.get("note"):
                    det += f" · ⚠ {im['note']}"
                step(g, ts, "Image model finished the picture" if im else "Picture failed", det if im else res[:300], ms=ms, raw=_raw(d))
            elif name == "find_images":
                found = [ln for ln in res.splitlines() if ln.startswith("- ")]
                step(g, ts, f"Found {sum(1 for f in found if 'nothing found' not in f)} picture(s)",
                     "\n".join(f[:160] for f in found[:8]), ms=ms, raw=_raw(d))
            else:
                step(g, ts, f"{name} finished", res[:500], ms=ms, raw=_raw(d))
        elif typ == "llm_error":
            step(group(t, "error"), ts, "The AI call failed", str(d.get("error") or d)[:500], raw=_raw(d))
        elif typ == "tools_loaded":
            step(group(t, "tools"), ts, "Loaded more tools", ", ".join(d.get("names") or d.get("tools") or []), raw=_raw(d))
        elif typ == "turn.end":
            t["end"] = ts
            t["total_ms"] = int(float(d.get("duration_sec") or 0) * 1000) or None
            t["model"] = d.get("model", "")
            t["tokens"] = (d.get("msg_input_tokens"), d.get("msg_output_tokens"))
        elif typ == "notes_checked":
            ch = d.get("changes") or []
            if ch:
                det = "\n".join(f"{c.get('op')}: {c.get('page', '')} [{c.get('section', '')}] — {c.get('text') or c.get('old') or ''}"
                                for c in ch)
                title = f"Note-taker saved {len(ch)} thing(s) about you"
            elif d.get("skipped"):
                det, title = f"Skipped: {d['skipped']}", "Note-taker: nothing to check"
            elif d.get("error"):
                det, title = d["error"], "Note-taker failed"
            else:
                det, title = "Read your message and Umbra's reply; nothing new worth remembering.", "Note-taker: nothing new"
            step(group(t, "after"), ts, title, det + (f"\n— {d.get('model')}" if d.get("model") else ""),
                 ms=d.get("ms"), raw=_raw(d))
        elif typ == "mem.summary.refresh:start":
            summary_start = ts
        elif typ in ("mem.summary.refresh:done", "mem.summary.refresh:noop", "mem.summary.refresh:error"):
            ms = int((ts - summary_start) * 1000) if summary_start else None
            summary_start = None
            if typ.endswith("done"):
                step(group(t, "after"), ts, "Refreshed this chat's summary",
                     f"“{d.get('summary', '')}” — used to remember long chats; {d.get('provider', '')}/{d.get('model', '')}",
                     ms=ms, raw=_raw(d))
            elif typ.endswith("noop"):
                step(group(t, "after"), ts, "Chat summary checked — no change needed", "", ms=ms, raw=_raw(d))
            else:
                step(group(t, "error"), ts, "Chat summary refresh failed", str(d.get("error"))[:300], ms=ms, raw=_raw(d))

    out = []
    for t in cards[-limit_turns:]:
        if not t["groups"]:
            continue
        for g in t["groups"]:
            ats = [s["at"] for s in g["steps"]]
            g["time"] = _hms(min(s["at"] - (s["ms"] or 0) / 1000 for s in g["steps"])) if ats else ""
            starts = [s["at"] - (s["ms"] or 0) / 1000 for s in g["steps"]]
            g["ms"] = int((max(ats) - min(starts)) * 1000) if ats else None
            g["after_reply_s"] = round(min(ats) - t["end"], 1) if (t["end"] and ats and g["key"] == "after") else None
        # keep the natural order: message → memory → prompt → tools/search/pictures → reply → after → errors
        rank = {"message": 0, "memory": 1, "prompt": 2, "memwrite": 3, "search": 4, "find": 5, "picture": 6,
                "tools": 7, "reply": 8, "after": 9, "error": 10}
        t["groups"].sort(key=lambda g: (rank.get(g["key"], 7), g["time"]))
        del t["_g"]
        t.pop("_calls", None)
        t["time"] = _hms(t["start"])
        out.append(t)
    return {"turns": out}


@router.get("/sessions/{sid}/story")
def story(sid: str, authorization: Optional[str] = Header(None)):
    from api.hub import require_auth
    require_auth(authorization)
    return build(sid)
