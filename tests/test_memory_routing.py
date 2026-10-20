"""A suggestion request must see the domain page (games, anime...) whole."""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import agent.core as C
from store import notebook as NB
from agent.core import UmbraAgent

fails = []


def check(label, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + label + ("" if cond else "   <- " + str(detail)[:200]))
    if not cond:
        fails.append(label)


ask = "Pesquisa na internet jogos de theory crafting"
check("reco intent detected", bool(C._RECO_RE.search(ask)), ask)
check("research intent detected", bool(C._RESEARCH_RE.search(ask)), ask)
check("domain -> games page", C._domain_pages(ask) == ["games/games"], C._domain_pages(ask))
check("plain chat has no domain", C._domain_pages("oi, tudo bem?") == [], C._domain_pages("oi, tudo bem?"))
check("anime ask -> anime page", C._domain_pages("me sugere animes parecidos com Frieren") == ["media/anime"],
      C._domain_pages("me sugere animes parecidos com Frieren"))

txt, picked = NB.context_for(ask, [], whole_pages=True, force=["games/games"])
check("games page picked", "games/games" in picked, picked)
check("known game listed for the model", "Siralim" in txt, txt[:300])

txt2, picked2 = NB.context_for("xyz", [], force=["nope/nothing"])
check("unknown forced page is ignored", "nope/nothing" not in picked2, picked2)

# the real thing: what the prompt actually carries for that message
a = UmbraAgent()
a.bind_session(None)
prompt = a._build_system_prompt(ask)
check("prompt carries the games page", "Siralim" in prompt or "SIRALIM" in prompt.upper(), prompt[:200])
check("prompt mentions his taste", "theorycraft" in prompt.lower() or "complexidade" in prompt.lower()
      or "complexity" in prompt.lower())

prompt2 = a._build_system_prompt("Qual a capital da França?")
check("unrelated question stays lean", "Siralim" not in prompt2, len(prompt2))

# the general routing holes: a note must answer to its own words, not only to
# its exact title
txt3, picked3 = NB.context_for("quero jogar siralim de novo", [], whole_pages=False)
check("partial title finds the section", picked3.get("games/games") == ["SIRALIM ULTIMATE"], picked3)
check("section body reaches the prompt", "theorycrafting" in txt3.lower() or "complexity" in txt3.lower(), txt3[:200])

txt4, picked4 = NB.context_for("o que eu acho de disgaea", [], whole_pages=False)
check("a name inside a note finds that note", picked4.get("games/games") == ["JRPG RECOMMENDATIONS INTEREST"], picked4)

txt5, picked5 = NB.context_for("oi, tudo bem?", [], whole_pages=False)
check("small talk loads nothing", not picked5, picked5)

pr = a._build_system_prompt("quero jogar siralim de novo")
check("prompt has the note for that game", "Siralim" in pr)

print("FAILURES:", len(fails))
sys.exit(1 if fails else 0)
