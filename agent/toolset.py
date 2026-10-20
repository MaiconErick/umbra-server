"""
Which tool schemas go to the model, and how merged tools map to handlers.

Before: 14 memory + agent tools (~1,300 tokens) went with EVERY call, and the
other tools had to be ticked per chat. Now:

  always sent   memory      — one tool for every memory write (op=...)
                load_tools  — lists the extra tools; the model loads what it needs
                web_search  — tiny, and the most common need
  on demand     shell, python, read_file/write_file/list_dir, browser,
                web_search, generate_image, tts, system_snapshot,
                agents (scheduling / reminders), categorize_chat, switch_topic,
                switch_model

A tool the model loads stays loaded for that chat (chat_meta.loaded_tools),
so later replies don't pay the extra step and the prompt-cache prefix stays
stable. Tools ticked in the chat settings (chat_meta.enabled_tools) and the
ones a persona declares are simply pre-loaded.
"""
from __future__ import annotations

# ── always-on schemas (kept short: descriptions carry the field list) ──
MEMORY = {"type": "function", "function": {
    "name": "memory",
    "description": (
        "Your memory files about Maicon. A note-taker already saves what matters after every reply, so only write when "
        "he asks you to remember something. op + fields:\n"
        "read(page[,section]) open a page/section from the memory map before answering\n"
        "note(page,section,text[,aliases]) e.g. page='media/anime', section='JUJUTSU KAISEN', aliases=['jjk']\n"
        "rule(text) how to talk to him / 'from now on'\n"
        "about(text) a core fact about who he is\n"
        "fact(fact) machine fact: IP, path, port, hardware\n"
        "word(word,lang[,translation,context]) vocabulary he's learning"
    ),
    "parameters": {"type": "object", "properties": {
        "say": {"type": "string", "description": "One short sentence (max ~90 chars, English) telling Maicon, in your own words, what this call is for before it happens. Shown above the bubble in the app. Don't repeat the command; react to what the last result showed."},
        "op": {"type": "string", "enum": ["read", "note", "rule", "about", "fact", "word"]},
        "page": {"type": "string"}, "section": {"type": "string"}, "text": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "fact": {"type": "string"},
        "word": {"type": "string"}, "lang": {"type": "string"}, "translation": {"type": "string"}, "context": {"type": "string"},
    }, "required": ["op"]},
}}

AGENTS = {"type": "function", "function": {
    "name": "agents",
    "description": (
        "Scheduled background agents (also how you send Maicon a reminder later). "
        "op=create(name,trigger,action[,tint]) | list | update(id, any of name/trigger/action/enabled) | delete(id) | run(id). "
        "(Older docs call these create_agent / list_agents / update_agent / delete_agent / run_agent_now.) "
        "trigger={type:'cron',expr:'0 10 14 6 *'} for one-shot/clock times or {type:'interval',seconds:>=30}. "
        "action={type:'shell',command} | {type:'python',code} | {type:'prompt',prompt,session_id,push}. "
        "Reminder: type='prompt', push=true, session_id=this chat, prompt = instructions to your future self "
        "(\"Maicon ia aumentar a dose hoje. Pergunta se já aumentou.\"). Only when Proactive Mode is enabled."
    ),
    "parameters": {"type": "object", "properties": {
        "say": {"type": "string", "description": "One short sentence (max ~90 chars, English) telling Maicon, in your own words, what this call is for before it happens. Shown above the bubble in the app. Don't repeat the command; react to what the last result showed."},
        "op": {"type": "string", "enum": ["create", "list", "update", "delete", "run"]},
        "id": {"type": "string"}, "name": {"type": "string"},
        "trigger": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["interval", "cron"]}, "seconds": {"type": "integer"}, "expr": {"type": "string"}}},
        "action": {"type": "object", "properties": {
            "type": {"type": "string", "enum": ["shell", "python", "prompt"]}, "command": {"type": "string"},
            "code": {"type": "string"}, "prompt": {"type": "string"}, "session_id": {"type": "string"},
            "push": {"type": "boolean"}, "tools": {"type": "array", "items": {"type": "string"}}}},
        "enabled": {"type": "boolean"}, "tint": {"type": "string"},
    }, "required": ["op"]},
}}

WEB_SEARCH = {"type": "function", "function": {
    "name": "web_search",
    "description": "Search the web for anything recent or that you're unsure of (releases, news, prices, dates, facts) and for recommendations. Returns an answer with sources. Write the query like a precise question. reddit=true also pulls what Reddit threads say — use it for recommendations, opinions, experiences, 'is X worth it'.",
    "parameters": {"type": "object", "properties": {"say": {"type": "string", "description": "One short sentence (max ~90 chars, English) saying what this search is for, in your own words. Shown above the search bubble in the app."}, "query": {"type": "string"}, "reddit": {"type": "boolean"}}, "required": ["query"]},
}}

FIND_IMAGES = {"type": "function", "function": {
    "name": "find_images",
    "description": "Find real pictures to show inside your reply. One call for the whole list. "
                   "kind: game (Steam gameplay screenshots) | movie (posters; series/anime too) | "
                   "photo (real photos of an action, object, animal, food — e.g. what a word means; write the query in English) | "
                   "any (people, places, things). Then put ![caption](url) on its own line under each item.",
    "parameters": {"type": "object", "properties": {
        "say": {"type": "string", "description": "One short sentence (max ~90 chars, English) saying why these pictures are being pulled, in your own words."},
        "queries": {"type": "array", "items": {"type": "string"}, "description": "exact names, e.g. game titles"},
        "kind": {"type": "string", "enum": ["game", "movie", "photo", "any"]},
        "per": {"type": "integer", "description": "pictures per query, 1-4 (default 1)"}},
        "required": ["queries"]},
}}

# One line each, shown inside load_tools' description.
LOADABLE = {
    "web_search": "search the internet (news, releases, prices, facts)",
    "shell": "run bash on the Pi (also loads read_file, write_file, list_dir)",
    "python": "run Python on the Pi",
    "read_file": "read a file on the Pi",
    "write_file": "write a whole file on the Pi",
    "edit_file": "change part of a file without rewriting it (exact snippet; best for code)",
    "grep": "search file contents under a folder (returns file:line: text)",
    "glob": "find files by name pattern, newest first",
    "repo_map": "code map: find a class/function/file by name without grepping",
    "run": "build or test a project folder (gradle / npm / pytest / make)",
    "todo": "the task list of this chat (set steps, mark them done)",
    "jobs": "run a long command in the background and check its log later",
    "task": "hand work to a sub-agent with a fresh context; returns a short report",
    "list_dir": "list a folder on the Pi",
    "browser": "headless Vivaldi: open/scrape/screenshot/click/type on a known URL",
    "generate_image": "create an image from a prompt",
    "tts": "turn text into speech audio",
    "system_snapshot": "Pi health: RAM, disk, load, IP, uptime",
    "agents": "schedule jobs and reminders (cron/interval; shell/python/prompt)",
    "categorize_chat": "tag/group this chat",
    "switch_topic": "suggest starting a new chat for a new subject",
    "switch_model": "change the global default model",
    "network": "home network devices: list, name/forget a device, scan now",
}

LOAD_TOOLS = {"type": "function", "function": {
    "name": "load_tools",
    "description": "Load extra tools, then call them in your next step. They stay loaded in this chat. Available: "
                   + "; ".join(f"{k} — {v}" for k, v in LOADABLE.items() if k != "web_search"),
    "parameters": {"type": "object", "properties": {"say": {"type": "string", "description": "One short sentence (max ~90 chars, English) saying which capability you need and why."}, "names": {"type": "array", "items": {"type": "string"}}}, "required": ["names"]},
}}

IMPLIES = {"shell": {"read_file", "write_file", "list_dir", "edit_file"},
           "python": {"read_file", "write_file", "edit_file"}}

# old per-tool names → the tool that replaced them
LEGACY = {
    "create_agent": "agents", "list_agents": "agents", "update_agent": "agents",
    "delete_agent": "agents", "run_agent_now": "agents",
    "add_rule": "memory", "add_fact": "memory", "add_trait": "memory", "update_topic": "memory",
    "create_topic": "memory", "remember_about_user": "memory", "learn_word": "memory",
    "recall_trait_history": "memory", "update_session": "memory",
}

MEMORY_OPS = {"note": "nb_note", "rule": "nb_rule", "about": "nb_about", "fact": "add_fact", "word": "nb_word",
              "read": "read_memory",
              # older names the model may still use
              "remember": "nb_note", "trait": "nb_note", "topic": "nb_note", "new_topic": "nb_note"}
AGENT_OPS = {"create": "create_agent", "list": "list_agents", "update": "update_agent",
             "delete": "delete_agent", "run": "run_agent_now"}


def normalize(names) -> set[str]:
    """Map old names to the new ones, drop always-on/unknown, add implied tools."""
    out: set[str] = set()
    for n in names or []:
        n = LEGACY.get(n, n)
        if n in LOADABLE:
            out.add(n)
    for n in list(out):
        out |= IMPLIES.get(n, set())
    return out


_BY_NAME: dict | None = None


def _by_name() -> dict:
    """Tool schemas indexed by name. Built once: `build` runs on every tool
    round of a turn, and rebuilding this map each time was pure waste."""
    global _BY_NAME
    if _BY_NAME is None:
        from agent.tools import TOOL_DEFINITIONS_COMPACT
        _BY_NAME = {t["function"]["name"]: t for t in TOOL_DEFINITIONS_COMPACT}
        _BY_NAME["agents"] = AGENTS
        _BY_NAME["web_search"] = WEB_SEARCH
    return _BY_NAME


def build(loaded: set[str]) -> list[dict]:
    """Schemas for one model call: the two always-on tools + whatever is loaded."""
    by_name = _by_name()
    # web_search is tiny (~60 tokens) and the most common need: always offered,
    # so "what's new with X" doesn't cost an extra load_tools step.
    out = [MEMORY, LOAD_TOOLS, WEB_SEARCH, FIND_IMAGES]
    for n in sorted(loaded - {"web_search"}):
        if n in by_name:
            out.append(by_name[n])
    return out


def route(name: str, args: dict) -> tuple[str, dict]:
    """memory/agents calls → the original handler name + args."""
    args = dict(args or {})
    if name == "memory":
        op = (args.pop("op", "") or "").strip().lower()
        return MEMORY_OPS.get(op, f"memory:{op or '?'}"), args
    if name == "agents":
        op = (args.pop("op", "") or "").strip().lower()
        return AGENT_OPS.get(op, f"agents:{op or '?'}"), args
    return name, args


def catalog() -> list[dict]:
    """For the app's tool picker."""
    return [{"name": k, "description": v} for k, v in LOADABLE.items()]
