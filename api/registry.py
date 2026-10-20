"""
AgentRegistry — caches one UmbraAgent per session_id and serializes chat() calls.
"""
import json, os, threading, logging
from agent.core import UmbraAgent

logger = logging.getLogger(__name__)

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR  = os.path.join(BASE_DIR, "memory")
from store import paths as _P
CHAT_MODELS = os.path.join(_P.CHATS, "chat_models.json")


def _load_chat_models() -> dict:
    if not os.path.exists(CHAT_MODELS):
        return {}
    try:
        with open(CHAT_MODELS) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_chat_models(data: dict):
    with open(CHAT_MODELS, "w") as f:
        json.dump(data, f, indent=2)


def get_chat_model(sid: str) -> dict | None:
    return _load_chat_models().get(sid)


def set_chat_model(sid: str, provider: str, model: str):
    data = _load_chat_models()
    data[sid] = {"provider": provider, "model": model}
    _save_chat_models(data)


def clear_chat_model(sid: str):
    data = _load_chat_models()
    data.pop(sid, None)
    _save_chat_models(data)


class AgentRegistry:
    """Holds one UmbraAgent per session. Each chat() call is serialized per-agent."""

    def __init__(self):
        self._agents: dict[str, UmbraAgent] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()

    def get(self, sid: str | None) -> UmbraAgent:
        """Returns (creating if needed) an agent for the given sid. Sid can be None for ad-hoc."""
        key = sid or "__adhoc__"
        with self._global_lock:
            if key not in self._agents:
                agent = UmbraAgent()
                agent.bind_session(sid)
                override = get_chat_model(sid) if sid else None
                if override:
                    agent.set_model_override(override["provider"], override["model"])
                self._agents[key] = agent
                self._locks[key] = threading.Lock()
            return self._agents[key]

    def lock_for(self, sid: str | None) -> threading.Lock:
        key = sid or "__adhoc__"
        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def drop(self, sid: str):
        key = sid or "__adhoc__"
        with self._global_lock:
            self._agents.pop(key, None)
            self._locks.pop(key, None)

    def adopt_sid(self, old_sid: str | None, new_sid: str):
        """If the agent auto-created a new session_id mid-chat, rekey the cache."""
        with self._global_lock:
            old_key = old_sid or "__adhoc__"
            if old_key in self._agents and new_sid not in self._agents:
                self._agents[new_sid] = self._agents.pop(old_key)
                self._locks[new_sid]  = self._locks.pop(old_key, threading.Lock())


REGISTRY = AgentRegistry()
