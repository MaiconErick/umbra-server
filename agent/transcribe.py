"""
Voice → text for the app's mic button. The phone picks the engine per recording.

  gemini  better with PT-BR / mixed speech (the user's experience with xWapp);
          the free key has a small daily quota PER MODEL, so on a 429 it tries
          the next model, then OpenAI.
  openai  gpt-4o-transcribe (falls back to whisper-1).
"""
from __future__ import annotations

import base64, logging, os

import requests

logger = logging.getLogger(__name__)

# 1-min voice note, paid key (2026-10-07): 3.5-flash-lite 2.3 s, 3-flash-preview 12 s; 2.5-flash unavailable to new projects
GEMINI_CHAIN = ["gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-flash-lite-latest"]
PROMPT = ("Transcribe this voice message verbatim in its original language. "
          "Output only the transcript, nothing else.")


def _role(name: str, default: str) -> str:
    try:
        from agent.model_roles import get_role
        r = get_role(name) or {}
        return r.get("model") or default
    except Exception:
        return default


def _gemini(data: bytes, mime: str, model: str) -> str:
    key = os.getenv("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("no Gemini key")
    body = {"contents": [{"parts": [
        {"text": PROMPT},
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}},
    ]}]}
    if model.startswith("gemini-2.5-flash"):
        body["generationConfig"] = {"thinkingConfig": {"thinkingBudget": 0}}   # no thinking for plain transcription
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      json=body, headers={"x-goog-api-key": key}, timeout=30)
    try:
        from store import quota
        quota.record(model, r.status_code, r.text if r.status_code == 429 else "", what="voice")
    except Exception:
        pass
    if r.status_code != 200:
        raise RuntimeError(f"{r.status_code}: {r.text[:160]}")
    cand = (r.json().get("candidates") or [{}])[0]
    return "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()


def _openai(path: str, language: str | None, model: str) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    for m in (model, "whisper-1"):
        try:
            with open(path, "rb") as f:
                kw = {"model": m, "file": f}
                if language:
                    kw["language"] = language
                return (getattr(client.audio.transcriptions.create(**kw), "text", "") or "").strip()
        except Exception as e:
            if m == "whisper-1":
                raise
            logger.warning(f"openai transcribe {m} failed, trying whisper-1: {e}")
    return ""


def transcribe(path: str, mime: str, provider: str = "openai", language: str | None = None) -> dict:
    tried = []
    if provider == "gemini":
        first = _role("transcribe_gemini", GEMINI_CHAIN[0])
        with open(path, "rb") as f:
            data = f.read()
        for m in [first] + [x for x in GEMINI_CHAIN if x != first]:
            try:
                text = _gemini(data, mime, m)
                if text:
                    return {"text": text, "provider": "gemini", "model": m, "tried": tried}
                tried.append(f"gemini/{m}: empty")
            except Exception as e:
                tried.append(f"gemini/{m}: {str(e)[:80]}")
                if not str(e).startswith(("429", "404", "500", "503")) and "timed out" not in str(e).lower():
                    break
    m = _role("audio_transcribe", "gpt-4o-transcribe")
    text = _openai(path, language, m)
    return {"text": text, "provider": "openai", "model": m, "tried": tried,
            "fallback": provider == "gemini"}
