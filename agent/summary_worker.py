"""
Background rolling-summary updater.

Runs after each turn (off the main response path) so the user-visible reply
is never blocked on summary generation. Uses a separately-configured cloud
model (see memory.manager.get_app_setting('summary_model')) — Ollama is
explicitly refused to keep the Pi cool.
"""
from __future__ import annotations

import logging, threading, traceback
from typing import Callable, Optional

from agent.providers import get_client
from store.manager import get_summary, update_summary, get_app_setting, get_session_log

logger = logging.getLogger(__name__)

_PROMPT = """\
You maintain the rolling summary of a chat between Maicon/Erick and his
personal AI agent "Umbra".

Output rules — FOLLOW EXACTLY:
- Output ONE LINE only.
- A short noun-phrase / topic line, max 60 characters, NO punctuation at end.
- Examples of good output: "casual greeting", "xUmbra Android build",
  "disk-space cleanup discussion", "anime recs — dark psychological",
  "user identity Q&A + supplements".
- Bad output: "I summarized…", "Updated to:", any narrative or sentence.
- The summary describes the FULL scope of the chat so far, NOT just the last
  turn. Never NARROW the topic when new turns expand its scope —
  combine with " + " or " — " as needed.
- If nothing has changed, repeat the current summary verbatim.

Current summary:
{current_summary}

Recent conversation log (newest at the bottom):
{conversation}

New summary (one line, max 60 chars):"""


def _build_messages(current: str,
                    conversation: str) -> list[dict]:
    return [{
        "role": "user",
        "content": _PROMPT.format(
            current_summary=current.strip() or "(none yet)",
            conversation=conversation.strip()[:8000],
        ),
    }]


def _recent_conversation(sid: str,
                         user_msg: str,
                         assistant_msg: str) -> str:
    """Pull a window of recent turns from the session log so the summary
    reflects the whole chat scope, not just the last exchange."""
    try:
        log = get_session_log(sid) or ""
    except Exception:
        log = ""
    # Take roughly the last ~40 lines; if the log is missing, fall back to
    # just the current turn.
    if log:
        lines = log.splitlines()[-40:]
        body = "\n".join(lines)
    else:
        body = ""
    if user_msg or assistant_msg:
        body += f"\n[USER] {(user_msg or '').strip()}"
        body += f"\n[UMBRA] {(assistant_msg or '').strip()}"
    return body


def _run(sid: str,
         user_message: str,
         assistant_message: str,
         emit: Optional[Callable] = None) -> None:
    """Synchronous worker body. Always meant to run inside a daemon thread."""
    try:
        cfg = get_app_setting("summary_model") or {}
        provider = (cfg.get("provider") or "").lower()
        model    = cfg.get("model") or ""
        if not provider or not model:
            logger.info("summary worker: no summary_model configured; skipping")
            return
        if provider == "ollama":
            logger.info("summary worker: ollama refused (keep the Pi cool); skipping")
            return

        model_id = model
        current = get_summary(sid) or ""
        conversation = _recent_conversation(sid, user_message, assistant_message)
        messages = _build_messages(current, conversation)
        if emit:
            try:
                emit("mem.summary.refresh:start",
                     sid=sid, provider=provider, model=model_id)
            except Exception:
                pass

        from agent.providers import quick_completion
        resp, used = quick_completion(
            provider, model, label="title & summary", sid=sid,
            messages=messages,
            temperature=0.2,
            max_tokens=400,
        )
        new_summary = (resp.choices[0].message.content or "").strip()
        # Some models wrap output in quotes or markdown — normalize.
        for ch in ("`", "*", "_"):
            new_summary = new_summary.replace(ch, "")
        if (new_summary.startswith('"') and new_summary.endswith('"')) or \
           (new_summary.startswith("'") and new_summary.endswith("'")):
            new_summary = new_summary[1:-1].strip()
        # Some models still emit a multi-line preamble — keep just the last
        # non-empty line (which is usually the actual summary).
        non_empty = [ln.strip() for ln in new_summary.splitlines() if ln.strip()]
        if non_empty:
            new_summary = non_empty[-1]
        # Hard cap so a bad day doesn't bloat the summary file.
        if len(new_summary) > 200:
            new_summary = new_summary[:200].rstrip()
        if not new_summary:
            logger.info(f"summary worker: empty response for {sid}; skipping write")
            return
        if new_summary == current.strip():
            _maybe_retitle(sid, conversation, provider, model)
            if emit:
                try:
                    emit("mem.summary.refresh:noop",
                         sid=sid, summary=new_summary)
                except Exception:
                    pass
            return

        # the chat may have been deleted while the model was thinking
        from store.manager import get_session
        if not get_session(sid):
            logger.info(f"summary worker: {sid} no longer exists; dropping summary")
            return
        update_summary(sid, new_summary, source="summary_worker")
        _maybe_retitle(sid, conversation, provider, model)
        if emit:
            try:
                emit("mem.summary.refresh:done",
                     sid=sid, summary=new_summary,
                     provider=provider, model=model_id)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"summary worker failed for {sid}: {e}\n{traceback.format_exc()}")
        if emit:
            try:
                emit("mem.summary.refresh:error", sid=sid, error=str(e))
            except Exception:
                pass


_TITLE_PROMPT = """\
Give this chat a short title.

Rules:
- 2 to 5 words, at most 32 characters, no quotes, no ending punctuation.
- Same language as the conversation.
- Name the MAIN subject only (not greetings, not every side topic, no " + " lists).
- If the current title still fits the main subject, output it EXACTLY unchanged.

Current title: {title}

Conversation (newest at the bottom):
{conversation}

Title:"""

_GENERIC = {"", "new chat", "(no summary)", "task in progress", "nova conversa"}


def _maybe_retitle(sid: str, conversation: str, provider: str, model: str, force: bool = False) -> None:
    """Titles are short and stable: set after replies 1 and 3, then re-checked
    every 6 replies and only changed when the main subject changed. A title the
    user typed is never touched (title_locked)."""
    try:
        from store.manager import get_chat_meta, set_chat_meta, get_session, update_session
        from store import messages as msg_store
        from agent.providers import quick_completion
        meta = get_chat_meta(sid)
        if meta.get("title_locked"):
            return
        n = sum(1 for m in msg_store.list_messages(sid) if m.get("role") == "assistant")
        last = int(meta.get("title_at") or 0)
        title = (get_session(sid) or {}).get("summary") or ""
        generic = title.strip().lower() in _GENERIC
        due = force or generic or (n in (1, 3) and n != last) or (n >= 6 and n - last >= 6)
        if not due:
            return
        resp, _ = quick_completion(
            provider, model, label="chat title", sid=sid,
            messages=[{"role": "user", "content": _TITLE_PROMPT.format(
                title=title or "(none yet)", conversation=conversation.strip()[-5000:])}],
            temperature=0.2, max_tokens=40)
        new = (resp.choices[0].message.content or "").strip().splitlines()[-1:] or [""]
        new = new[0].strip().strip('"\'*_`').rstrip(".!?:;")
        if len(new) > 40:
            new = new[:40].rsplit(" ", 1)[0]
        set_chat_meta(sid, title_at=n)
        if new and new != title and get_session(sid):
            update_session(sid, summary=new)
    except Exception as e:
        logger.warning(f"retitle failed for {sid}: {e}")


def schedule(sid: str,
             user_message: str,
             assistant_message: str,
             emit: Optional[Callable] = None) -> None:
    """Fire-and-forget the worker on a daemon thread."""
    if not sid:
        return
    from store import workers as W
    W.register("summary", "Chat summarizer", "After each reply, refreshes the chat's short summary (and early on its "
               "title) so long chats aren't forgotten.", every=None, emoji="📜")

    def _go():
        with W.run("summary") as r:
            _run(sid, user_message, assistant_message, emit)
            r.note = f"chat {sid}"
    t = threading.Thread(
        target=_go,
        args=(),
        name=f"summary-worker-{sid}",
        daemon=True,
    )
    t.start()
