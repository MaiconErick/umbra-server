"""
"How Umbra thinks": the order in which Umbra reads things for every message,
when each part is used, and the files behind it (editable from the app).
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import APIRouter, Header

router = APIRouter(prefix="/api/v2")
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _f(rel: str, label: str = "") -> dict:
    p = os.path.join(BASE_DIR, rel)
    size = os.path.getsize(p) if os.path.exists(p) else 0
    try:
        from store.manager import count_tokens
        tok = count_tokens(open(p, encoding="utf-8").read()) if size else 0
    except Exception:
        tok = size // 4
    return {"path": rel, "label": label or os.path.basename(rel), "bytes": size, "tokens": tok, "exists": os.path.exists(p)}


def steps() -> list[dict]:
    from store import notebook as NB
    from store.manager import core_files
    from agent import personas as P

    core = [_f(f"memory/core/{n}") for n, _ in core_files()] + [_f("memory/core/ORDER.txt", "ORDER.txt (reading order)")]
    personas = []
    secret = []
    for p in P.list_personas():
        name = p.get("name")
        opts = (P.get_persona(name) or {}).get("options") or {}
        tag = " · " + ", ".join(k for k, v in opts.items() if v and k != "note_taker") if opts else ""
        personas.append(_f(f"personas/{name}/PERSONA.md", f"{p.get('display_name') or name} — personality{tag}"))
        personas.append(_f(f"personas/{name}/README.md", f"{p.get('display_name') or name} — settings (model, options)"))
        if str(opts.get("notebook") or "").startswith("private-"):
            secret += [_f(f"memory/files/{pid}.md") for pid in NB.pages(area=opts["notebook"])]
    pages = [_f(f"memory/files/{pid}.md") for pid in NB.pages() if pid not in NB.ALWAYS]

    return [
        {"title": "Umbra's instructions", "when": "Every message (not in standalone personas like FBI)",
         "why": "Who Umbra is, how it works on the Pi, how to use tools and memory, how to write. "
                "Read in the order of ORDER.txt; any new .md you add to memory/core is read too.",
         "files": core},
        {"title": "Persona", "when": "Chats that use a persona",
         "why": "The persona's personality and rules, added after Umbra's instructions. A 'standalone' persona "
                "(FBI) replaces Umbra's instructions and memory entirely.",
         "files": personas},
        {"title": "This chat's own instructions", "when": "Only if you wrote some in Chat settings",
         "why": "Extra instructions just for one chat (Chat settings → system prompt).", "files": []},
        {"title": "Reminders switch", "when": "Every message",
         "why": "Tells Umbra whether it may schedule a message to you later in this chat (Chat settings → proactive).",
         "files": []},
        {"title": "About you", "when": "Every message", "why": "Who you are, in a few lines.",
         "files": [_f(f"memory/files/{NB.ABOUT}.md")]},
        {"title": "How to talk to you", "when": "Every message", "why": "Permanent rules like \"say vc, never tu\".",
         "files": [_f(f"memory/files/{NB.RULES}.md")]},
        {"title": "Devices", "when": "Every message", "why": "Your machines: the Pi, phone, Thor, PC… with IPs and notes.",
         "files": [_f(f"memory/files/{NB.DEVICES}.md")]},
        {"title": "Persona's private files", "when": "Only in that persona's chats (FBI)",
         "why": "Pages only one persona can see — loaded in full, never shown to other chats or the note-taker.",
         "files": secret},
        {"title": "Memory map", "when": "Every message",
         "why": "Just the page names and their section titles, so Umbra knows what it could open.", "files": []},
        {"title": "Notes that match your message", "when": "When your message mentions a page, title or nickname",
         "why": "The matching sections (plus that page's GENERAL) — found instantly, no AI. For recommendations the "
                "whole page is read.", "files": pages},
        {"title": "Chat summary", "when": "Chats that already have one",
         "why": "A short summary of the chat so far, so long chats aren't forgotten.", "files": []},
        {"title": "Today's date", "when": "Every message", "why": "So 'recent' and 'this year' mean the right thing.",
         "files": []},
        {"title": "Message hints", "when": "Recommendation or 'show me a picture' messages",
         "why": "A one-line nudge: search + find pictures, or find a real photo instead of drawing one.", "files": []},
        {"title": "Tool list", "when": "Every message",
         "why": "memory, web_search, find_images, load_tools — plus any tools loaded earlier in the chat.", "files": []},
        {"title": "The AI answers", "when": "Every message",
         "why": "The model reads all of the above plus the chat so far and replies — calling tools when it needs to.",
         "files": []},
        {"title": "After the reply: note-taker", "when": "Every message (not in personas with note_taker: off)",
         "why": "In the background, a small model checks what you said and updates the memory pages above.",
         "files": []},
        {"title": "After the reply: chat summary and title", "when": "Every message",
         "why": "In the background, refreshes the chat's summary and (early on) its title.", "files": []},
    ]


@router.get("/brain")
def brain(authorization: Optional[str] = Header(None)):
    from api.hub import require_auth
    require_auth(authorization)
    return {"steps": steps()}
