"""
Model Lab — side-by-side tests of models: plain answer, web-search answer, voice
transcription. Results are kept (memory/model_tests.json), can be pinned,
re-run and edited from the app. Every call is billed in usage.db as category
"model_testing" (attached to a chat when the test was started inside one).
"""
from __future__ import annotations

import base64, json, os, threading, time, uuid
from typing import Optional

import requests
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from api.hub import require_auth

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from store import paths as _P
FILE = os.path.join(_P.LAB, "model_tests.json")
SAMPLE_VOICE = os.path.join(_P.LAB, "lab_sample_voice.ogg")
_lock = threading.Lock()
router = APIRouter(prefix="/api/v2/lab")

KINDS = ("plain", "search", "voice")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"


def _load() -> list[dict]:
    try:
        with open(FILE) as f:
            return json.load(f)
    except Exception:
        return []


def _save(tests: list[dict]) -> None:
    tmp = FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(tests, f, indent=1, ensure_ascii=False)
    os.replace(tmp, FILE)


def _price(prov: str, model: str, tin: int, tout: int) -> float:
    try:
        from store.usage import price_for
        p = price_for(f"{prov}/{model}")
        return tin * p.get("input", 0) / 1e6 + tout * p.get("output", 0) / 1e6
    except Exception:
        return 0.0


# ── one run of one model on one kind ─────────────────────────────────
def _run_one(prov: str, model: str, kind: str, question: str) -> dict:
    t0 = time.time()
    out = {"ok": False, "text": "", "ms": 0, "cost": 0.0, "sources": 0, "error": None}
    try:
        if kind == "voice":
            if not os.path.exists(SAMPLE_VOICE):
                raise RuntimeError("no sample voice note on the Pi")
            from agent.transcribe import _gemini, _openai
            if prov == "gemini":
                with open(SAMPLE_VOICE, "rb") as f:
                    out["text"] = _gemini(f.read(), "audio/ogg", model)
            else:
                out["text"] = _openai(SAMPLE_VOICE, None, model)
            out["ok"] = bool(out["text"])
        elif prov == "gemini":
            key = os.getenv("GEMINI_API_KEY", "")
            body = {"contents": [{"parts": [{"text": question}]}]}
            if kind == "search":
                body["tools"] = [{"google_search": {}}]
            r = requests.post(GEMINI_URL.format(model), json=body, headers={"x-goog-api-key": key}, timeout=90)
            try:
                from store import quota
                quota.record(model, r.status_code, r.text if r.status_code == 429 else "", what="lab")
            except Exception:
                pass
            if r.status_code != 200:
                raise RuntimeError(f"{r.status_code}: {r.text[:160]}")
            j = r.json(); um = j.get("usageMetadata") or {}
            c = (j.get("candidates") or [{}])[0]
            out["text"] = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []))
            out["sources"] = len((c.get("groundingMetadata") or {}).get("groundingChunks") or [])
            out["cost"] = _price("gemini", model, um.get("promptTokenCount", 0),
                                 um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0))
            out["ok"] = True
        elif prov == "openrouter":
            key = os.getenv("OPENROUTER_API_KEY", "")
            body = {"model": model, "messages": [{"role": "user", "content": question}], "usage": {"include": True}}
            if kind == "search" and not model.endswith(":online") and not model.startswith("perplexity/"):
                body["plugins"] = [{"id": "web", "engine": "exa", "max_results": 5}]
            r = requests.post("https://openrouter.ai/api/v1/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {key}"}, timeout=120)
            if r.status_code != 200:
                raise RuntimeError(f"{r.status_code}: {r.text[:160]}")
            j = r.json(); m = j["choices"][0]["message"]
            out["text"] = m.get("content") or ""
            out["sources"] = len(j.get("citations") or []) or len(m.get("annotations") or [])
            out["cost"] = float((j.get("usage") or {}).get("cost") or 0)
            out["ok"] = True
        else:  # openai direct (no built-in search here)
            if kind == "search":
                raise RuntimeError("OpenAI direct has no search in Umbra — use it through OpenRouter")
            from agent.providers import get_client
            c, mid = get_client(prov, model)
            r = c.chat.completions.create(model=mid, messages=[{"role": "user", "content": question}])
            out["text"] = r.choices[0].message.content or ""
            u = r.usage
            out["cost"] = _price(prov, mid, getattr(u, "prompt_tokens", 0), getattr(u, "completion_tokens", 0))
            out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:240]
    out["ms"] = int((time.time() - t0) * 1000)
    out["at"] = time.time()
    return out


def _bill(res: dict, prov: str, model: str, sid: Optional[str], test_name: str, kind: str):
    try:
        from store import usage
        usage.record_turn(sid=sid, provider=prov, model=model, input_tokens=0, output_tokens=0,
                          actual_cost=res.get("cost", 0.0), category="model_testing",
                          label=f"{test_name} · {kind}")
    except Exception:
        pass


# ── API ─────────────────────────────────────────────────────────────
class ModelIn(BaseModel):
    provider: str
    model: str


class TestIn(BaseModel):
    name: Optional[str] = None
    question: str = ""
    kinds: list[str] = ["plain", "search"]
    models: list[ModelIn] = []
    sid: Optional[str] = None
    pinned: bool = False


class RunIn(BaseModel):
    provider: Optional[str] = None   # only this model (retry one cell)
    model: Optional[str] = None
    kind: Optional[str] = None
    sid: Optional[str] = None


@router.get("")
def list_tests(authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    tests = _load()
    tests.sort(key=lambda t: (0 if t.get("pinned") else 1, -t.get("updated", 0)))
    return {"tests": tests, "has_sample_voice": os.path.exists(SAMPLE_VOICE)}


@router.post("")
def create_test(body: TestIn, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    t = {"id": "t-" + uuid.uuid4().hex[:8], "name": body.name or body.question[:50] or "Test",
         "question": body.question, "kinds": [k for k in body.kinds if k in KINDS] or ["plain"],
         "models": [{"provider": m.provider, "model": m.model, "results": {}} for m in body.models],
         "sid": body.sid, "pinned": body.pinned, "created": time.time(), "updated": time.time()}
    with _lock:
        tests = _load(); tests.append(t); _save(tests)
    return t


@router.patch("/{tid}")
def edit_test(tid: str, body: dict, authorization: Optional[str] = Header(None)):
    """name / question / kinds / pinned / models (list of {provider, model})."""
    require_auth(authorization)
    with _lock:
        tests = _load()
        t = next((x for x in tests if x["id"] == tid), None)
        if not t:
            raise HTTPException(404, "test not found")
        for k in ("name", "question", "pinned", "sid"):
            if k in body:
                t[k] = body[k]
        if "kinds" in body:
            t["kinds"] = [k for k in body["kinds"] if k in KINDS]
        if "models" in body:
            old = {(m["provider"], m["model"]): m for m in t["models"]}
            t["models"] = [old.get((m["provider"], m["model"]), {"provider": m["provider"], "model": m["model"], "results": {}})
                           for m in body["models"]]
        t["updated"] = time.time()
        _save(tests)
    return t


@router.delete("/{tid}")
def delete_test(tid: str, authorization: Optional[str] = Header(None)):
    require_auth(authorization)
    with _lock:
        _save([t for t in _load() if t["id"] != tid])
    return {"ok": True}


@router.post("/{tid}/run")
def run_test(tid: str, body: RunIn, authorization: Optional[str] = Header(None)):
    """Runs all cells (or one) in parallel; returns the updated test when done."""
    require_auth(authorization)
    t = next((x for x in _load() if x["id"] == tid), None)
    if not t:
        raise HTTPException(404, "test not found")
    sid = body.sid or t.get("sid")
    jobs = []
    for m in t["models"]:
        if body.provider and (m["provider"], m["model"]) != (body.provider, body.model):
            continue
        for k in t["kinds"]:
            if body.kind and k != body.kind:
                continue
            jobs.append((m["provider"], m["model"], k))
    results: dict = {}

    def work(p, mo, k):
        res = _run_one(p, mo, k, t["question"])
        _bill(res, p, mo, sid, t["name"], k)
        results[(p, mo, k)] = res

    threads = [threading.Thread(target=work, args=j) for j in jobs]
    for th in threads: th.start()
    for th in threads: th.join(timeout=150)
    with _lock:
        tests = _load()
        cur = next((x for x in tests if x["id"] == tid), None)
        if cur:
            for m in cur["models"]:
                for k in cur["kinds"]:
                    r = results.get((m["provider"], m["model"], k))
                    if r:
                        m.setdefault("results", {})[k] = r
            cur["updated"] = time.time()
            if sid and not cur.get("sid"):
                cur["sid"] = sid
            _save(tests)
    return cur


def seed_pinned():
    """The comparisons measured on 2026-10-07 while setting things up (pinned, not re-run)."""
    if any(t.get("seeded") for t in _load()):
        return
    day = time.mktime((2026, 10, 7, 11, 40, 0, 0, 0, -1))

    def r(ms, cost, text, src=0, ok=True, err=None):
        return {"ok": ok, "ms": ms, "cost": cost, "text": text, "sources": src, "error": err, "at": day}
    tests = _load()
    tests += [
        {"id": "t-seed-search", "seeded": True, "pinned": True, "created": day, "updated": day,
         "name": "Web search: Slayyyter album", "question": "When does Slayyyter's new album come out, and when was it announced? Answer in one sentence with dates.",
         "kinds": ["plain", "search"], "sid": None, "models": [
            {"provider": "openrouter", "model": "perplexity/sonar", "results": {"search": r(1800, 0.0051, "Crank 2 out Sept 10, 2026; album WOR$T MAN IN AMERICA on Dec 4, 2026", 3)}},
            {"provider": "openrouter", "model": "openai/gpt-4.1-mini", "results": {"search": r(3900, 0.0077, "Crank 2 released September 10, 2026 (Exa search)", 1)}},
            {"provider": "openrouter", "model": "google/gemini-3.5-flash:online", "results": {"search": r(5300, 0.0311, "Crank 2 released September 10, 2026 (Google native search)")}},
            {"provider": "openrouter", "model": "google/gemini-2.5-flash-lite", "results": {"search": r(4700, 0.0072, "Crank 2 released September 10, 2026 (Exa search)")}},
            {"provider": "gemini", "model": "gemini-3.5-flash-lite", "results": {
                "plain": r(1800, 0.0, "ok"), "search": r(2300, 0.0, "Album Dec 4, 2026, announced Oct 5, 2026", 1)}},
            {"provider": "gemini", "model": "gemini-3.5-flash", "results": {
                "plain": r(1500, 0.0, "ok"), "search": r(5800, 0.0, "Announced Oct 5, 2026; out Dec 4, 2026", 2)}},
            {"provider": "gemini", "model": "gemini-3-flash-preview", "results": {
                "plain": r(1200, 0.0, "", ok=False, err="503 busy"), "search": r(12500, 0.0, "Announced Oct 5, 2026; out Dec 4, 2026", 1)}},
         ]},
        {"id": "t-seed-voice", "seeded": True, "pinned": True, "created": day, "updated": day,
         "name": "Voice note → text (1 min, PT-BR)", "question": "(sample voice note)", "kinds": ["voice"], "sid": None, "models": [
            {"provider": "gemini", "model": "gemini-3.5-flash-lite", "results": {"voice": r(2300, 0.0, "Não, amigo, eu tinha comentado com você que eu não ia mais no show…")}},
            {"provider": "gemini", "model": "gemini-3-flash-preview", "results": {"voice": r(12400, 0.0, "Não, amigo, eu tinha comentado com você que eu não ia mais no show…")}},
            {"provider": "openai", "model": "gpt-4o-transcribe", "results": {"voice": r(5400, 0.0, "Não amigo, eu tinha comentado com você que eu não ia mais no show…")}},
         ]},
    ]
    _save(tests)
