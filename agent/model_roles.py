"""
Model roles — assign specific models to specific tasks.

The user can pin which model handles:
  chat            — default chat (mirrors agent/providers.set_model)
  image_gen       — image generation tool
  tts             — text-to-speech tool
  audio_transcribe — Whisper-equivalent for the audio button
  router_fallback — topic router's LLM fallback (memory/router.py)

Persisted to memory/model_roles.json. Endpoints read/write via api/server.py.
"""
import os, json, threading
from typing import Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from store import paths as _P
ROLES_FILE = os.path.join(_P.SETTINGS, "model_roles.json")

KNOWN_ROLES = {
    "chat":             "Default chat model (also what get_client() returns when no override)",
    "image_gen":        "Image generation tool (DALL-E / gpt-image-1 / etc.)",
    "tts":              "Text-to-speech tool",
    "audio_transcribe": "Audio-to-text (Whisper-equivalent)",
    "router_fallback":  "Topic router LLM fallback (when keyword scan misses)",
    "trait_extractor":  "Reads each message for lasting facts about you (traits)",
    "summary":          "Writes each chat's rolling summary and title",
    "web_search":       "Looks things up on the internet (OpenRouter model; an id ending in :online uses the provider's own search)",
    "transcribe_gemini": "Voice to text when Gemini is picked at the mic",
}

DEFAULTS = {
    "image_gen":        {"provider": "openai", "model": "gpt-image-1"},
    "tts":              {"provider": "openai", "model": "tts-1"},
    "audio_transcribe": {"provider": "openai", "model": "gpt-4o-transcribe"},
    "router_fallback":  {"provider": "openai", "model": "gpt-4.1-mini"},
    "trait_extractor":  {"provider": "openai", "model": "gpt-4.1-mini"},
    "summary":          {"provider": "openai", "model": "gpt-4.1-mini"},
    "web_search":       {"provider": "openrouter", "model": "perplexity/sonar"},
    "transcribe_gemini": {"provider": "gemini", "model": "gemini-3.5-flash-lite"},
}

_lock = threading.RLock()


def _load() -> dict:
    if not os.path.exists(ROLES_FILE):
        return {}
    try:
        with open(ROLES_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(ROLES_FILE), exist_ok=True)
    tmp = ROLES_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, ROLES_FILE)


def list_roles() -> dict:
    """Returns merged dict — saved overrides applied on top of DEFAULTS, plus
    the 'chat' role pulled live from providers.get_current()."""
    saved = _load()
    out = {}
    for role in KNOWN_ROLES:
        if role == "chat":
            try:
                from agent.providers import get_current
                cur = get_current() or {}
                out[role] = {"provider": cur.get("provider", ""),
                             "model":    cur.get("model", "")}
            except Exception:
                out[role] = {"provider": "", "model": ""}
            continue
        if role == "summary":
            out[role] = _summary_role()
            continue
        out[role] = saved.get(role) or DEFAULTS.get(role) or {"provider": "", "model": ""}
    return out


def _summary_role() -> dict:
    # the summary model lives in app_settings (summary_model) for older code
    try:
        from store.manager import get_app_setting
        sm = get_app_setting("summary_model") or {}
        if sm.get("provider") and sm.get("model"):
            return {"provider": sm["provider"], "model": sm["model"]}
    except Exception:
        pass
    return DEFAULTS["summary"]


def get_role(role: str) -> Optional[dict]:
    if role == "chat":
        try:
            from agent.providers import get_current
            return get_current()
        except Exception:
            return None
    if role == "summary":
        return _summary_role()
    rec = _load().get(role) or DEFAULTS.get(role)
    return rec


def set_role(role: str, provider: str, model: str) -> dict:
    if role == "chat":
        from agent.providers import set_model
        set_model(provider, model)
        return {"provider": provider, "model": model}
    if role == "summary":
        from store.manager import set_app_setting
        set_app_setting("summary_model", {"provider": provider, "model": model})
        return {"provider": provider, "model": model}
    with _lock:
        data = _load()
        data[role] = {"provider": provider, "model": model}
        _save(data)
    return data[role]
