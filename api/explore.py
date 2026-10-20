"""
Model explorer for the app's model picker — what OpenRouter's site shows:

  cards       most used this week (+ change), smartest, best value (smart per $),
              best for coding, best with tools (agentic), best free
  categories  OpenRouter use-case lists (programming, roleplay, translation, …)
  models      every model with vendor, description, prices, context, modalities

Sources: the public /api/v1/models API (+ ?category=) and the data embedded in
openrouter.ai/rankings (weekly usage, Artificial-Analysis benchmark scores).
Cached for 6 hours in memory/models_explore.json.
"""
from __future__ import annotations

import json, logging, os, re, threading, time
from typing import Optional

import requests
from fastapi import APIRouter, Header

from api.hub import require_auth

logger = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from store import paths as _P
CACHE = os.path.join(_P.CACHE, "models_explore.json")
MAX_AGE = 6 * 3600
CATEGORIES = ["programming", "roleplay", "translation", "marketing", "technology", "science",
              "finance", "health", "legal", "academia", "trivia"]
UA = {"User-Agent": "Mozilla/5.0 (xUmbra model picker)"}
router = APIRouter(prefix="/api/v2/models")
_lock = threading.Lock()
_mem: dict = {}


def _vendor(m: dict) -> str:
    n = m.get("name") or ""
    if ": " in n:
        return n.split(": ", 1)[0]
    return (m.get("id") or "?").split("/")[0].replace("-", " ").title()


def _rankings_blob() -> str:
    r = requests.get("https://openrouter.ai/rankings", headers=UA, timeout=30)
    r.raise_for_status()
    return r.text.replace('\\"', '"')


def _query_data(h: str, key: str):
    i = h.find(key)
    if i < 0:
        return None
    j = h.rfind('"state":{"data":', 0, i)
    if j < 0:
        return None
    j += len('"state":{"data":')
    depth, k = 0, j
    while k < len(h):
        c = h[k]
        if c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                break
        k += 1
    return json.loads(h[j:k + 1])


def _blend(m: dict) -> float:
    """Price of a typical call per 1M tokens (3 parts input : 1 part output)."""
    return (3 * m["in"] + m["out"]) / 4


def _picks(by_slug: dict, scores: dict, need_tools: bool = False, free_only: bool = False) -> list:
    """Three honest choices for one goal: the best score, the best score per dollar among strong
    models (>= 75 % of the best) and the cheapest model that is still decent (>= 60 % of the best)."""
    pool = {}
    for k, s in scores.items():
        m = by_slug.get(k)
        if not m or "text" not in (m.get("output") or ["text"]) or (need_tools and not m["tools"]):
            continue
        if m["free"] != free_only:
            continue
        pool[k] = s
    if not pool:
        return []
    top = max(pool.values())
    out, used = [], set()

    def add(role, label, k):
        if k and k not in used:
            used.add(k)
            out.append({"id": k, "role": role, "label": label})
    add("best", "🏆 Best", max(pool, key=pool.get))
    if free_only:
        for k in sorted(pool, key=pool.get, reverse=True)[1:3]:
            add("alt", "🆓 Also free", k)
        return out
    paid = {k: s for k, s in pool.items() if _blend(by_slug[k]) > 0}
    strong = {k: s for k, s in paid.items() if s >= 0.75 * top}
    if strong:
        add("value", "💸 Best value", max(strong, key=lambda k: strong[k] / _blend(by_slug[k])))
    decent = {k: s for k, s in paid.items() if s >= 0.6 * top}
    if decent:
        add("budget", "🪙 Cheapest decent", min(decent, key=lambda k: _blend(by_slug[k])))
    return out


def _build_guide(by_slug: dict, intel: dict, coding: dict, agentic: dict) -> list:
    goals = [
        ("coding", "💻 Coding", "writing and fixing code", coding, False, False),
        ("agent", "🤖 Agent / tools", "calls tools reliably (Umbra tasks, shell, files)", agentic, True, False),
        ("smart", "🧠 Smartest", "hard questions, reasoning, long answers", intel, False, False),
        ("free", "🆓 Free", "best models that cost nothing (rate limited)", intel, False, True),
    ]
    return [{"key": k, "title": t, "sub": sub, "picks": _picks(by_slug, sc, tools, free)}
            for k, t, sub, sc, tools, free in goals]


def _build() -> dict:
    raw = requests.get("https://openrouter.ai/api/v1/models", timeout=30).json().get("data", [])
    by_slug = {}
    models = []
    for m in raw:
        p = m.get("pricing") or {}
        arch = m.get("architecture") or {}
        sp = m.get("supported_parameters") or []
        try:
            pin, pout = float(p.get("prompt") or 0) * 1e6, float(p.get("completion") or 0) * 1e6
        except (TypeError, ValueError):
            pin = pout = 0.0
        row = {
            "id": m["id"], "name": (m.get("name") or m["id"]).split(": ", 1)[-1], "vendor": _vendor(m),
            "description": (m.get("description") or "")[:1500],
            "in": round(pin, 4), "out": round(pout, 4), "ctx": m.get("context_length") or 0,
            "input": arch.get("input_modalities") or [], "output": arch.get("output_modalities") or [],
            "tools": "tools" in sp, "reasoning": "reasoning" in sp or "include_reasoning" in sp,
            "free": m["id"].endswith(":free") or (pin == 0 and pout == 0), "created": m.get("created") or 0,
            "image_price": float(p.get("image") or 0) if p.get("image") not in (None, "", "0") else 0,
            # ≈ cost of one generated 1024² picture (~1290 image tokens + a short prompt)
            "per_image": round(float(p.get("image_output") or 0) * 1290 + float(p.get("prompt") or 0) * 120
                               + float(p.get("completion") or 0) * 60, 4) if float(p.get("image_output") or 0) > 0 else 0,
        }
        models.append(row)
        by_slug[m["id"]] = row
        if m.get("canonical_slug"):
            by_slug[m["canonical_slug"]] = row

    def resolve(slug: Optional[str]) -> Optional[str]:
        if not slug:
            return None
        if slug in by_slug:
            return by_slug[slug]["id"]
        base = re.sub(r"-\d{8}$", "", slug)
        if base in by_slug:
            return by_slug[base]["id"]
        return None

    cards = []
    guide = []
    try:
        h = _rankings_blob()
        week = _query_data(h, '"queryKey":["rankings","models",{"view":"week"}]') or []
        tot, change = {}, {}
        for r in week:
            mid = resolve(r.get("model_permaslug"))
            if not mid:
                continue
            tot[mid] = tot.get(mid, 0) + (r.get("total_prompt_tokens") or 0) + (r.get("total_completion_tokens") or 0)
            if r.get("change") is not None:
                change[mid] = r["change"]
        top = sorted(tot.items(), key=lambda kv: -kv[1])[:10]
        cards.append({"key": "top_week", "title": "🔥 Most used this week", "sub": "tokens on OpenRouter",
                      "items": [{"id": k, "note": f"{v / 1e12:.1f}T tokens" + (f" · {'▲' if change.get(k, 0) >= 0 else '▼'}{abs(change.get(k, 0)) * 100:.0f}%" if k in change else "")}
                                for k, v in top]})
        bench = _query_data(h, '"queryKey":["rankings","benchmarks"]') or {}
        aa = (bench.get("aaData") or {})

        def scored(key):
            out = {}
            for x in aa.get(key) or []:
                mid = resolve(x.get("openrouter_slug") or x.get("heuristic_openrouter_slug") or x.get("permaslug"))
                if mid and x.get("score") is not None and mid not in out:
                    out[mid] = float(x["score"])
            return out
        intel, coding, agentic = scored("intelligence"), scored("coding"), scored("agentic")

        def card(key, title, sub, scores, n=10):
            items = sorted(scores.items(), key=lambda kv: -kv[1])[:n]
            return {"key": key, "title": title, "sub": sub, "items": [{"id": k, "note": f"score {v:g}"} for k, v in items]}
        cards.append(card("smartest", "🧠 Smartest", "Artificial Analysis intelligence index", intel))
        # value = intelligence per dollar (blend 3:1 input:output per 1M), only reasonably smart ones
        val = {}
        for k, s in intel.items():
            m = by_slug.get(k)
            if not m or m["free"] or s < 30:
                continue
            blend = (3 * m["in"] + m["out"]) / 4
            if blend > 0:
                val[k] = s / blend
        vitems = sorted(val.items(), key=lambda kv: -kv[1])[:10]
        cards.append({"key": "best_value", "title": "💸 Best value", "sub": "smartest per dollar",
                      "items": [{"id": k, "note": f"score {intel[k]:g} · ${by_slug[k]['in']:.2f}/${by_slug[k]['out']:.2f}"} for k, _ in vitems]})
        cards.append(card("coding", "💻 Best for coding", "coding index", coding))
        cards.append(card("agentic", "🤖 Best with tools", "agentic index (tool use)", agentic))
        free = {k: s for k, s in intel.items() if by_slug.get(k, {}).get("free")}
        if free:
            cards.append(card("free", "🆓 Best free", "free models by intelligence", free))
        for k, s in intel.items():
            if k in by_slug:
                by_slug[k]["score"] = s
        for k, s in coding.items():
            if k in by_slug:
                by_slug[k]["coding"] = s
        for k, s in agentic.items():
            if k in by_slug:
                by_slug[k]["agentic"] = s
        guide = _build_guide(by_slug, intel, coding, agentic)
    except Exception as e:
        logger.warning(f"rankings scrape failed: {e}")

    cats = {}
    for c in CATEGORIES:
        try:
            ids = [m["id"] for m in requests.get(f"https://openrouter.ai/api/v1/models?category={c}", timeout=20).json().get("data", [])]
            if ids:
                cats[c] = ids
        except Exception:
            pass
    return {"updated": time.time(), "cards": cards, "guide": guide, "categories": cats, "models": models}


def get(refresh: bool = False) -> dict:
    global _mem
    with _lock:
        if not _mem and os.path.exists(CACHE):
            try:
                with open(CACHE) as f:
                    _mem = json.load(f)
            except Exception:
                _mem = {}
        fresh = _mem and time.time() - _mem.get("updated", 0) < MAX_AGE
    if fresh and not refresh:
        return _mem
    data = _build()
    with _lock:
        _mem = data
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, CACHE)
    return data


@router.get("/explore")
def explore(refresh: bool = False, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    return get(refresh)


# ───────────────────────── full descriptions ─────────────────────────
# OpenRouter's API cuts long descriptions ("…strengths in long-horizon..."). The model's own page carries the
# whole text in its schema.org data, so "more" in the app asks for it once; cached on disk for good.

DESC_CACHE = os.path.join(_P.CACHE, "model_descriptions.json")
_desc_lock = threading.Lock()


def _desc_load() -> dict:
    try:
        return json.load(open(DESC_CACHE, encoding="utf-8"))
    except Exception:
        return {}


@router.get("/describe")
def describe(id: str, authorization: Optional[str] = Header(None)):
    """Full description of one OpenRouter model (id like openai/gpt-6-astra)."""
    require_auth(authorization)
    mid = id.strip().split(":")[0]
    with _desc_lock:
        cache = _desc_load()
        if mid in cache:
            return {"id": mid, "description": cache[mid]}
    full = ""
    try:
        html = requests.get(f"https://openrouter.ai/{mid}", headers=UA, timeout=20).text
        # the schema.org block: "url":"https://openrouter.ai/<id>","description":"…"
        m = re.search(r'"url":"https://openrouter\.ai/' + re.escape(mid) + r'","description":"((?:[^"\\]|\\.)*)"', html)
        if m:
            full = json.loads('"' + m.group(1) + '"')
    except Exception as e:
        logger.warning(f"describe {mid}: {e}")
    if full:
        with _desc_lock:
            cache = _desc_load(); cache[mid] = full
            with open(DESC_CACHE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(DESC_CACHE + ".tmp", DESC_CACHE)
    return {"id": mid, "description": full}
