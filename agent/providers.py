"""
Provider management — OpenAI, Gemini, OpenRouter, Ollama.
Handles model listing, switching, fallback on Ollama failure.
"""
import os, json, logging, requests, configparser
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG     = configparser.ConfigParser()
CONFIG.read(os.path.join(BASE_DIR, "config.ini"))
MEMORY_DIR = os.path.join(BASE_DIR, "memory")
LOGS_DIR   = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)

PROVIDER_CONFIG = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key":  os.getenv("OPENROUTER_API_KEY", ""),
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "api_key":  os.getenv("OPENAI_API_KEY", ""),
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key":  os.getenv("GEMINI_API_KEY", ""),
    },
}

from store import paths as _P
MODELS_FILE = os.path.join(_P.SETTINGS, "models_used.json")

# ─────────────────────────────────────────
#  Persistence
# ─────────────────────────────────────────
def _load_used() -> dict:
    if os.path.exists(MODELS_FILE):
        with open(MODELS_FILE) as f:
            return json.load(f)
    return {"current": None, "history": []}

def _save_used(data: dict):
    with open(MODELS_FILE, "w") as f:
        json.dump(data, f, indent=2)

def get_current() -> dict | None:
    return _load_used().get("current")

def set_model(provider: str, model_id: str):
    used = _load_used()
    entry = {"provider": provider.lower(), "model": model_id}
    history = used.get("history", [])
    if entry not in history:
        history.insert(0, entry)
        used["history"] = history[:20]
    used["current"] = entry
    _save_used(used)
    logger.info(f"Model set: [{provider}] {model_id}")

def get_history() -> list:
    return _load_used().get("history", [])


# ─────────────────────────────────────────
#  Small background calls (router, trait extractor, summary)
# ─────────────────────────────────────────
QUICK_FALLBACK = ("openai", "gpt-4.1-mini")


def fast_kwargs(provider: str, model: str) -> dict:
    """Turn thinking off for quick helper calls: Gemini 2.5 Flash thinks by
    default, which made a 1-line JSON answer take 2-9 s and sometimes spent
    the whole token budget so the reply came back cut off."""
    m = (model or "").lower().split("/")[-1]
    if provider == "gemini" and "flash" in m and m.startswith("gemini-2.5"):
        return {"reasoning_effort": "none"}
    if m.startswith("gpt-5") or m.startswith(("o1", "o3", "o4")):
        return {"reasoning_effort": "minimal"}
    return {}


def quick_completion(provider: str, model: str, label: str = "", sid: str | None = None, **kwargs):
    """chat.completions.create for helper calls, with thinking off and one
    retry on OpenAI when the chosen provider is out of quota (the Gemini key
    is free tier: 20 requests/day, gone after ~5 chat turns).
    Returns (response, "provider/model" actually used)."""
    from store import usage as _u
    client, mid = get_client(provider, model)
    try:
        if provider == "openrouter":
            kwargs.setdefault("extra_body", {"usage": {"include": True}})
        r = client.chat.completions.create(model=mid, **fast_kwargs(provider, mid), **kwargs)
        _u.record_call(provider, mid, getattr(r, "usage", None), sid=sid, label=label)
        return r, f"{provider}/{mid}"
    except Exception as e:
        err = str(e)
        quota = ("429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower())
        if not quota or (provider, model) == QUICK_FALLBACK:
            raise
        logger.warning(f"{provider}/{model} out of quota, retrying on {QUICK_FALLBACK[0]}/{QUICK_FALLBACK[1]}")
        fp, fm = QUICK_FALLBACK
        client, mid = get_client(fp, fm)
        r = client.chat.completions.create(model=mid, **kwargs)
        _u.record_call(fp, mid, getattr(r, "usage", None), sid=sid, label=label)
        return r, f"{fp}/{mid}"

# ─────────────────────────────────────────
#  Client factory
# ─────────────────────────────────────────
def get_client(provider: str = None, model: str = None) -> tuple[OpenAI, str]:
    """Returns (OpenAI-compatible client, model_id)."""
    if provider is None or model is None:
        current = get_current()
        if current:
            provider = current["provider"]
            model    = current["model"]
        else:
            provider = CONFIG.get("general", "default_provider", fallback="openrouter")
            model    = CONFIG.get("general", "default_model",    fallback="anthropic/claude-3.5-sonnet")

    if provider == "ollama":
        host = _resolve_ollama_host()
        if not host:
            raise ConnectionError("Ollama host not reachable")
        client = OpenAI(base_url=f"{host}/v1", api_key="ollama")
        return client, model

    cfg = PROVIDER_CONFIG.get(provider)
    if not cfg:
        raise ValueError(f"Unknown provider: {provider}")

    if provider == "gemini":
        return OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"], http_client=_gemini_http()), model
    client = OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"])
    return client, model


_GEMINI_HTTP = None


def _gemini_http():
    """httpx client whose response hook feeds memory/quota.py (per-model daily counts)."""
    global _GEMINI_HTTP
    if _GEMINI_HTTP is None:
        import httpx

        def _hook(resp):
            try:
                body = json.loads(resp.request.content or b"{}")
                model = body.get("model", "?")
            except Exception:
                model = "?"
            text = ""
            if resp.status_code == 429:
                try:
                    resp.read(); text = resp.text
                except Exception:
                    pass
            try:
                from store import quota
                quota.record(model, resp.status_code, text, what="chat")
            except Exception:
                pass
        _GEMINI_HTTP = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0), event_hooks={"response": [_hook]})
    return _GEMINI_HTTP

# ─────────────────────────────────────────
#  Model listing
# ─────────────────────────────────────────
def fetch_models() -> dict:
    result = {}

    # OpenRouter
    key = os.getenv("OPENROUTER_API_KEY", "")
    if key:
        try:
            r = requests.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10
            )
            result["openrouter"] = [m["id"] for m in r.json().get("data", [])]
        except Exception as e:
            result["openrouter"] = [f"ERROR: {e}"]

    # OpenAI
    key = os.getenv("OPENAI_API_KEY", "")
    if key:
        try:
            r = requests.get(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10
            )
            result["openai"] = sorted([
                m["id"] for m in r.json().get("data", [])
                if "gpt" in m["id"]
            ])
        except Exception as e:
            result["openai"] = [f"ERROR: {e}"]

    # Gemini
    key = os.getenv("GEMINI_API_KEY", "")
    if key:
        try:
            r = requests.get(
                f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
                timeout=10
            )
            result["gemini"] = [
                m["name"].replace("models/", "")
                for m in r.json().get("models", [])
            ]
        except Exception as e:
            result["gemini"] = [f"ERROR: {e}"]

    # Ollama
    host = _resolve_ollama_host()
    if host:
        try:
            r = requests.get(f"{host}/api/tags", timeout=5)
            result["ollama"] = [m["name"] for m in r.json().get("models", [])]
        except Exception as e:
            result["ollama"] = [f"ERROR: {e}"]
    else:
        result["ollama"] = ["NOT REACHABLE"]

    return result

# ─────────────────────────────────────────
#  Ollama discovery
# ─────────────────────────────────────────
def _resolve_ollama_host() -> str | None:
    # 1. explicit env
    host = os.getenv("OLLAMA_HOST", "").strip()
    if host:
        try:
            r = requests.get(f"{host}/api/tags", timeout=3)
            if r.ok:
                return host
        except Exception:
            pass

    # 2. auto-scan common subnet
    import subprocess, re
    try:
        result = subprocess.run(
            ["ip", "route"], capture_output=True, text=True, timeout=5
        )
        # grab first 192.168.x or 10.x subnet
        match = re.search(r'(\d+\.\d+\.\d+)\.\d+/\d+', result.stdout)
        if match:
            subnet = match.group(1)
            for i in range(1, 255):
                candidate = f"http://{subnet}.{i}:11434"
                try:
                    r = requests.get(f"{candidate}/api/tags", timeout=0.5)
                    if r.ok:
                        logger.info(f"Ollama found at {candidate}")
                        return candidate
                except Exception:
                    continue
    except Exception:
        pass

    return None

def log_ollama_failure(model: str):
    path = os.path.join(LOGS_DIR, "ollama_failures.log")
    from datetime import datetime
    with open(path, "a") as f:
        f.write(f"[{datetime.now().isoformat()}] Failed to connect for model: {model}\n")
