"""
The note-taker: after every reply (in the background, so nobody waits) a small
model reads the exchange and updates Umbra's memory files — new tastes, what Maicon
watched/played/bought, progress, opinions, plans — as short dated lines on the
right page and section. It prefers changing an existing line ("watching S2" →
"finished S2, loved it") over piling up new ones.
"""
from __future__ import annotations

import json
import logging
import re

from store import notebook as NB

logger = logging.getLogger(__name__)

_PROMPT = """You keep Umbra's memory files about Maicon (the user). Read the exchange and decide what, if anything,
is worth remembering LONG-TERM about Maicon. Small talk, tests and plain questions need nothing — then answer {{"changes": []}}.
But whenever he shares something about HIMSELF — what he watched, played, finished, bought, liked, disliked, felt, plans,
his progress, his opinion of something — that IS worth a note, even if it's said in passing.

Worth noting: his tastes and opinions (liked / disliked / why), what he watched, played, read, bought or is
doing now (with season/episode/progress), plans and wishes ("wants to watch X"), people in his life, health,
routines, projects, things he's learning. Facts about him, in his favour or not.
NOT worth noting: greetings, tests, thanks, one-off requests ("make an image of a lion"), things Umbra said
or looked up, opinions Umbra gave, anything about the assistant itself.

The memory files (page: sections):
{map}

Already written in the relevant sections:
{current}

Pages you can also create: {suggested}
(or another page under media/, games/, tech/, life/, learning/, shopping/, work/ when nothing fits)

How to write:
- One section per specific thing (a show, game, person, device…) named in CAPS with its usual title, e.g.
  [JUJUTSU KAISEN], [DRAGON QUEST IX], [THE OWL HOUSE]. Add short nicknames people use (aliases), e.g. ["jjk"],
  ["dq9", "dragon quest 9"], ["owl house"]. Use [GENERAL] for his taste about the whole subject.
- Lines are short, concrete, in English, about Maicon, e.g. "Watched S1 (Jan 2026), didn't like it much: not enough magic".
- If a line already says something about the same thing, use "update" with "old" = that line, instead of adding.
- Something that holds for how Umbra should talk to him ("call me X", "don't say tu") → op "rule".
- A core fact about who he is (job, city, languages, family basics) → op "about".
- Big page? When a page reaches ~12 sections (or ~6 KB), split it: move the narrower ones to a
  sub-page — games/jrpg, games/indie, media/music-2020s, media/anime-seasonal — by adding them there
  and removing them from the parent (one "add" + one "remove" per section). Taste sections [GENERAL]
  stay in the parent. Never leave a copy in both pages.
- Only the categories he actually talks about: do not create empty pages.

Exchange:
MAICON: {user}
UMBRA: {reply}

Answer JSON only:
{{"changes": [
  {{"op": "add", "page": "media/anime", "section": "JUJUTSU KAISEN", "aliases": ["jjk"], "text": "..."}},
  {{"op": "update", "page": "...", "section": "...", "old": "existing line", "text": "new line"}},
  {{"op": "remove", "page": "...", "section": "...", "old": "line that is no longer true"}},
  {{"op": "rule", "text": "..."}},
  {{"op": "about", "text": "..."}}
]}}"""

_TRIVIAL = re.compile(r"^\W*(oi+e?|ol[aá]|hi|hey|hello|bom dia|boa (tarde|noite)|ok|blz|beleza|vlw|valeu|obrigad\w*|thanks?|"
                      r"kk+|haha+|sim|n[aã]o|yes|no|test\w*)\W*$", re.I)


def _model() -> tuple[str, str]:
    try:
        from agent.model_roles import get_role
        for role in ("note_taker", "trait_extractor"):
            r = get_role(role) or {}
            if r.get("provider") and r.get("model"):
                return r["provider"], r["model"]
    except Exception:
        pass
    return "openai", "gpt-4.1-mini"


def take_notes(user: str, reply: str, sid: str | None = None, emit=None) -> list[dict]:
    """Run once per turn, in a background thread. Returns the changes applied."""
    import time as _t
    t0 = _t.time()
    NB.set_context(sid, "note-taker")
    user = (user or "").strip()
    if len(user) < 6 or _TRIVIAL.match(user):
        if emit:
            emit("notes_checked", changes=[], skipped="too short / just a greeting", ms=0)
        return []
    hits = NB.match(user)
    current = []
    for pid, secs in hits.items():
        current.append(f"## {pid}\n" + NB.Page.load(pid).render(only=secs, header=False))
    about, rules = NB.always_text()
    if about:
        current.append("## about-me\n" + about)
    if rules:
        current.append("## rules\n" + rules)
    prompt = _PROMPT.format(
        map=NB.map_text() or "(empty — create pages as needed)",
        current="\n".join(current) or "(nothing yet)",
        suggested=", ".join(p for p in NB.SUGGESTED if p not in NB.pages()),
        user=user[:2500], reply=(reply or "")[:1500])
    try:
        from agent.providers import quick_completion
        prov, model = _model()
        r, used = quick_completion(prov, model, label="notes", sid=sid,
                                   messages=[{"role": "user", "content": prompt}],
                                   response_format={"type": "json_object"}, max_tokens=700, temperature=0.2)
        raw = r.choices[0].message.content or "{}"
        data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1] or "{}")
    except Exception as e:
        logger.warning(f"note-taker failed: {e}")
        if emit:
            emit("notes_checked", changes=[], error=str(e)[:200], ms=int((_t.time() - t0) * 1000))
        return []
    done = []
    for c in (data.get("changes") or [])[:8]:
        op = (c.get("op") or "").lower()
        try:
            if op == "add" and c.get("page") and c.get("text"):
                res = NB.add(c["page"], c.get("section") or "GENERAL", c["text"], c.get("aliases"), source="notes")
            elif op == "update" and c.get("page") and c.get("text"):
                res = NB.update(c["page"], c.get("section") or "GENERAL", c.get("old") or "", c["text"], source="notes")
            elif op == "remove" and c.get("page") and c.get("old"):
                res = NB.remove(c["page"], c.get("section") or "GENERAL", c["old"], source="notes")
            elif op == "rule" and c.get("text"):
                res = NB.add(NB.RULES, "GENERAL", c["text"], source="notes")
            elif op == "about" and c.get("text"):
                res = NB.add(NB.ABOUT, "GENERAL", c["text"], source="notes")
            else:
                continue
            done.append({**c, "result": res})
        except Exception as e:
            logger.warning(f"note-taker change failed {c}: {e}")
    if emit:
        try:
            emit("notes_checked", changes=done, model=f"{prov}/{model}", ms=int((_t.time() - t0) * 1000))
        except Exception:
            pass
    return done
