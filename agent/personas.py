"""
Personas registry — chat personas loaded from /home/pi/MaiconErick/Umbra/personas/.

Each persona is a folder containing:
  PERSONA.md   — full persona definition, used as chat sys_primary
  README.md    — display metadata (tagline, "best for", traits, model, chat_style)
  <icon>.{png,jpg,jpeg,webp,avif,gif}       — optional avatar shown in picker
  background.{png,jpg,jpeg,webp,avif,gif}   — optional full-surface chat background

Public API:
  list_personas()                 -> [PersonaMeta dict, ...]
  get_persona(name)               -> PersonaMeta dict + full markdown bodies, or None
  read_persona_md(name)           -> str
  read_readme_md(name)            -> str
  write_persona_md(name, text)    -> bool
  write_readme_md(name, text)     -> bool
  find_icon_path(name)            -> str | None
  save_icon(name, data, ext)      -> str (new path) | None
  find_background_path(name)      -> str | None
  save_background(name, data, ext)-> str (new path) | None
  load_persona_for_chat(name)     -> str   (PERSONA.md text, or "" if missing)
"""
from __future__ import annotations

import os, re, json
from typing import Optional

from store.manager import count_tokens

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERSONAS_DIR = os.path.join(BASE_DIR, "personas")

IMAGE_EXTS = ("png", "jpg", "jpeg", "webp", "avif", "gif")
ICON_EXTS  = IMAGE_EXTS  # kept as alias in case anything imports it
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _persona_dir(name: str) -> Optional[str]:
    if not _SAFE_NAME.match(name or ""):
        return None
    path = os.path.join(PERSONAS_DIR, name)
    return path if os.path.isdir(path) else None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s).strip()


def _humanize(slug: str) -> str:
    return " ".join(p.capitalize() for p in slug.replace("_", "-").split("-"))


def _parse_readme(readme: str, fallback_name: str) -> dict:
    """Extract display_name, tagline, best_for, traits[], model, chat_style.
    Backwards-compatible with the three shapes seen in upstream SOUL.md-style
    READMEs, plus two new keys:
      model:        a `model: provider/model-id` one-liner (anywhere in file)
      chat_style:   a fenced ```json block right after a `## Chat Style` heading
    """
    text = readme or ""
    display_name = ""
    tagline = ""
    best_for = ""
    traits: list[str] = []
    model = ""
    chat_style: Optional[dict] = None

    # H1 — plain markdown first, HTML fallback
    m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
    if m:
        display_name = _strip_html(m.group(1)).strip()
    if not display_name:
        m = re.search(r"<h1[^>]*>(.+?)</h1>", text, re.S | re.I)
        if m:
            display_name = _strip_html(m.group(1)).strip()
    if not display_name:
        display_name = _humanize(fallback_name)

    # Tagline — HTML <em> first, then a plain *italic* line near the top
    m = re.search(r"<em>(.+?)</em>", text, re.S | re.I)
    if m:
        tagline = _strip_html(m.group(1)).strip()
    if not tagline:
        for line in text.splitlines()[:30]:
            s = line.strip()
            if (s.startswith("*") and s.endswith("*") and len(s) > 4
                    and not s.startswith("**")):
                tagline = s.strip("* ").strip()
                break

    # Best for — HTML <strong>Best for</strong> ... or ## Best For section
    m = re.search(r"<strong>\s*Best for\s*</strong>\s*(.+?)(?:</p>|\n\n)",
                  text, re.S | re.I)
    if m:
        best_for = _strip_html(m.group(1)).strip().rstrip(".")
    if not best_for:
        m = re.search(r"^##+\s*Best\s*For\s*\n+(.+?)(?:\n##|\Z)", text,
                      re.S | re.I | re.M)
        if m:
            best_for = m.group(1).strip().split("\n\n")[0].strip().rstrip(".")

    # Traits — "### Traits" or "## Traits" followed by a bullet list
    m = re.search(r"^##+\s*Traits\s*\n+(.+?)(?:\n##|\Z)", text,
                  re.S | re.I | re.M)
    if m:
        block = m.group(1)
        for line in block.splitlines():
            s = line.strip()
            if s.startswith(("*", "-", "•")):
                traits.append(s.lstrip("*-• ").strip())

    # Model — one-liner anywhere: `model: anthropic/claude-haiku-4-5-20251001`
    m = re.search(r"^\s*model\s*:\s*(\S+)\s*$", text, re.M | re.I)
    if m:
        model = m.group(1).strip()

    # Needs-proactive — one-liner, boolean; personas that must be able to push
    # notifications (e.g. Events) auto-enable allow_proactive on their chats.
    needs_proactive = False
    m = re.search(r"^\s*needs[_-]?proactive\s*:\s*(true|false|yes|no|1|0)\s*$",
                  text, re.M | re.I)
    if m:
        needs_proactive = m.group(1).lower() in ("true", "yes", "1")

    # Enable-tools — comma-separated tool names the persona needs available in
    # its chats. session_persona_set copies these into chat_meta.enabled_tools
    # so the model actually sees them in the tool schema (Umbra sends [] by
    # default). E.g. `enable_tools: read_file, write_file` for the FBI persona.
    enable_tools: list[str] = []
    m = re.search(r"^\s*enable[_-]?tools\s*:\s*(.+?)\s*$", text, re.M | re.I)
    if m:
        enable_tools = [t.strip() for t in m.group(1).split(",") if t.strip()]

    # Reasoning-effort — per-persona override for the reasoning_effort param
    # sent to gpt-5*/o-series models. Valid values: minimal | low | medium | high.
    # Personas whose job is fast orchestration (Muse) can drop this to
    # "minimal" so gpt-5-mini doesn't burn 300+ hidden reasoning tokens per
    # turn (which is the biggest single latency source for those models).
    reasoning_effort = ""
    m = re.search(r"^\s*reasoning[_-]?effort\s*:\s*(\S+)\s*$", text, re.M | re.I)
    if m:
        val = m.group(1).strip().lower()
        if val in ("minimal", "low", "medium", "high"):
            reasoning_effort = val

    # Options (one-liners in README.md):
    #   standalone: yes   → skip Umbra's own instruction files and notebook; the persona is the whole prompt
    #   notebook: private-fbi → the notebook area only this persona sees (always loaded in full)
    #   note_taker: off   → don't run the background note-taker in its chats
    #   secret: yes       → its chats are hidden from the chat list (opened from the vault)
    #   vanish: yes       → messages vanish word by word in the app
    options = {}
    for key in ("standalone", "notebook", "memory", "note_taker", "secret", "vanish"):
        m = re.search(rf"^\s*{key}\s*:\s*(\S+)\s*$", text, re.M | re.I)
        if m:
            v = m.group(1).strip().lower()
            if key == "memory":
                key = "notebook"          # same thing, nicer word
            options[key] = (v in ("yes", "true", "on", "1")) if key != "notebook" else v
            if key == "note_taker":
                options[key] = v not in ("off", "no", "false", "0")

    # Chat style — fenced ```json block right after a `## Chat Style` heading
    m = re.search(
        r"^##+\s*Chat\s*Style\s*\n+```(?:json)?\s*\n(.*?)\n```",
        text, re.S | re.I | re.M,
    )
    if m:
        try:
            chat_style = json.loads(m.group(1))
        except Exception:
            chat_style = None

    # Fallback tagline: first non-heading, non-image paragraph
    if not tagline:
        for para in re.split(r"\n\s*\n", text):
            p = _strip_html(para).strip()
            if not p or p.startswith(("#", "<", "!", "```", "---")):
                continue
            if "Best for" in p or "Best For" in p:
                continue
            tagline = p.split("\n")[0].strip()
            if len(tagline) > 240:
                tagline = tagline[:237] + "..."
            break

    return {
        "display_name": display_name,
        "tagline": tagline,
        "best_for": best_for,
        "traits": traits,
        "model": model,
        "chat_style": chat_style,
        "needs_proactive": needs_proactive,
        "enable_tools": enable_tools,
        "reasoning_effort": reasoning_effort,
        "options": options,
    }


def _find_image_by_stem(name: str, stem: str) -> Optional[str]:
    """Locate <dir>/<stem>.<ext> for the persona `name`."""
    d = _persona_dir(name)
    if not d:
        return None
    for ext in IMAGE_EXTS:
        cand = os.path.join(d, f"{stem}.{ext}")
        if os.path.isfile(cand):
            return cand
    return None


def find_icon_path(name: str) -> Optional[str]:
    d = _persona_dir(name)
    if not d:
        return None
    # Prefer <name>.<ext> or icon.<ext>, then any image that isn't the background/example.
    exact = _find_image_by_stem(name, name) or _find_image_by_stem(name, "icon")
    if exact:
        return exact
    for fn in sorted(os.listdir(d)):
        low = fn.lower()
        if not low.endswith(tuple(f".{e}" for e in IMAGE_EXTS)):
            continue
        if low.startswith("example.") or low.startswith("background."):
            continue
        return os.path.join(d, fn)
    return None


def find_background_path(name: str) -> Optional[str]:
    return _find_image_by_stem(name, "background")


def _persona_md_fallback_tagline(persona_text: str) -> str:
    """When there's no README tagline, pull a one-liner from PERSONA.md."""
    if not persona_text:
        return ""
    m = re.search(r"^##\s+Identity\s*\n+(.+?)(?:\n##|\Z)", persona_text, re.S | re.M)
    block = m.group(1) if m else persona_text
    for para in re.split(r"\n\s*\n", block):
        p = para.strip()
        if not p or p.startswith(("#", ">", "<", "```", "---")):
            continue
        sents = re.split(r"(?<=[.!?])\s+", p)
        acc = ""
        for s in sents:
            cand = (acc + " " + s).strip() if acc else s.strip()
            if len(cand) > 200:
                break
            acc = cand
        if not acc:
            acc = p[:200].rstrip() + ("..." if len(p) > 200 else "")
        return acc
    return ""


def _persona_meta(name: str) -> Optional[dict]:
    d = _persona_dir(name)
    if not d:
        return None
    persona_md_path = os.path.join(d, "PERSONA.md")
    readme_md_path  = os.path.join(d, "README.md")
    persona_text = _read(persona_md_path)
    readme_text  = _read(readme_md_path)
    parsed = _parse_readme(readme_text, name)
    if not parsed["tagline"]:
        parsed["tagline"] = _persona_md_fallback_tagline(persona_text)
    icon_path = find_icon_path(name)
    bg_path   = find_background_path(name)
    return {
        "name": name,
        "display_name": parsed["display_name"],
        "tagline": parsed["tagline"],
        "best_for": parsed["best_for"],
        "traits": parsed["traits"],
        "model": parsed["model"],
        "chat_style": parsed["chat_style"],
        "needs_proactive": parsed["needs_proactive"],
        "enable_tools": parsed["enable_tools"],
        "reasoning_effort": parsed["reasoning_effort"],
        "options": parsed.get("options") or {},
        "persona_md_chars": len(persona_text),
        "persona_md_tokens": count_tokens(persona_text),
        "persona_md_path": persona_md_path,
        "readme_md_path": readme_md_path,
        "has_persona_md": bool(persona_text),
        "has_readme": bool(readme_text),
        "has_icon": icon_path is not None,
        "icon_ext": (os.path.splitext(icon_path)[1].lstrip(".") if icon_path else None),
        "icon_mtime": (int(os.path.getmtime(icon_path)) if icon_path else 0),
        "has_background": bg_path is not None,
        "background_ext": (os.path.splitext(bg_path)[1].lstrip(".") if bg_path else None),
        "background_mtime": (int(os.path.getmtime(bg_path)) if bg_path else 0),
        "updated_at": int(max(
            os.path.getmtime(persona_md_path) if os.path.exists(persona_md_path) else 0,
            os.path.getmtime(readme_md_path) if os.path.exists(readme_md_path) else 0,
        )),
    }


def list_personas() -> list[dict]:
    if not os.path.isdir(PERSONAS_DIR):
        return []
    out = []
    for entry in sorted(os.listdir(PERSONAS_DIR)):
        full = os.path.join(PERSONAS_DIR, entry)
        if not os.path.isdir(full):
            continue
        meta = _persona_meta(entry)
        if meta:
            out.append(meta)
    return out


def get_persona(name: str) -> Optional[dict]:
    meta = _persona_meta(name)
    if not meta:
        return None
    meta = dict(meta)
    meta["persona_md"] = _read(meta["persona_md_path"])
    meta["readme_md"]  = _read(meta["readme_md_path"])
    return meta


def read_persona_md(name: str) -> str:
    d = _persona_dir(name)
    return _read(os.path.join(d, "PERSONA.md")) if d else ""


def read_readme_md(name: str) -> str:
    d = _persona_dir(name)
    return _read(os.path.join(d, "README.md")) if d else ""


def _atomic_write(path: str, text: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def write_persona_md(name: str, text: str) -> bool:
    d = _persona_dir(name)
    if not d:
        return False
    return _atomic_write(os.path.join(d, "PERSONA.md"), text)


def write_readme_md(name: str, text: str) -> bool:
    d = _persona_dir(name)
    if not d:
        return False
    return _atomic_write(os.path.join(d, "README.md"), text)


def _save_image(name: str, data: bytes, ext: str, stem: str) -> Optional[str]:
    """Replace the persona's image with the given stem (icon or background)."""
    d = _persona_dir(name)
    if not d:
        return None
    ext = (ext or "").lower().lstrip(".")
    if ext not in IMAGE_EXTS:
        return None
    for fn in os.listdir(d):
        low = fn.lower()
        for e in IMAGE_EXTS:
            if low == f"{stem}.{e}":
                try:
                    os.remove(os.path.join(d, fn))
                except Exception:
                    pass
    if stem == "icon":
        for fn in os.listdir(d):
            low = fn.lower()
            for e in IMAGE_EXTS:
                if low == f"{name}.{e}":
                    try:
                        os.remove(os.path.join(d, fn))
                    except Exception:
                        pass
    out = os.path.join(d, f"{stem}.{ext}")
    try:
        with open(out, "wb") as f:
            f.write(data)
        return out
    except Exception:
        return None


def save_icon(name: str, data: bytes, ext: str) -> Optional[str]:
    return _save_image(name, data, ext, "icon")


def save_background(name: str, data: bytes, ext: str) -> Optional[str]:
    return _save_image(name, data, ext, "background")


def load_persona_for_chat(name: str) -> str:
    """Return the PERSONA.md text for use as chat sys_primary. Returns '' if the
    persona no longer exists (chat continues with global identity in that case)."""
    return read_persona_md(name) if name else ""
