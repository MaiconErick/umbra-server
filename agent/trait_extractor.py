"""Background trait extractor — Gemini Flash pass over each user turn to
detect stable personal preferences and apply them to memory/traits/*.

Called synchronously (in-thread) BEFORE the main LLM call so any
supersede decisions can inject a contradiction warning into the same
turn's system prompt.

Design in one line:
    user_turn + loaded_traits  →  Flash JSON  →  add/reinforce/nuance/supersede/noop

Emits detailed debug events via the passed emit_fn so xUmbra can show
what the extractor decided and why.
"""
from __future__ import annotations

import json, os, re, time, logging
from typing import Callable

from store import manager as M

logger = logging.getLogger(__name__)

DEFAULT_PROV     = "openai"
DEFAULT_MODEL    = "gpt-4.1-mini"
TIMEOUT_SEC      = 8.0
MAX_ACTIONS_PER_TURN = 6

_PROMPT_TEMPLATE = """You are a memory extractor for a personal AI agent.

Your job: given the user's most recent turn AND their existing stable traits,
decide what memory operations should happen. Focus on STABLE personal traits,
preferences, opinions — NOT transient state (what they did today, what boss
they beat, current mood spikes).

For each trait you detect, choose ONE action:
- add        : brand new trait, no conflict with existing
- reinforce  : same trait already recorded — just note reinforcement
- nuance     : related to existing trait, adds detail (merge with old)
- supersede  : contradicts an existing trait — user changed their mind
- noop       : not a trait, ignore

Return a JSON array. Each element:
{{
  "action":   "add|reinforce|nuance|supersede",
  "category": "<one of the available categories>",
  "section":  "SHOUTY-KEBAB (e.g. GENRES, PLAYSTYLE, DISLIKES, ROUTINE)",
  "text":     "one short phrase — the trait itself",
  "old":      "the existing text being replaced (ONLY when action is supersede or nuance)",
  "reason":   "one short sentence explaining your choice"
}}

Rules:
- Max {max_actions} actions per turn.
- If nothing extractable, return [].
- Prefer noop over creating weak/uncertain traits.
- Section names must be SHOUTY-KEBAB-CASE (e.g. GENRES, PLAYSTYLE).
- Output ONLY the JSON array. No prose, no code fences.

AVAILABLE CATEGORIES: {all_categories}

LOADED TRAIT FILES (for reinforce/supersede/nuance checks):
{loaded_traits}

USER TURN:
{user_turn}

JSON:"""


def _fallback_model() -> tuple[str, str]:
    """Respect model-role config if set, else default to Gemini Flash."""
    try:
        from agent.model_roles import get_role
        rec = get_role("trait_extractor") or {}
        if rec.get("provider") and rec.get("model"):
            return rec["provider"], rec["model"]
    except Exception:
        pass
    return DEFAULT_PROV, DEFAULT_MODEL


def extract_and_apply(user_turn: str,
                      loaded_categories: list[str],
                      sid: str | None,
                      emit_fn: Callable[..., None]) -> dict:
    """Run the extractor and apply decisions. Returns:
        {
          "applied":         [action_dicts],
          "contradictions":  [supersede_dicts],  # for warning injection
          "latency_ms":      int,
          "input_tokens":    int,
          "output_tokens":   int,
          "model":           "prov/model",
          "path":            "extracted" | "skipped" | "error",
          "error":           str | None,
        }
    """
    result = {
        "applied":        [],
        "contradictions": [],
        "latency_ms":     0,
        "input_tokens":   0,
        "output_tokens":  0,
        "model":          "",
        "path":           "skipped",
        "error":          None,
    }

    user_turn = (user_turn or "").strip()
    if not user_turn or len(user_turn) < 8:
        result["path"] = "skipped"
        result["error"] = "user turn too short"
        emit_fn("trait_extractor:skip", reason=result["error"],
                user_turn_preview=user_turn[:80])
        return result

    if not os.getenv("GEMINI_API_KEY"):
        result["path"] = "skipped"
        result["error"] = "GEMINI_API_KEY not set"
        emit_fn("trait_extractor:skip", reason=result["error"])
        return result

    # Gather context: loaded trait bodies + all category names
    all_cats = M.list_trait_categories()
    loaded_bodies: dict[str, str] = {}
    for cat in loaded_categories:
        try:
            body = M.load_trait(cat) if cat in all_cats else ""
        except Exception:
            body = ""
        if body:
            loaded_bodies[cat] = body

    loaded_desc = "\n\n".join(
        f"[{cat}]\n{body}" for cat, body in loaded_bodies.items()
    ) if loaded_bodies else "(no trait files loaded this turn)"

    prompt = _PROMPT_TEMPLATE.format(
        max_actions=MAX_ACTIONS_PER_TURN,
        all_categories=", ".join(all_cats) or "(none)",
        loaded_traits=loaded_desc[:6000],
        user_turn=user_turn[:2000],
    )

    prov, model_name = _fallback_model()
    result["model"] = f"{prov}/{model_name}"

    emit_fn("trait_extractor:start",
            model=result["model"],
            loaded_categories=loaded_categories,
            all_categories=all_cats,
            user_turn_preview=user_turn[:120],
            reason=f"analyzing user turn for trait signals using {result['model']}")

    t0 = time.time()
    try:
        from agent.providers import quick_completion
        resp, result["model"] = quick_completion(
            prov, model_name, label="trait extractor", sid=sid,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=800,
            timeout=TIMEOUT_SEC,
        )
        result["latency_ms"] = int((time.time() - t0) * 1000)
        content = (resp.choices[0].message.content or "").strip()
        usage = getattr(resp, "usage", None)
        if usage:
            result["input_tokens"]  = getattr(usage, "prompt_tokens", 0)
            result["output_tokens"] = getattr(usage, "completion_tokens", 0)

        # Extract JSON array (may be wrapped in prose or code fence)
        m = re.search(r'\[[\s\S]*\]', content)
        if not m:
            result["path"] = "error"
            result["error"] = "no JSON array in extractor response"
            emit_fn("trait_extractor:error",
                    reason=result["error"], response_raw=content[:400])
            return result

        try:
            actions = json.loads(m.group(0))
        except Exception as e:
            result["path"] = "error"
            result["error"] = f"json parse error: {e}"
            emit_fn("trait_extractor:error",
                    reason=result["error"], response_raw=content[:400])
            return result

        if not isinstance(actions, list):
            actions = []
        actions = actions[:MAX_ACTIONS_PER_TURN]

        # Apply each action
        for act in actions:
            if not isinstance(act, dict):
                continue
            kind     = (act.get("action")   or "").strip().lower()
            category = (act.get("category") or "").strip().lower()
            section  = (act.get("section")  or "GENERAL").strip().upper()
            text     = (act.get("text")     or "").strip()
            old      = (act.get("old")      or "").strip()
            why      = (act.get("reason")   or "").strip()

            if kind == "noop" or not text or not category:
                continue

            # Normalise section
            section = re.sub(r"[^A-Z0-9-]+", "-", section).strip("-") or "GENERAL"

            try:
                if kind == "add":
                    M.add_trait(category, section, text, source="extractor")
                    result["applied"].append({
                        "action": "add", "category": category,
                        "section": section, "text": text, "reason": why,
                    })

                elif kind == "reinforce":
                    # Don't touch active file; just log to journal
                    M.append_trait_journal(category, "reinforce",
                                           section=section, text=text,
                                           source="extractor", reason=why)
                    result["applied"].append({
                        "action": "reinforce", "category": category,
                        "section": section, "text": text, "reason": why,
                    })

                elif kind == "nuance":
                    if old:
                        M.supersede_trait(category, section, old, text,
                                          source="extractor")
                    else:
                        M.add_trait(category, section, text, source="extractor")
                    result["applied"].append({
                        "action": "nuance", "category": category,
                        "section": section, "text": text, "old": old, "reason": why,
                    })

                elif kind == "supersede":
                    if not old:
                        # Falls back to add if we can't identify the old text
                        M.add_trait(category, section, text, source="extractor")
                        result["applied"].append({
                            "action": "add", "category": category,
                            "section": section, "text": text, "reason": why,
                            "note": "supersede requested but no 'old' given",
                        })
                    else:
                        M.supersede_trait(category, section, old, text,
                                          source="extractor")
                        result["applied"].append({
                            "action": "supersede", "category": category,
                            "section": section, "text": text, "old": old,
                            "reason": why,
                        })
                        # Flag for contradiction warning in the current turn
                        result["contradictions"].append({
                            "category": category, "section": section,
                            "old": old, "new": text, "reason": why,
                        })
            except Exception as e:
                logger.warning(f"trait_extractor apply failed: {e}")
                emit_fn("trait_extractor:apply_error",
                        reason=f"{kind} for [{category}/{section}] failed: {e}",
                        action=act)

        result["path"] = "extracted"
        emit_fn("trait_extractor:result",
                model=result["model"],
                latency_ms=result["latency_ms"],
                input_tokens=result["input_tokens"],
                output_tokens=result["output_tokens"],
                applied_count=len(result["applied"]),
                applied=result["applied"],
                contradictions=result["contradictions"],
                reason=(f"{len(result['applied'])} action(s) applied "
                        f"({len(result['contradictions'])} contradiction(s))"))
        return result

    except Exception as e:
        result["latency_ms"] = int((time.time() - t0) * 1000)
        result["path"] = "error"
        result["error"] = str(e)[:200]
        emit_fn("trait_extractor:error",
                reason=result["error"], model=result["model"],
                latency_ms=result["latency_ms"])
        return result
