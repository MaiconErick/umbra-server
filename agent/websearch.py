"""
web_search tool — answers from the live web, with sources.

Engines (model role "web_search", falls through on errors/quota):
  gemini      Gemini with Google Search grounding (native API; best for
              "what's new" questions — same Google results the Gemini app uses)
  openrouter  any OpenRouter model with ":online" (provider-native search when
              the model has one, otherwise Exa)
"""
from __future__ import annotations

import json, logging, os, time

import requests

logger = logging.getLogger(__name__)

# Measured 2026-10-07: openrouter gpt-4.1-mini + Exa = 3.5 s, $0.007/search, correct.
# Gemini grounding needs a paid Google key (free tier: 429). A model id ending in
# ":online" (e.g. google/gemini-3.5-flash:online) uses the provider's own search (~$0.03).
# 2nd round: perplexity/sonar 1.7-1.9 s, $0.005, cited — 3x faster than gpt-4.1-mini+Exa.
DEFAULT = {"provider": "openrouter", "model": "perplexity/sonar"}
FALLBACKS = [("openrouter", "perplexity/sonar"), ("openrouter", "openai/gpt-4.1-mini"), ("gemini", "gemini-3.5-flash")]
_last: dict = {}


def _role() -> dict:
    try:
        from agent.model_roles import get_role
        r = get_role("web_search") or {}
        if r.get("provider") and r.get("model"):
            return r
    except Exception:
        pass
    return DEFAULT


def _gemini(query: str, model: str) -> tuple[str, list[dict]]:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("no GEMINI_API_KEY")
    body = {
        "contents": [{"parts": [{"text": (
            f"{query}\n\nAnswer from current web results. Be factual and specific "
            f"(dates, numbers, names). Today is {time.strftime('%Y-%m-%d')}.")}]}],
        "tools": [{"google_search": {}}],
    }
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      json=body, headers={"x-goog-api-key": key}, timeout=60)
    try:
        from store import quota
        quota.record(model, r.status_code, r.text if r.status_code == 429 else "", what="search")
    except Exception:
        pass
    if r.status_code != 200:
        raise RuntimeError(f"gemini {r.status_code}: {r.text[:200]}")
    j = r.json()
    um = j.get("usageMetadata") or {}
    _last["in_tok"], _last["out_tok"] = um.get("promptTokenCount", 0), um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0)
    cand = (j.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    meta = cand.get("groundingMetadata") or {}
    sources = [{"title": (c.get("web") or {}).get("title", ""), "url": (c.get("web") or {}).get("uri", "")}
               for c in meta.get("groundingChunks") or [] if c.get("web")]
    return text.strip(), sources


def _domain(u: str) -> str:
    try:
        return u.split("/")[2].removeprefix("www.")
    except Exception:
        return u


def _openrouter(query: str, model: str, progress=None) -> tuple[str, list[dict]]:
    """Streams the engine's answer so the app can show sites and text as they arrive."""
    key = os.getenv("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("no OPENROUTER_API_KEY")
    body = {"model": model, "usage": {"include": True}, "stream": True, "messages": [{"role": "user", "content":
            f"{query}\n\nAnswer from current web results with dates and specifics, concisely "
            f"(the key facts or a short list, under ~180 words). Today is {time.strftime('%Y-%m-%d')}."}]}
    if not model.endswith(":online") and not model.startswith("perplexity/"):
        # Exa search: fast (~3 s) and cheap; native search only when asked for with :online
        body["plugins"] = [{"id": "web", "engine": "exa", "max_results": 6}]
    r = requests.post("https://openrouter.ai/api/v1/chat/completions", timeout=90, stream=True,
                      headers={"Authorization": f"Bearer {key}"}, json=body)
    if r.status_code != 200:
        raise RuntimeError(f"openrouter {r.status_code}: {r.text[:200]}")
    text, sources, seen = "", [], set()
    last_push = 0.0

    def add(url, title=""):
        if url and url not in seen:
            seen.add(url)
            sources.append({"title": title or "", "url": url})

    for line in r.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        data = line[6:]
        if data == b"[DONE]":
            break
        try:
            j = json.loads(data)
        except Exception:
            continue
        for u in j.get("citations") or []:
            if isinstance(u, str):
                add(u)
        for ch in j.get("choices") or []:
            d = ch.get("delta") or ch.get("message") or {}
            text += d.get("content") or ""
            for a in d.get("annotations") or []:
                uc = a.get("url_citation") or {}
                add(uc.get("url"), uc.get("title", ""))
        if j.get("usage"):
            _last["cost"] = j["usage"].get("cost")
        if progress and time.time() - last_push > 0.35:
            last_push = time.time()
            progress({"stage": "reading" if text else "searching",
                      "sites": [_domain(s["url"]) for s in sources][:10],
                      "preview": text[-220:]})
    if progress:
        progress({"stage": "done", "sites": [_domain(s["url"]) for s in sources][:10], "preview": text[-220:]})
    return text.strip(), sources


def search(query: str, sid: str | None = None, engine: dict | None = None, reddit: bool = False,
           progress=None) -> str:
    query = (query or "").strip()
    if not query:
        return "Missing 'query'"
    label = query
    if reddit:
        query += '\n\nAlso search Reddit (reddit.com threads) for this and include what people there recommend or say, citing the threads.'
    role = engine if (engine and engine.get("provider") and engine.get("model")) else _role()
    order = [(role["provider"], role["model"])] + [f for f in FALLBACKS if f != (role["provider"], role["model"])]
    errors = []
    for prov, model in order:
        try:
            t0 = time.time()
            if progress:
                try:
                    from store.prices import names as _names
                    nm = _names().get(model if prov == "openrouter" else f"google/{model}", "") or model
                except Exception:
                    nm = model
                progress({"stage": "asking", "engine": nm.replace(": ", " "), "reddit": reddit, "sites": [], "preview": ""})
            if prov == "gemini":
                text, sources = _gemini(query, model)
                if progress:
                    progress({"stage": "done", "sites": [_domain(s["url"]) for s in sources][:10], "preview": text[-220:]})
            else:
                text, sources = _openrouter(query, model, progress)
            if not text:
                raise RuntimeError("empty answer")
            _last.update(engine=f"{prov}/{model}", ms=int((time.time() - t0) * 1000), sources=sources)
            try:
                from store import usage as _u
                _u.record_call(prov, model, sid=sid, category="search", label=("[reddit] " if reddit else "") + label[:80],
                               actual_cost=_last.get("cost") if prov == "openrouter" else None,
                               input_tokens=_last.get("in_tok", 0), output_tokens=_last.get("out_tok", 0))
            except Exception:
                pass
            seen, lines = set(), []
            for s in sources:
                if s["url"] in seen:
                    continue
                seen.add(s["url"])
                lines.append(f"- {s['title'] or s['url']}: {s['url']}")
            src = ("\n\nSources:\n" + "\n".join(lines[:8])) if lines else ""
            return f"[web search via {prov}/{model}]\n{text}{src}"
        except Exception as e:
            logger.warning(f"web_search {prov}/{model} failed: {e}")
            errors.append(f"{prov}/{model}: {str(e)[:120]}")
    return "Web search failed: " + " | ".join(errors)
