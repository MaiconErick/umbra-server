"""
Image generation helpers: what each picture really cost, the list of image
models the app's picker bar shows, and "code art" (a text model writes a
Pillow script, the Pi runs it → PNG).

Prices are USD per 1M tokens. OpenAI and Gemini bill images as tokens
(~1290 output tokens for a 1024² Gemini picture; OpenAI depends on quality),
so the real cost comes from each response's usage. OpenRouter reports the
charged amount directly.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

# provider/model → (text in, image in, image out) per 1M tokens, or flat per image
OPENAI = {
    "gpt-image-1":      (5.0, 10.0, 40.0),
    "gpt-image-1-mini": (2.0, 2.5, 8.0),
    "gpt-image-1.5":    (5.0, 8.0, 32.0),
    "gpt-image-2":      (5.0, 8.0, 30.0),
    "gpt-image-2.5":    (5.0, 8.0, 30.0),
}
OPENAI_FLAT = {"dall-e-3": 0.04, "dall-e-2": 0.02}
GEMINI = {  # (input, image out)
    "gemini-2.5-flash-image":      (0.30, 30.0),
    "gemini-3.1-flash-lite-image": (0.25, 30.0),
    "gemini-3.1-flash-image":      (0.50, 60.0),
    "gemini-3-pro-image":          (2.00, 120.0),
    "gemini-nano-banana-2.1":      (1.50, 30.0),
    "nano-banana-pro":             (2.00, 120.0),
}


def _match(table: dict, model: str):
    m = (model or "").lower()
    for k in sorted(table, key=len, reverse=True):
        if m.startswith(k):
            return table[k]
    return None


def openai_cost(model: str, usage) -> float:
    flat = _match(OPENAI_FLAT, model)
    if flat is not None:
        return flat
    p = _match(OPENAI, model) or OPENAI["gpt-image-1"]
    if usage is None:
        return 0.0
    g = (lambda o, k: (o.get(k) if isinstance(o, dict) else getattr(o, k, None)) or 0)
    det = g(usage, "input_tokens_details") or {}
    txt = g(det, "text_tokens") if det else 0
    img = g(det, "image_tokens") if det else 0
    if not det:
        txt = g(usage, "input_tokens")
    out = g(usage, "output_tokens")
    return (txt * p[0] + img * p[1] + out * p[2]) / 1e6


def gemini_cost(model: str, usage_meta: dict | None) -> float:
    p = _match(GEMINI, model.split("/")[-1]) or GEMINI["gemini-2.5-flash-image"]
    u = usage_meta or {}
    inp = int(u.get("promptTokenCount") or 0)
    out = int(u.get("candidatesTokenCount") or 0) + int(u.get("thoughtsTokenCount") or 0)
    if not out:
        out = 1290
    return (inp * p[0] + out * p[1]) / 1e6


def openrouter_cost(usage) -> float | None:
    if usage is None:
        return None
    c = getattr(usage, "cost", None)
    if c is None:
        c = (getattr(usage, "model_extra", None) or {}).get("cost")
    return float(c) if c is not None else None


def record(provider: str, model: str, cost: float, prompt: str, sid: str | None, elapsed: float = 0.0):
    try:
        from store import usage as U
        U.record_turn(sid=sid, provider=provider, model=model, input_tokens=0, output_tokens=0,
                      actual_cost=max(0.0, float(cost or 0)), category="image", label=(prompt or "")[:100],
                      elapsed_sec=round(float(elapsed or 0), 2))
    except Exception:
        pass


# ── the image-model picker ──────────────────────────────────
# What each model is good at (written by hand; the Arena rank/score next to it
# is fetched daily from arena.ai's text-to-image leaderboard).
#   id → (name, provider, ≈$/picture, arena name, tags, note)
MODELS = [
    ("gpt-image-2.5-flare", "GPT Image 2.5 Flare", "openai", 0.013, "gpt-image-2.5-flare",
     ["best value", "#2 quality", "text in images", "edits"],
     "Top-tier quality for about a cent: in my test it was the fastest OpenAI model (13 s, $0.013). Polished, detailed scenes and exact text. "
     "OpenAI refuses some fan art (e.g. Pokémon) — then it switches to Nano Banana automatically."),
    ("gpt-image-2.5-sunburst", "GPT Image 2.5 Sunburst", "openai", 0.013, "gpt-image-2.5-sunburst",
     ["#1 quality", "text in images", "long prompts", "edits"],
     "The best-rated image model right now. Same price as Flare, a bit slower (~23 s), slightly richer scenes. "
     "Best pick for posters, packaging, signs and long, detailed prompts."),
    ("gemini-nano-banana-2.1", "Nano Banana 2.1", "gemini", 0.07, "gemini-nano-banana-2.1",
     ["Google's best", "edits", "keeps characters", "fan art OK"],
     "Google's best (~15 s, ~$0.07). Clean, cute results and very good at editing your pictures while keeping the same character. "
     "Draws fan art that OpenAI refuses."),
    ("gemini-3.1-flash-lite-image", "Nano Banana 2 Lite", "gemini", 0.045, "gemini-3.1-flash-lite-image (nano-banana-2-lite)",
     ["fastest", "drafts", "edits"],
     "The quick one — about 4 s per picture. Decent quality; great for drafts and lots of variations. Picks its own (often wide) framing."),
    ("gemini-2.5-flash-image", "Nano Banana (original)", "gemini", 0.039, "gemini-2.5-flash-image-preview (nano-banana)",
     ["fast", "cheap", "older"],
     "The first Nano Banana: ~6 s and $0.039 with nice, simple results. Older and lower-rated than the newer ones."),
    ("gpt-image-2", "GPT Image 2", "openai", 0.053, "gpt-image-2 (medium)",
     ["top quality", "slow", "edits"],
     "Still near the top, but slower (~40 s) and 4× the price of GPT Image 2.5 — no reason to pick it now."),
    ("gemini-3.1-flash-image", "Nano Banana 2", "gemini", 0.09, "gemini-3.1-flash-image (nano-banana-2) [web-search]",
     ["good quality", "edits"],
     "Good all-rounder (~10 s) but pricier (~$0.09) and lower-rated than Nano Banana 2.1. Picks its own (often wide) framing."),
    ("gemini-3-pro-image", "Nano Banana Pro", "gemini", 0.17, "gemini-3-pro-image-2k (nano-banana-pro)",
     ["2K/4K output", "pricey"],
     "Big high-resolution pictures and dense infographics. ~$0.17 and ~20 s, and now ranked below Nano Banana 2.1. "
     "In my test it returned two copies side by side."),
    ("gpt-image-1.5", "GPT Image 1.5", "openai", 0.047, "gpt-image-1.5-high-fidelity",
     ["older", "edits"], "Older OpenAI model (~13 s, ~$0.047). The 2.5 models are better and cheaper."),
    ("gpt-image-1", "GPT Image 1", "openai", 0.042, "gpt-image-1",
     ["older"], "The server's old default (~15–20 s, ~$0.042). The 2.5 models are better and cheaper."),
    ("gpt-image-1-mini", "GPT Image 1 Mini", "openai", 0.0085, "gpt-image-1-mini",
     ["cheapest", "older"], "The cheapest picture (under a cent, ~12 s). Fine for quick drafts; clearly below the 2.5 models."),
    ("code/anthropic/claude-sonnet-5.5", "Code art · Claude Sonnet 5.5", "code", 0.1, None,
     ["true pixel art", "exact pixel version", "icons", "slow"],
     "Claude writes a Python script that draws on a real pixel grid — the only TRUE pixel art here (the others make pixel-*style* "
     "paintings). Simpler compositions, but crisp sprites, icons and diagrams. With a reference it pixelates your actual picture, "
     "so the pixel version matches it 1:1. 1–2 min, $0.07–0.13."),
    ("code/openai/gpt-5-mini", "Code art · GPT-5 Mini", "code", 0.015, None,
     ["cheap", "crude"],
     "Same idea with a small model: very crude in my test. Only for simple shapes, patterns and test sprites."),
]

# OpenRouter-only image models (the direct-key versions above are cheaper than the same model via OpenRouter)
OPENROUTER_EXTRA = {
    "openai/gpt-5.4-image-2": ("gpt-image-2 (medium)", ["via OpenRouter"],
                               "GPT Image 2 through OpenRouter, paid from your OpenRouter credits instead of the OpenAI key."),
}

from store import paths as _P
_ARENA_FILE = os.path.join(_P.CACHE, "image_arena.json")
SAMPLES_DIR = os.path.join(_P.CACHE, "image_samples")


def arena() -> dict:
    """{arena model name: {rank, score}} from arena.ai, refreshed once a day."""
    import json
    try:
        if os.path.exists(_ARENA_FILE) and time.time() - os.path.getmtime(_ARENA_FILE) < 86400:
            return json.load(open(_ARENA_FILE))
    except Exception:
        pass
    out = {}
    try:
        import html as _html
        import requests
        t = requests.get("https://arena.ai/leaderboard/text-to-image", timeout=20,
                         headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128 Safari/537.36"}).text
        for row in t.split("<tr")[1:]:
            txt = [_html.unescape(x).strip() for x in re.split(r"<[^>]+>", row) if x.strip()]
            sc = re.search(r'body-sm">(\d{3,4})</span><span class="text-text-tertiary', row)
            if sc and len(txt) > 4 and txt[1].isdigit():
                out[txt[4]] = {"rank": int(txt[1]), "score": int(sc.group(1))}
        if out:
            os.makedirs(os.path.dirname(_ARENA_FILE), exist_ok=True)
            json.dump({"_total": len(out), **out}, open(_ARENA_FILE, "w"))
    except Exception:
        try:
            return json.load(open(_ARENA_FILE))
        except Exception:
            return {}
    return {"_total": len(out), **out}


def _measured() -> dict:
    """Real average cost and time per picture by model, from what was actually charged."""
    try:
        import sqlite3
        from store.usage import USAGE_DB
        c = sqlite3.connect(USAGE_DB)
        rows = c.execute("SELECT provider, model, AVG(input_cost+output_cost+cache_read_cost+cache_write_cost), COUNT(*), "
                         "AVG(NULLIF(elapsed_sec, 0)) FROM turns WHERE category='image' GROUP BY provider, model").fetchall()
        c.close()
        return {f"{r[0]}|{r[1]}": (r[2] or 0, r[3], r[4]) for r in rows}
    except Exception:
        return {}


def catalog() -> list[dict]:
    """Image models for the picker: id (what default_image_model stores), name, source, price, quality, speed, notes."""
    ar = arena()
    total = ar.get("_total") or 0
    out = []
    for mid, name, prov, est, aname, tags, note in MODELS:
        out.append({"id": mid, "provider": prov, "model": mid.split("/", 1)[1] if prov == "code" else mid,
                    "name": name, "est": est, "tags": tags, "note": note, "arena": aname})
    try:
        from api import explore as _ex
        names = {m["id"]: m for m in _ex.get(False).get("models", [])}
    except Exception:
        names = {}
    for oid, (aname, tags, note) in OPENROUTER_EXTRA.items():
        m = names.get(oid)
        if m:
            out.append({"id": oid, "provider": "openrouter", "model": oid, "name": m["name"],
                        "est": m.get("per_image") or 0, "tags": tags, "note": note, "arena": aname})
    meas = _measured()
    for r in out:
        a = ar.get(r.pop("arena") or "")
        if a:
            r["rank"], r["score"], r["ranked_of"] = a["rank"], a["score"], total
        prov = "openrouter" if r["provider"] == "code" else r["provider"]
        key = f"{prov}|{'code/' + r['model'] if r['provider'] == 'code' else r['model']}"
        m = meas.get(key)
        if m and m[1]:
            r["avg"], r["made"] = round(m[0], 4), m[1]
            if m[2]:
                r["secs"] = round(m[2], 1)
        if os.path.exists(os.path.join(SAMPLES_DIR, _sample_name(r["id"]))):
            r["sample"] = _sample_name(r["id"])
    return out


def display_name(model: str) -> str:
    m = (model or "").lower()
    for mid, name, *_ in MODELS:
        if m == mid or m == mid.split("/", 1)[-1] or (m.startswith(mid) and not mid.startswith("code/")):
            return name
    return model


_TYPICAL = {"openai": 20.0, "gemini": 12.0, "openrouter": 15.0, "code": 90.0}


def expected_secs(provider: str, model: str) -> float:
    """How long this model usually takes here (measured), for the app's progress bar."""
    prov = "openrouter" if provider == "code" else provider
    key = f"{prov}|{model if provider != 'code' or model.startswith('code/') else 'code/' + model}"
    m = _measured().get(key)
    if m and m[2]:
        return round(float(m[2]), 1)
    return _TYPICAL.get(provider, 15.0)


def _sample_name(mid: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "_", mid.lower()) + ".jpg"


def save_sample(mid: str, path: str):
    """Keep a small copy of one picture per model so the picker can show what it makes."""
    try:
        from PIL import Image
        os.makedirs(SAMPLES_DIR, exist_ok=True)
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((512, 512))
            im.save(os.path.join(SAMPLES_DIR, _sample_name(mid)), quality=88)
    except Exception:
        pass


# ── code art ─────────────────────────────────────────────────
_CODE_SYS = """You are a pixel artist and creative coder. Write ONE complete Python 3 script that draws the
requested picture with Pillow (PIL) — numpy is available too. Rules:
- Save a PNG to the path in os.environ["OUT"]. Nothing else touches the disk; no network.
- Pixel art: draw on a small grid (16–64 px) with a hand-picked palette, outlines and shading,
  then upscale with Image.NEAREST so the final image is 512–1024 px.
- Other art (icons, logos, diagrams, patterns, charts): draw at 1024 px with anti-aliasing (draw at 2x, downscale LANCZOS).
- Make it look good: clear silhouette, consistent light source, a background that suits it.
- If reference images are given they are attached AND on disk: os.environ["REF"] = paths joined by "|".
  To make a pixel / simplified version of a reference, LOAD it and work from its real pixels:
  crop to the subject, downscale to the target grid (Image.BOX), quantize to a 12–32 colour palette,
  clean stray pixels, add a 1 px dark outline around the subject, upscale with NEAREST. Keep pose, colours, layout.
Reply with only the code in one ```python block."""


def code_art(prompt: str, model: str, out_path: str, refs: list | None = None, progress=None) -> tuple[str | None, float, str]:
    """→ (path or None, cost USD, error text)."""
    import base64
    from openai import OpenAI
    client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.getenv("OPENROUTER_API_KEY"))
    refs = refs or []
    content = prompt
    if refs:
        content = [{"type": "text", "text": prompt + "\n(Reference image(s) attached; on disk via os.environ['REF'].)"}]
        for r in refs:
            ext = r.rsplit(".", 1)[-1].lower()
            mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
            with open(r, "rb") as fh:
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64," + base64.b64encode(fh.read()).decode()}})
    msgs = [{"role": "system", "content": _CODE_SYS}, {"role": "user", "content": content}]
    cost, err = 0.0, ""
    say = progress or (lambda **_: None)
    for attempt in range(2):
        say(stage="writing" if attempt == 0 else "fixing", attempt=attempt + 1)
        r = client.chat.completions.create(model=model, messages=msgs, max_tokens=8000,
                                           extra_body={"usage": {"include": True}})
        cost += openrouter_cost(r.usage) or 0.0
        text = r.choices[0].message.content or ""
        m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.S)
        code = m.group(1) if m else text
        work = os.path.join(os.path.dirname(out_path), f"codeart_{int(time.time() * 1000)}.py")
        with open(work, "w", encoding="utf-8") as f:
            f.write(code)
        say(stage="running", attempt=attempt + 1)
        try:
            p = subprocess.run([sys.executable, work], env={**os.environ, "OUT": out_path, "REF": "|".join(refs)},
                               cwd=os.path.dirname(out_path), capture_output=True, text=True, timeout=90)
            err = (p.stderr or "")[-1500:]
        except subprocess.TimeoutExpired:
            err = "the script ran longer than 90 s"
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return out_path, cost, ""
        msgs += [{"role": "assistant", "content": text},
                 {"role": "user", "content": f"It failed:\n{err or 'no PNG was written'}\nFix it; reply with the full script again."}]
    return None, cost, err or "no PNG was written"
