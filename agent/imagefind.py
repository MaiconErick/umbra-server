"""
find_images: pictures the model can put inside a reply (![caption](url)).

Sources (all free, no key):
  game   Steam store search → real gameplay screenshots (fallback: Wikipedia)
  movie  Wikipedia poster of "<title> film" (series/anime too)
  photo  Openverse Creative-Commons photos → what a word / action / object looks like
  any    Wikipedia lead image (people, places, things) → Commons → Openverse

Queries run in parallel; one ~0.6 s round trip for a whole list of games.
"""
from __future__ import annotations

import concurrent.futures as cf
import re

import requests

_H = {"User-Agent": "UmbraBot/1.0 (personal assistant; contact via Pi)"}
_T = 6


def _steam(name: str, n: int) -> list[dict]:
    s = requests.get("https://store.steampowered.com/api/storesearch/",
                     params={"term": name, "cc": "us", "l": "en"}, headers=_H, timeout=_T).json()
    items = s.get("items") or []
    if not items:
        return []
    # prefer an exact-ish title match over DLC / soundtracks
    want = re.sub(r"\W+", "", name.lower())
    items.sort(key=lambda it: (re.sub(r"\W+", "", it.get("name", "").lower()) != want,
                               any(w in it.get("name", "").lower() for w in ("soundtrack", "dlc", "artbook"))))
    app = items[0]
    d = requests.get("https://store.steampowered.com/api/appdetails",
                     params={"appids": app["id"], "filters": "screenshots"}, headers=_H, timeout=_T).json()
    shots = ((d.get(str(app["id"])) or {}).get("data") or {}).get("screenshots") or []
    out = []
    for sh in shots[:n]:
        out.append({"url": sh.get("path_full") or sh.get("path_thumbnail"), "title": app.get("name", name),
                    "source": "Steam", "width": 1920, "height": 1080})
    return out


def _wikipedia(name: str, n: int) -> list[dict]:
    j = requests.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "generator": "search", "gsrsearch": name, "gsrlimit": 3,
        "prop": "pageimages", "piprop": "thumbnail|name", "pithumbsize": 900, "pilicense": "any", "format": "json"},
        headers=_H, timeout=_T).json()
    pages = sorted(((j.get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 99))
    out = []
    for p in pages:
        th = p.get("thumbnail")
        words = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 2]
        if th and words and not any(w in p.get("title", "").lower() for w in words):
            th = None
        if th:
            out.append({"url": th["source"], "title": p.get("title", name), "source": "Wikipedia",
                        "width": th.get("width"), "height": th.get("height")})
        if len(out) >= n:
            break
    return out


def _commons(name: str, n: int) -> list[dict]:
    j = requests.get("https://commons.wikimedia.org/w/api.php", params={
        "action": "query", "generator": "search", "gsrsearch": name, "gsrnamespace": 6, "gsrlimit": n + 2,
        "prop": "imageinfo", "iiprop": "url|size|mime", "iiurlwidth": 900, "format": "json"},
        headers=_H, timeout=_T).json()
    out = []
    for p in ((j.get("query") or {}).get("pages") or {}).values():
        ii = (p.get("imageinfo") or [{}])[0]
        if not str(ii.get("mime", "")).startswith("image/") or ii.get("mime") == "image/svg+xml":
            continue
        title = p.get("title", "").removeprefix("File:")
        words = [w for w in re.findall(r"\w+", name.lower()) if len(w) > 2]
        if words and sum(w in title.lower() for w in words) < max(1, (len(words) + 1) // 2):
            continue   # Commons search is loose: skip pictures whose name doesn't match
        out.append({"url": ii.get("thumburl") or ii.get("url"), "title": title,
                    "source": "Wikimedia", "width": ii.get("thumbwidth"), "height": ii.get("thumbheight")})
        if len(out) >= n:
            break
    return out


def _openverse(name: str, n: int) -> list[dict]:
    """Creative-Commons photos (mostly Flickr) — actions, objects, animals, food, concepts."""
    j = requests.get("https://api.openverse.org/v1/images/", params={
        "q": name, "page_size": max(n, 4), "mature": "false"}, headers=_H, timeout=_T).json()
    out = []
    for r in j.get("results") or []:
        if not r.get("url"):
            continue
        out.append({"url": r["url"], "title": (r.get("title") or name)[:80], "source": "Openverse/" + (r.get("source") or "cc"),
                    "width": r.get("width"), "height": r.get("height")})
        if len(out) >= n:
            break
    return out


def _wiki_movie(name: str, n: int) -> list[dict]:
    got = _wikipedia(name + " film", n)
    return got or _wikipedia(name, n)


def _one(q: str, kind: str, n: int) -> dict:
    chain = {"game": (_steam, _wikipedia, _commons),
             "movie": (_wiki_movie, _commons),
             "photo": (_openverse, _commons, _wikipedia)}.get(kind, (_wikipedia, _commons, _openverse))
    for fn in chain:
        try:
            got = [x for x in fn(q, n) if x.get("url")]
        except Exception:
            got = []
        if got:
            return {"query": q, "images": got}
    return {"query": q, "images": []}


def find(queries: list[str], kind: str = "any", per: int = 1) -> list[dict]:
    qs = [q.strip() for q in (queries or []) if q and q.strip()][:10]
    per = max(1, min(int(per or 1), 4))
    kind = (kind or "any").lower()
    with cf.ThreadPoolExecutor(max_workers=min(8, len(qs) or 1)) as ex:
        return list(ex.map(lambda q: _one(q, kind, per), qs))


def as_tool_result(res: list[dict]) -> str:
    lines = ["Images found. Put each one in your reply as markdown on its own line, right under the item it shows: "
             "![short caption](url). Skip ones that don't fit."]
    for r in res:
        if not r["images"]:
            lines.append(f"- {r['query']}: nothing found")
            continue
        for im in r["images"]:
            lines.append(f"- {r['query']} ({im['source']}: {im['title']}): {im['url']}")
    return "\n".join(lines)
