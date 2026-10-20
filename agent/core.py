"""
Umbra agent core — main LLM loop with tool calling and memory.
"""
import os, json, logging, configparser, re, time, base64
from dotenv import load_dotenv

from agent.providers  import get_client, get_current
from agent.tools      import TOOL_DEFINITIONS, TOOL_DEFINITIONS_COMPACT, dispatch_tool, tool_system_snapshot, TEMP_DIR
from agent import summary_worker
from store import manager as M
from store import router as topic_router
from store.manager   import (
    # legacy/compat names still used directly below
    get_rules, get_all_sessions, get_memory,
    create_session, update_session, append_memory, add_rule, append_session_log,
    get_session_summary, save_prompt_snapshot, get_user_facts, append_user_fact,
    set_chat_meta, get_chat_meta,
    # new layered-memory API
    load_system_prompt, load_rules, load_facts,
    list_topics, load_topic, update_topic_section, create_topic,
    list_trait_categories, load_trait, add_trait, update_trait_section,
    supersede_trait, read_trait_journal,
    add_fact, learn_word, get_summary, update_summary,
    save_pending_state, take_pending_state, clear_pending_state,
    load_read_ledger, save_read_ledger, clear_read_ledger,
)
from store import messages as msg_store
from agent import toolset
import shutil, uuid, threading

load_dotenv()
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG   = configparser.ConfigParser()
CONFIG.read(os.path.join(BASE_DIR, "config.ini"))

def _max_tool_loops() -> int:
    try:
        return CONFIG.getint("limits", "max_tool_loops", fallback=60)
    except Exception:
        return 60


MAX_TOOL_LOOPS = _max_tool_loops()

# How many times a turn may extend its own tool budget before it stops and
# asks the user. One extension covers "I hit the limit mid-task", which used
# to need a manual "continue" every single time.
MAX_BUDGET_EXTENSIONS = 1

# How much conversation history one request carries. Both the live trim and
# the rebuild after a restart use these, so a chat looks the same either way.
HISTORY_MAX_CHARS    = 24000
HISTORY_MAX_MESSAGES = 60

# A shell command that *runs a build or test* counts as "I actually checked
# the change": after an edit with no build, the turn gets nudged to verify
# instead of claiming it works. Matched per shell segment and from the start
# of the segment, so `echo make it work` does not count.
_BUILD_START = re.compile(
    r"^(?:\./)?(gradlew|npm|yarn|pnpm|pytest|go|cargo|mvn|mvnw|dotnet|"
    r"make|tsc|flutter|swift|xcodebuild|python3?)\b", re.IGNORECASE)
# Wrappers people put in front of the real command: sudo, bash -c "…", quotes.
_WRAPPER = re.compile(r"^(?:sudo|nohup|time|bash|sh|zsh|env)\b\s*(?:-[a-zA-Z]+\s*)?|^[\"']",
                      re.IGNORECASE)


def _strip_wrappers(s: str) -> str:
    for _ in range(4):
        m = _WRAPPER.match(s)
        if not m or m.end() == 0:
            break
        s = s[m.end():].lstrip()
    return s


_CODE_EXT = (".py", ".pyw", ".kt", ".kts", ".java", ".ts", ".tsx", ".js", ".jsx", ".c", ".h",
             ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".rs", ".go", ".sh", ".lua", ".dart", ".swift",
             ".m", ".mm", ".scala", ".rb", ".php", ".vue", ".svelte", ".json", ".gradle", ".kts")


def _is_code_file(path) -> bool:
    """Only real code needs a build after an edit; a note or a .txt does not."""
    return str(path or "").lower().endswith(_CODE_EXT)


def _is_build_command(command: str) -> bool:
    for part in re.split(r"&&|\|\||;|\n|\|", command or ""):
        s = _strip_wrappers(part.strip())
        m = _BUILD_START.match(s)
        if not m:
            continue
        tool = m.group(1).lower()
        if tool in ("npm", "yarn", "pnpm"):
            if re.search(r"\b(build|test|check|lint|compile)\b", s[m.end():]):
                return True
        elif tool.startswith("python"):
            if re.search(r"-m\s+(pytest|unittest)", s):
                return True
        else:
            return True
    return False


def _msg_chars(m: dict) -> int:
    content = m.get("content")
    if isinstance(content, list):
        return sum(len(str(p.get("text") or "")) for p in content if isinstance(p, dict))
    return len(str(content or ""))


def trim_history(conv: list[dict]) -> list[dict]:
    """Keep the newest messages inside the history budget.

    The old rule kept a fixed 40 messages, which is not the same thing: one
    tool round costs two messages, so a tool-heavy turn carried far less
    context than a normal chat, and a restored chat (24k characters) did not
    match a live one. Trimmed history must still start with a user turn — a
    tool result whose call was dropped is rejected by every provider.
    """
    keep_from = 0
    total = sum(_msg_chars(m) for m in conv)
    while keep_from < len(conv) - 2 and (total > HISTORY_MAX_CHARS
                                         or len(conv) - keep_from > HISTORY_MAX_MESSAGES):
        total -= _msg_chars(conv[keep_from])
        keep_from += 1
    out = conv[keep_from:]
    for i, m in enumerate(out):
        if m.get("role") == "user":
            return out[i:]
    return []


def _max_output_tokens() -> int:
    """Output cap for one reply. Was a hardcoded 4096 for every model, which
    cuts long answers mid-sentence; config.ini can raise it."""
    try:
        v = CONFIG.getint("general", "max_output_tokens", fallback=0)
    except Exception:
        v = 0
    return v if v and v > 0 else 4096


def _short(value, limit: int = 140) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


def _tool_note(name: str, args: dict, say: str = "") -> str:
    """One plain sentence saying what a tool call is doing (shown by the app).

    The model sends its own sentence in the `say` argument ("Let me open the
    memory file to check whether that is documented"). When it does, that is
    what the app shows above the command bubble; the generic fallbacks below
    only exist for calls that arrived without it.
    """
    name = (name or "").strip()
    args = args or {}
    said = str(say or args.get("say") or "").strip()
    if said:
        return _short(said, 170)
    # Fallbacks never paste the raw command/code: the bubble already shows it
    # on its own line, and a pasted command there is what made the header read
    # like one giant line of shell instead of a sentence.
    if name == "shell":
        cmd = str(args.get("command") or "")
        if re.search(r"\b(sed|awk|grep|head|tail|cat|wc)\b", cmd):
            return "Reading files on the Pi"
        if re.search(r"\b(apt|pip|npm|make|systemctl|service)\b", cmd):
            return "Changing something on the Pi"
        return "Running a command on the Pi"
    if name == "python":
        return "Running Python on the Pi"
    if name == "web_search":
        return "Searching the web: " + _short(args.get("query"), 90)
    if name == "find_images":
        q = args.get("queries") or args.get("query")
        if isinstance(q, list):
            q = ", ".join(str(x) for x in q[:3])
        return "Looking for real pictures: " + _short(q, 80)
    if name == "generate_image":
        return "Generating an image"
    if name == "memory":
        op = args.get("op") or "read"
        sec = args.get("section") or args.get("page") or ""
        return "Checking my memory (" + str(op) + (" \u00b7 " + str(sec) if sec else "") + ")"
    if name in ("read_file", "file_read"):
        return "Reading " + _short(args.get("path"), 100)
    if name in ("write_file", "file_write"):
        return "Writing " + _short(args.get("path"), 100)
    if name in ("list_dir", "ls"):
        return "Listing " + _short(args.get("path") or args.get("dir"), 100)
    if name in ("load_tools", "load_tool"):
        names = args.get("names") or args.get("name") or ""
        if isinstance(names, list):
            names = ", ".join(str(x) for x in names)
        return "Loading tools: " + _short(names, 80)
    if name in ("browser", "open_url"):
        return "Browsing " + _short(args.get("url") or args.get("query"), 90)
    if name in ("tts", "speak"):
        return "Turning text into speech"
    if name in ("system_snapshot", "snapshot"):
        return "Checking the Pi's health"
    if name == "network":
        return "Looking at the home network (" + str(args.get("op") or "list") + ")"
    if name in ("agents", "schedule"):
        return "Scheduling a job"
    return ("Using " + name) if name else "Using a tool"


def _limit_message() -> str:
    """Shown when a turn burns its whole tool budget. Says what happened and
    what the user can do, instead of a bracketed machine note."""
    return _limit_message_body()


def _limit_message_body() -> str:
    return (f"I hit my tool budget for this turn ({MAX_TOOL_LOOPS} calls) and stopped "
            "there. The work so far is above. Tell me \"continue\" and I pick up "
            "where I left off with a fresh budget, or narrow the task and I go straight to it.")


def _args_key(name: str, args: dict) -> str:
    try:
        # `say` is only the sentence shown in the app; two calls with the same
        # real arguments are still duplicates even if the wording changed.
        _a = {k: v for k, v in (args or {}).items() if k != "say"}
        return name + "|" + json.dumps(_a, sort_keys=True, default=str)
    except Exception:
        return name + "|" + str(args)


# ─────────────────────────────────────────
#  File-read guard
# ─────────────────────────────────────────
# The old dedup only caught byte-identical calls, so `sed -n '40,120p' f.kt`,
# `sed -n '121,200p' f.kt` and `grep -n foo f.kt` all counted as different and
# one single file could be re-read dozens of times in a turn (that is what ate
# whole 60-call budgets without ever reaching a write). The guard below counts
# per FILE, not per command.
_READ_VERBS   = ("cat", "sed", "awk", "head", "tail", "grep", "egrep", "less",
                 "more", "wc", "nl", "tac", "sort", "uniq", "stat", "file", "od")
_WRITE_VERBS  = ("tee", "patch", "truncate", "touch", "mkdir", "mv", "cp", "rm",
                 "install", "chmod", "chown", "dd", "ln", "write_file")
_READ_TOOLS   = ("read_file", "file_read")
_WRITE_GATE_READS   = 25   # after this, reads are closed for the turn
_MAX_REFUNDS        = 8    # rounds where every tool call was refused get their slot back


def _looks_like_path(tok: str) -> bool:
    tok = tok.strip("'\"`()[]{};,")
    if not tok or tok.startswith("-") or tok.startswith("$"):
        return False
    if len(tok) < 3 or len(tok) > 260:
        return False
    if "://" in tok or tok.startswith("http"):
        return False
    if "/" not in tok and "." not in tok:
        return False
    return True


def _range_key(cmd: str) -> str:
    """What part of a file a read command is asking for. Two reads of the same
    file are only redundant when they ask for the same slice: `sed -n '1,120p'`
    and `sed -n '400,520p'` on a 900-line file are both legitimate."""
    m = re.search(r"sed\b[^|;]*?-n\s*['\"]?\s*(\d+)\s*(?:,|\s)\s*(\d+)\s*p", cmd)
    if m:
        return "sed:%s-%s" % (m.group(1), m.group(2))
    m = re.search(r"awk\b[^|;]*NR\s*>=\s*(\d+)[^;]*NR\s*<=\s*(\d+)", cmd)
    if m:
        return "awk:%s-%s" % (m.group(1), m.group(2))
    m = re.search(r"\bhead\b[^|;]*?(?:-n\s*|-)(\d+)", cmd)
    if m:
        return "head:%s" % m.group(1)
    m = re.search(r"\btail\b[^|;]*?(?:-n\s*|-)(\d+)", cmd)
    if m:
        return "tail:%s" % m.group(1)
    m = re.search(r"\b(?:grep|egrep)\b([^|;]*)", cmd)
    if m:
        pat = re.sub(r"\s+", " ", m.group(1)).strip()
        # drop the file arguments so the key is just "what was searched for"
        pat = " ".join(w for w in pat.split()
                       if not _looks_like_path(w))
        return "grep:" + pat[:100]
    return "whole"


def _read_targets(name: str, args: dict) -> list:
    """Files a call is about to read. Empty list = not a read, guard ignores it."""
    if name in _READ_TOOLS:
        p = str(args.get("path") or "").strip()
        return [os.path.abspath(p)] if p else []
    if name != "shell":
        return []
    cmd = str(args.get("command") or "")
    if not cmd or len(cmd) > 4000:
        return []
    words = re.split(r"[\s|;&()<>]+", cmd)
    verbs = [w for w in words if w]
    # Only arguments that come after the read verb are files being read.
    # `venv/bin/python -c "..." | tail -5` reads nothing from disk: `python`
    # sits before `tail`, so it must not be counted as a read target.
    first = next((i for i, w in enumerate(verbs) if w in _READ_VERBS), None)
    if first is None:
        return []
    verbs = verbs[first + 1:]
    if any(w in _WRITE_VERBS for w in verbs):
        return []
    # `sed -i`, `awk -i inplace`, redirects, heredocs: those change the file
    if re.search(r"\bsed\b[^|;]*\s-i|\bawk\b[^|;]*inplace|(?:^|[\s;|&])>+\s*[^\s&|]|\btee\b|\bsort\b[^|;]*\s-o\b", cmd):
        return []
    out = []
    for tok in verbs:
        if not _looks_like_path(tok):
            continue
        full = os.path.abspath(os.path.expanduser(tok))
        if os.path.isfile(full) and full not in out:
            out.append(full)
    return out[:4]

# ─────────────────────────────────────────
#  Prompt-cache helpers
# ─────────────────────────────────────────
def _caching_enabled() -> bool:
    CONFIG.read(os.path.join(BASE_DIR, "config.ini"))
    return CONFIG.getboolean("general", "enable_prompt_caching", fallback=False)

def _model_caching_status(provider: str, model: str) -> str:
    """Return one of: 'explicit', 'auto', 'unsupported'.
      explicit  → needs cache_control hints (Anthropic via OpenRouter / Anthropic direct).
      auto      → provider caches automatically for prompts >1024 tokens (OpenAI direct, OR-OpenAI, Gemini).
      unsupported → no provider-side caching (Ollama)."""
    p = (provider or "").lower()
    m = (model or "").lower()
    if p == "openrouter":
        return "explicit" if m.startswith("anthropic/") else "auto"
    if p == "anthropic":
        return "explicit"
    if p in ("openai", "google", "gemini"):
        return "auto"
    if p == "ollama":
        return "unsupported"
    return "auto"

def _wrap_system_for_cache(system_text: str) -> list[dict]:
    """OpenRouter/OpenAI-compat way to mark a system message for Anthropic
    prompt caching: send `content` as a content-block list with a final
    cache_control marker on the last block."""
    return [{"type": "text", "text": system_text,
             "cache_control": {"type": "ephemeral"}}]

def _extract_cache_usage(usage) -> tuple[int, int, int]:
    """Return (uncached_input, cache_read, cache_write) from a usage object.

    Providers expose this differently:
      OpenAI: usage.prompt_tokens_details.cached_tokens (subset of prompt_tokens)
      Anthropic via OpenRouter: usage.cache_read_input_tokens + usage.cache_creation_input_tokens
                                (additive to prompt_tokens? or already inside it?
                                 OpenRouter reports prompt_tokens as the UNCACHED count.)
    We normalize to: input_tokens (billed at full rate) + cache_read + cache_write."""
    if usage is None:
        return 0, 0, 0
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    # Anthropic-style (additive to prompt_tokens on OpenRouter)
    cache_read  = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    # OpenAI-style: cached_tokens is a SUBSET of prompt_tokens
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached_subset = int(getattr(details, "cached_tokens", 0) or 0)
        if cached_subset and not cache_read:
            cache_read = cached_subset
            prompt_tokens = max(0, prompt_tokens - cached_subset)
    return prompt_tokens, cache_read, cache_write

# Image data URLs (base64-encoded PNGs from phone uploads) wreck token counting
# because tiktoken treats every base64 char as a token — a 200KB photo becomes
# ~280k "tokens" in the breakdown even though the LLM actually charges ~85-1k.
# We redact these to a marker before counting / before persisting snapshots.
import re as _img_re
_DATA_URL_RE = _img_re.compile(r'"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+"')

def _redact_images(obj):
    """Return a deep-copyish version of `obj` with base64 image URLs collapsed
    to placeholders. Works on dicts/lists/str returned by serializers."""
    try:
        s = json.dumps(obj)
    except Exception:
        return obj
    def _repl(m):
        size_kb = (len(m.group(0)) * 3 // 4) // 1024
        return f'"[image ~{size_kb}KB redacted for counting]"'
    redacted = _DATA_URL_RE.sub(_repl, s)
    if redacted == s:
        return obj
    try:
        return json.loads(redacted)
    except Exception:
        return obj

# Models that require the OpenAI Responses API instead of Chat Completions.
RESPONSES_API_MODELS = {"gpt-image-1"}

# Memory tools — ALWAYS sent (the user opted in to keep auto-naming, auto-rules,
# auto-facts, auto-topics, vocab learning, agent management). ~400 schema tokens.
# Agent management is here because users repeatedly asked Umbra to create
# cron agents and got told "I created it" without the schema actually being
# available — that's worse than the schema cost.
MEMORY_TOOL_NAMES = {"update_session", "add_rule", "add_fact",
                     "add_trait", "recall_trait_history",
                     "update_topic", "create_topic", "learn_word",
                     "remember_about_user",
                     "create_agent", "list_agents", "delete_agent",
                     "update_agent", "run_agent_now"}

# Other system tools — opt-in via the toolbar dropdown (shown there as
# selectable tools). Off by default to save tokens.
ADVANCED_SYSTEM_TOOL_NAMES = {"categorize_chat",
                              "switch_topic", "switch_model"}

# Legacy alias — preserved so older code paths reading SYSTEM_TOOL_NAMES still
# work. Includes the always-on set; the opt-in set is added per chat.
SYSTEM_TOOL_NAMES = MEMORY_TOOL_NAMES | ADVANCED_SYSTEM_TOOL_NAMES

# Tool groups — enabling one in the UI implicitly enables siblings so the user
# doesn't have to remember that listing files and running ls are different tools.
TOOL_IMPLICATIONS = {
    "shell":  {"list_dir", "read_file", "write_file"},
    "python": {"read_file", "write_file"},
}

# ─────────────────────────────────────────
#  Tiktoken counter
# ─────────────────────────────────────────
try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")
    def _count_tokens(text: str) -> int:
        return len(_enc.encode(text))
except Exception:
    def _count_tokens(text: str) -> int:
        return len(text) // 4

# ─────────────────────────────────────────
#  Related session lookup
# ─────────────────────────────────────────
def _find_related_sessions(user_message: str, current_sid: str | None, max_results: int = 2) -> list[str]:
    sessions_text = get_all_sessions()
    if not sessions_text or sessions_text == "(no sessions yet)":
        return []

    stopwords = {"that", "this", "with", "from", "have", "will", "want", "make",
                 "just", "some", "what", "when", "where", "your", "para", "quero",
                 "fazer", "voce", "como", "isso", "esse", "minha", "pode", "mais"}
    words = set(w.lower() for w in re.findall(r'\b\w{4,}\b', user_message)
                if w.lower() not in stopwords)

    if not words:
        return []

    scores = {}
    for line in sessions_text.splitlines():
        id_match = re.search(r'\[ID:(\d+)\]', line)
        if not id_match:
            continue
        sid = id_match.group(1)
        if sid == current_sid:
            continue
        score = sum(1 for w in words if w in line.lower())
        if score > 0:
            scores[sid] = score

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [sid for sid, _ in ranked[:max_results]]

# ─────────────────────────────────────────
#  Error formatting
# ─────────────────────────────────────────
def _format_llm_error(e: Exception) -> str:
    err = str(e)

    if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
        current = get_current()
        model = f"{current['provider']}/{current['model']}" if current else "current model"
        # Look for "retryDelay': '49s'" first (structured), fall back to plain
        # "retry ... 49s" but bounded so we don't slurp "49.627164096".
        retry_match = re.search(r"retryDelay['\"]?:\s*['\"](\d+)s", err)
        if not retry_match:
            retry_match = re.search(r'retry.{0,40}?\b(\d+)(?:\.\d+)?\s*s\b', err, re.I)
        retry = f" Retry in ~{retry_match.group(1)}s." if retry_match else ""
        return (
            f"⚠️ *Rate limit reached* on `{model}`\n"
            f"Free tier quota exhausted.{retry}\n\n"
            f"Switch model with: `/model fetch`"
        )
    if "404" in err or "not a valid model" in err.lower() or "No endpoints" in err:
        current = get_current()
        model = current['model'] if current else "unknown"
        return f"⚠️ *Model not found:* `{model}`\nUse `/model fetch` to list available models."
    if "401" in err or "invalid api key" in err.lower() or "authentication" in err.lower():
        return "⚠️ *Authentication failed* — check your API key in `.env`"
    if "500" in err or "502" in err or "503" in err:
        return "⚠️ *Provider server error* — try again in a moment."
    if "timeout" in err.lower():
        return "⚠️ *Request timed out* — the model took too long to respond."
    return f"⚠️ *LLM error:*\n`{err[:400]}`"


# ─────────────────────────────────────────
#  Stream collector
# ─────────────────────────────────────────
class _ToolCallAccum:
    """Accumulates a partial tool call as it streams in. OpenAI's spec sends
    the function name + id once and the arguments as appended JSON chunks."""

    def __init__(self):
        self.id = None
        self.type = "function"
        self.function_name = ""
        self.function_arguments = ""

    def update(self, tc_delta):
        if getattr(tc_delta, "id", None):
            self.id = tc_delta.id
        fn = getattr(tc_delta, "function", None)
        if fn is not None:
            if getattr(fn, "name", None):
                self.function_name = (self.function_name or "") + (fn.name or "")
            if getattr(fn, "arguments", None):
                self.function_arguments = (self.function_arguments or "") + fn.arguments

    def to_message_tool_call(self):
        # Build an object with the same shape as the non-streaming choice.message.tool_calls[i].
        class _ToolCall:
            pass
        class _Fn:
            pass
        tc = _ToolCall()
        tc.id = self.id
        tc.type = self.type
        tc.function = _Fn()
        tc.function.name = self.function_name
        tc.function.arguments = self.function_arguments or "{}"
        return tc


class _StreamedMessage:
    """A drop-in stand-in for choices[0].message with model_dump support."""

    def __init__(self):
        self.role = "assistant"
        self.content = ""
        self.tool_calls = None  # list[_ToolCall] or None

    def model_dump(self, exclude_none=True):
        d = {"role": self.role}
        if self.content:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                } for tc in self.tool_calls
            ]
        if exclude_none:
            return {k: v for k, v in d.items() if v is not None}
        return d


class _StreamedResponse:
    """Stand-in for the non-streaming ChatCompletion result."""

    def __init__(self, message, usage):
        class _Choice:
            pass
        c = _Choice()
        c.message = message
        self.choices = [c]
        self.usage = usage


def _collect_stream(stream_iter, on_text_delta, prompt_text_for_estimate: str = "",
                    cancel_check=None):
    """Walk the streamed iterator, surface each text delta via callback, and
    return a synthesized non-streaming-shaped response so the existing loop
    keeps working without further changes.

    If the provider doesn't emit usage data (Ollama, some Gemini wrappers),
    we fall back to a tiktoken estimate so the per-message token strip
    still shows something sensible instead of zero."""
    msg = _StreamedMessage()
    tool_accums: dict[int, _ToolCallAccum] = {}
    usage = None

    for chunk in stream_iter:
        if cancel_check is not None and cancel_check():
            try:
                stream_iter.close()
            except Exception:
                pass
            break
        # usage is sometimes only present on the final chunk
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue
        text = getattr(delta, "content", None)
        if text:
            msg.content += text
            try:
                on_text_delta(text)
            except Exception:
                pass
        tcs = getattr(delta, "tool_calls", None)
        if tcs:
            for tc in tcs:
                idx = getattr(tc, "index", 0) or 0
                accum = tool_accums.setdefault(idx, _ToolCallAccum())
                accum.update(tc)

    if tool_accums:
        msg.tool_calls = [tool_accums[i].to_message_tool_call() for i in sorted(tool_accums)]

    if usage is None:
        class _Usage:
            pass
        u = _Usage()
        if callable(prompt_text_for_estimate):
            prompt_text_for_estimate = prompt_text_for_estimate()   # built lazily
        u.prompt_tokens = _count_tokens(prompt_text_for_estimate) if prompt_text_for_estimate else 0
        u.completion_tokens = _count_tokens(msg.content)
        usage = u

    return _StreamedResponse(msg, usage)


_RECO_RE = re.compile(
    r"\b(recomend\w*|recommend\w*|sugest\w*|suggest\w*|sugere|sugira|indica(?:ç|c)?\w*|me indica|"
    r"o que (?:eu )?(?:jogo|assisto|leio|escuto)|what should i (?:play|watch|read|listen)|"
    r"(?:jogos?|filmes?|séries?|series|animes?|livros?|games?|movies?|shows?|books?) "
    r"(?:parecid\w*|tipo|like|similar|de|com|pra|para|sobre)|"
    r"(?:quais|que) (?:jogos?|games?|filmes?|séries?|animes?|livros?))",
    re.I)

# Pages a suggestion request must see whole: without them the model recommends
# something he already plays or likes ("research theorycrafting games").
_DOMAIN_PAGES = (
    (re.compile(r"\b(jogos?|games?|gameplay|steam|nintendo|playstation|xbox)\b", re.I), "games/games"),
    (re.compile(r"\b(animes?|mang[aá]s?)\b", re.I), "media/anime"),
    (re.compile(r"\b(filmes?|movies?|s[ée]ries?|shows?)\b", re.I), "media/movies"),
    (re.compile(r"\b(livros?|books?|quadrinhos?|comics?)\b", re.I), "media/books"),
)
_RESEARCH_RE = re.compile(
    r"\b(pesquis\w*|procur\w*|busc\w*|search\w*|look up|find me|"
    r"me (?:mostr\w*|d[áa]|ach\w*|indic\w*|sugir\w*|recomend\w*))\b", re.I)


def _domain_pages(message: str) -> list[str]:
    out: list[str] = []
    for rx, pid in _DOMAIN_PAGES:
        if pid not in out and rx.search(message or ""):
            out.append(pid)
    return out


_SHOW_RE = re.compile(
    r"(mostr\w*|manda|quero ver|show me|send me|let me see)\b.{0,40}\b(imagem|imagens|foto|fotos|figura|picture|pictures|image|images|photo|photos|pic)\b"
    r"|\b(imagem|foto|picture|image|photo) (de|do|da|of)\b",
    re.I)


# "Ok, gerando…" / "vou mostrar uma imagem…" — the model said it would act, then ended its turn
_PROMISE_RE = re.compile(
    r"(ok,? gerando|gerando:|generating:|\bvou (gerar|mostrar|buscar|procurar|pesquisar|criar|fazer|desenhar|refazer|"
    r"rodar|checar|verificar|conferir|olhar|listar|escanear|ficar de olho)|"
    r"^\W*(fazendo|rodando) (uma|um|a|o) |\b(checando|verificando|conferindo|escaneando) agora|volto com|"
    r"agora vou|já vou|regenerando|aqui vai|i'?ll (now )?(generate|show|search|look up|create|make|draw|check|run|scan)|"
    r"let me (generate|show|search|find|draw))",
    re.I | re.M)


_TOOL_CAP = 12000


def _cap_tool_output(text: str) -> str:
    """A command that dumps a huge output (e.g. grep -R over a folder with libraries) would be re-read by the
    model on every following step and turn — keep the head and the tail and say what was cut."""
    if len(text) <= _TOOL_CAP:
        return text
    head, tail = text[:8000], text[-3000:]
    return (head + f"\n\n…[{len(text) - 11000:,} characters cut — the output was too long. Narrow the command "
            f"(grep -m 20, head, exclude venv/backups/logs) if you need the middle.]…\n\n" + tail)


_TABLE_RE = re.compile(
    r"\b(lista\w*|list|liste|tabela|table|planilha|spreadsheet|compar\w*|diferen[cç]as?\s+entre|difference\s+between|ranking|rank|top\s?\d+|os?\s+\d+\s+(mais|maiores|melhores)|"
    r"processos|processes|pre[cç]os|prices|specs?|especifica\w*|quanto\s+(cada|custa)|vs\.?|versus)\b",
    re.I)


# Casual chat (a joke, a reaction, a mood): small models answer even "KKKK olha isso" with advice, checklists and
# "se quiser, posso…" despite identity.md — a one-line reminder right next to the message fixes it.
_CASUAL_MARK = re.compile(r"(k{3,}|\bha(ha)+\b|\bmds\b|\bmano\b|\bvei\b|\bslk\b|\bpqp\b|\blol\b|\blmao\b|"
                          r"\bcaralho\b|\bpreguiça\b|\bcansad\w*|\bt[ée]dio\b|😂|🤣|😭)", re.I)
_TASK_WORDS = re.compile(
    r"\b(erro|error|bug|script|python|kotlin|c[oó]digo|code|comando|command|install\w*|instal\w*|config\w*|server|"
    r"servidor|adb|build|apk|compil\w*|deploy|porta|port|ip|wifi|rede|faz|faça|fa[cç]a|cria|crie|manda|mostra|lista|"
    r"roda|rode|abre|abra|procura|busca|pesquisa|me\s+lembr\w*|lembra|agenda|calcula|traduz|explica|resume|escreve|"
    r"show|make|create|find|search|install|run|open|remind|translate|explain|write|como\s+(eu\s+)?(fa[cç]o|resolvo)|"
    r"how\s+(do|to|can)|por\s*que|why)\b", re.I)


# He's excited about a release/announcement: react first, and look it up instead of describing what you haven't seen.
_NEWS_RE = re.compile(r"\b(lan[çc]ou|lan[çc]aram|saiu\s+(o|a|os|as)\b|anunciou|anunciaram|trailer|teaser|"
                      r"dropped|released|announced|revealed)\b", re.I)


def _no_long_dashes(text: str) -> str:
    """The long dash (— –) reads as "an AI wrote this", and he asked for it gone. Outside code: 10–20 → 10-20,
    a dash opening a line → "- ", any other one → a comma (or nothing right before punctuation)."""
    if not text or ("—" not in text and "–" not in text):
        return text
    parts = re.split(r"(```.*?```|`[^`\n]*`)", text, flags=re.S)
    for i in range(0, len(parts), 2):
        t = parts[i]
        t = re.sub(r"(\d)\s*[–—]\s*(\d)", r"\1-\2", t)
        t = re.sub(r"(?m)^(\s*)[—–]\s+", r"\1- ", t)
        t = re.sub(r"\s*[—–]+\s*(?=[.!?,;:)\n]|$)", "", t)
        t = re.sub(r"\s*[—–]+\s*", ", ", t)
        t = re.sub(r",\s*,", ",", t)
        parts[i] = t
    return "".join(parts)


def _is_casual(msg: str) -> bool:
    m = (msg or "").strip()
    if not m or len(m) > 400 or _TASK_WORDS.search(m):
        return False
    return bool(_CASUAL_MARK.search(m)) or (len(m) < 120 and "?" not in m)


def _reddit_mode(meta: dict) -> str:
    """auto (the model decides) | always | never — chat setting, else app default."""
    try:
        from store.manager import get_app_setting
        v = (meta or {}).get("reddit_mode") or get_app_setting("reddit_mode", "auto")
        return v if v in ("auto", "always", "never") else "auto"
    except Exception:
        return "auto"


def _search_direct(sid: str | None) -> bool:
    """Show the search engine's answer as the reply (skips one model call).
    Per chat (chat_meta.search_direct) → app default (app_settings) → off."""
    try:
        from store.manager import get_app_setting
        m = get_chat_meta(sid) if sid else {}
        v = m.get("search_direct")
        if v is None:
            v = get_app_setting("search_direct", False)
        return bool(v)
    except Exception:
        return False


def _smart_routing() -> bool:
    """App setting: also ask a small model to pick memory topics (slower, ~1 s)."""
    try:
        from store.manager import get_app_setting
        return bool(get_app_setting("smart_routing", False))
    except Exception:
        return False


def _ctx_window(provider: str, model: str) -> int:
    try:
        from store.prices import context_window
        return context_window(f"{provider}/{model}")
    except Exception:
        return 0


class UmbraAgent:
    def __init__(self):
        self.current_sid: str | None = None
        self.conversation: list[dict] = []
        self._send_callback    = None
        self._file_callback    = None
        self._status_callback  = None
        self._event_callback   = None
        self._debug = self._load_debug()
        self.session_input_tokens  = 0
        self.session_output_tokens = 0
        self._last_prompt_breakdown: dict = {}
        # per-chat model override — when set, takes precedence over global current model
        self.model_override: dict | None = None  # {"provider": str, "model": str}
        # What this session has already opened, kept across turns: the per-turn
        # counters die with a turn that hits the tool budget, and the next
        # "continue" then re-reads everything from scratch.
        self._reads_ledger: dict = {}
        self._ledger_sid: str | None = None

    def _load_debug(self) -> bool:
        CONFIG.read(os.path.join(BASE_DIR, "config.ini"))
        return CONFIG.getboolean("general", "debug", fallback=False)

    def set_send_callback(self, fn):   self._send_callback   = fn
    def set_file_callback(self, fn):   self._file_callback   = fn
    def set_status_callback(self, fn): self._status_callback = fn
    def set_event_callback(self, fn):  self._event_callback  = fn

    def set_model_override(self, provider: str | None, model: str | None):
        if provider and model:
            self.model_override = {"provider": provider, "model": model}
        else:
            self.model_override = None

    def bind_session(self, sid: str | None):
        """Bind to a session id and rebuild the recent conversation from the
        stored history, so a chat keeps its context after a server restart."""
        self.current_sid = sid
        self.conversation = []
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self._last_prompt_breakdown = {}
        if sid:
            try:
                self.conversation = msg_store.conversation_for_model(
                    sid, max_messages=HISTORY_MAX_MESSAGES, max_chars=HISTORY_MAX_CHARS)
            except Exception as e:
                logger.warning(f"history rebuild failed for {sid}: {e}")
            try:
                from store.manager import get_token_usage
                t = (get_token_usage(sid) or {}).get("totals") or {}
                self.session_input_tokens = int(t.get("input", 0) or 0)
                self.session_output_tokens = int(t.get("output", 0) or 0)
            except Exception:
                pass

    # cooperative cancel: set from another thread; checked between stream
    # chunks, tool calls and loop iterations; the shell tool kills its process
    def request_cancel(self):
        self._cancel_flag = True

    def _cancelled(self) -> bool:
        return bool(getattr(self, "_cancel_flag", False))

    def _store(self, role: str, text: str, **extra) -> dict | None:
        """Write one message to the structured history and announce it."""
        if not self.current_sid:
            return None
        # A turn that died mid-stream leaves an empty assistant row: no text,
        # no picture and no turn details. Storing it only shows the user a
        # blank bubble in the chat.
        if role == "assistant" and not (text or "").strip() \
                and not extra.get("media") and not extra.get("turn"):
            return None
        rec = {"role": role, "text": text or ""}
        rec.update({k: v for k, v in extra.items() if v is not None})
        try:
            rec = msg_store.append(self.current_sid, rec)
        except Exception as e:
            logger.warning(f"message store append failed: {e}")
            return None
        self._emit("message", record=rec)
        return rec

    def _send(self, text: str):
        if self._send_callback:
            self._send_callback(text)

    def _drop_invented_images(self, text: str) -> str:
        """Markdown pictures must come from find_images (or the user's own message)."""
        if not text or "![" not in text:
            return text
        ok = set((self._turn or {}).get("found_urls") or set())
        said = (self._turn or {}).get("user_text") or ""
        for t in (self._turn or {}).get("tools") or []:
            said += " " + str(t.get("result") or "")

        def keep(m):
            u = m.group(2)
            return m.group(0) if (u in ok or u in said) else ""
        out = re.sub(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)", keep, text)
        return re.sub(r"\n{3,}", "\n\n", out)

    def _image_refs(self, how: str | None) -> list[str]:
        """Pictures generate_image should work from (see its `reference` arg)."""
        how = (how or "").strip().lower()
        how = {"attached": "edit", "last_result": "edit", "previous": "original"}.get(how, how)
        if how == "none":
            return []
        exts = ("png", "jpg", "jpeg", "webp", "gif", "bmp")
        media_dir = os.path.join(BASE_DIR, "temp", "media")
        is_img = lambda p: p.rsplit(".", 1)[-1].lower() in exts
        cur = [p for p in (self._turn.get("user_media") or []) if is_img(p) and os.path.exists(p)]
        if how in ("", "auto", "edit") and cur:
            return cur
        if how in ("", "auto"):
            return []
        try:
            items = msg_store.list_messages(self.current_sid)[-20:] if self.current_sid else []
        except Exception:
            items = []
        # "attached" with nothing attached → the newest picture in the chat (his or yours)
        role = "user" if how == "original" else None
        for r in reversed(items):
            if role and r.get("role") != role:
                continue
            got = [os.path.join(media_dir, n) for n in (r.get("media") or []) if is_img(n)]
            got = [g for g in got if os.path.exists(g)]
            if got:
                return got[-4:]
        return []

    def _send_file(self, path: str):
        # Copy into temp/media under a unique name (served by /api/media/<name>)
        # and remember it on this turn so the stored reply carries the file.
        name = os.path.basename(path)
        try:
            media_dir = os.path.join(BASE_DIR, "temp", "media")
            os.makedirs(media_dir, exist_ok=True)
            ext = (path.rsplit(".", 1)[-1] if "." in path else "bin")[:6].lower()
            name = uuid.uuid4().hex + "." + ext
            shutil.copy2(path, os.path.join(media_dir, name))
        except Exception as e:
            logger.warning(f"media copy failed for {path}: {e}")
        turn = getattr(self, "_turn", None)
        if turn is not None:
            turn.setdefault("media", []).append(name)
        self._emit("media_added", name=name, url=f"/api/media/{name}")
        if self.current_sid and getattr(self, "_this_turn_keep", True):
            try:
                append_session_log(self.current_sid, "umbra", f"[MEDIA: {name}]")
            except Exception:
                pass
        if self._file_callback:
            self._file_callback(path)

    def _set_status(self, text: str):
        if self._status_callback:
            self._status_callback(text)

    # Debug-level tagging for the new memory/router/prompt events. The client
    # uses _level to filter at min/normal/max. Existing event types stay at
    # the "normal" default to preserve current UI behavior.
    _EVENT_LEVEL = {
        # MIN: always shown
        "status":          "min",
        "user_message":    "min",
        "assistant_delta": "min",
        "final":           "min",
        "done":            "min",
        "error":           "min",
        "cancelled":       "min",
        "side_message":    "min",
        "tool_progress":   "min",
        "file_sent":       "min",
        "media_url":       "min",
        "model_changed":   "min",
        # NORMAL: shown at normal+
        "llm_request":     "normal",
        "llm_response":    "normal",
        "llm_error":       "min",   # always show; this is the diagnostic the user needs
        "tool_start":      "normal",
        "tool_end":        "normal",
        "session_created": "normal",
        "session_updated": "normal",
        "rule_added":      "normal",
        "user_fact_added": "normal",
        "chat_categorized":"normal",
        "turn.start":      "normal",
        "turn.end":        "normal",
        "tools.filter":    "normal",
        # MAX: only at maximum verbosity
        "prompt.assembled":   "max",
        "mem.memory:read":    "max",
        "mem.memory:write":   "max",
        "mem.memory:write_skipped": "max",
        "mem.router:start":         "max",
        "mem.router:keyword_scan":  "max",
        "mem.router:llm_fallback":  "max",
        "mem.router:result":        "max",
        "mem.session:created":      "max",
        "mem.session:updated":      "max",
        "mem.session:deleted":      "max",
        "mem.session:log_append":   "max",
        "mem.chat_meta:updated":    "max",
    }

    # Events that are too noisy / too granular to persist (live-only via WS).
    # `final` is the canonical assistant text which is already in the session
    # log; `user_message` likewise; `done`/`cancelled` are transient turn
    # signals. Skipping them keeps the replay clean of duplicates.
    _EVENT_PERSIST_SKIP = frozenset({
        "assistant_delta", "tool_progress", "status", "pong",
        "user_message", "final", "done", "cancelled", "message", "media_added", "search_progress",
    })

    # Events that become a line in the reply's "what happened" details.
    _MEMORY_EVENTS = {
        "rule_added", "user_fact_added", "user_fact_added_topic_created",
        "trait_added", "chat_categorized", "session_created", "model_changed",
        "topic_switch_suggested", "mem.memory:write",
    }

    _DEFAULT_REASONS = {
        "turn.start":     "user turn arrived — starting agent loop",
        "turn.end":       "agent finished all tool loops and produced final reply",
        "turn:end":       "responses-API path finished",
        "turn:plan":      "computed per-turn context (sid, model, layer manifest)",
        "status":         "human-readable status update for UI header",
        "prompt.assembled": "final prompt segments concatenated + token-counted",
        "tools.filter":   "filtered tool schema down to the ones the model needs this turn",
        "llm_request":    "sending prompt to model provider",
        "llm_response":   "model returned a full response (usage + tool calls)",
        "llm_error":      "model call failed",
        "assistant_delta": "streaming text chunk from the model",
        "tool_start":     "invoking tool with args",
        "tool_end":       "tool returned a result",
        "session_created": "created a new session record",
        "session_updated": "updated session status/summary",
        "topic_switch_suggested": "agent proposes switching to a different topic pack",
        "model_changed":  "agent switched to a different model via switch_model tool",
        "rule_added":     "user-defined behavior rule appended to core/rules.txt",
        "user_fact_added": "personal state saved via remember_about_user",
        "user_fact_added_topic_created": "remember_about_user created a new topic pack",
        "trait_added":    "stable user trait saved via add_trait",
        "chat_categorized": "agent tagged/grouped the current chat via categorize_chat",
        "final":          "final user-facing text emitted",
    }

    # Plain-English titles for the debug view. Every event gets a human-readable
    # `title` alongside its raw `type` so the app can show "Wrote down a user
    # fact" instead of `user_fact_added`. Keep phrases short and jargon-free —
    # this is what shows up as the entry header in the debug list.
    _TITLES = {
        # Turn lifecycle
        "turn.start":                  "Started handling your message",
        "turn.end":                    "Finished this reply",
        "turn:end":                    "Finished this reply",
        "turn:plan":                   "Planned what to load for this turn",
        "prompt.assembled":            "Built the prompt for the model",
        "tools.filter":                "Chose which tools the model could use",
        # Model I/O
        "llm_request":                 "Asked the model",
        "llm_response":                "Model replied",
        "llm_error":                   "Model call failed",
        "status":                      "Status update",
        "assistant_delta":             "Streamed a piece of the reply",
        # Tools
        "tool_start":                  "Ran a tool",
        "tool_end":                    "Tool finished",
        # Session-level
        "session_created":             "Started a new chat",
        "session_updated":             "Renamed / re-tagged the chat",
        "topic_switch_suggested":      "Suggested switching topic",
        "model_changed":               "Switched to a different model",
        "chat_categorized":            "Tagged / grouped the chat",
        "final":                       "Sent the reply to you",
        # Memory writes
        "rule_added":                  "Saved a permanent rule",
        "user_fact_added":             "Wrote down a user fact",
        "user_fact_added_topic_created": "Wrote a user fact (started a new topic file)",
        "trait_added":                 "Saved a stable trait about you",
        # Memory-layer events (mem.*)
        "mem.memory:read":             "Read a memory file",
        "mem.memory:write":            "Updated a memory file",
        "mem.memory:write_skipped":    "Skipped writing (already there)",
        "mem.router:start":            "Deciding which topic files to load",
        "mem.router:keyword_scan":     "Scanned message for topic keywords",
        "mem.router:llm_fallback":     "Asked a small model to pick topics",
        "mem.router:result":           "Topic files chosen",
        "mem.router:topic:start":      "Deciding which topic files to load",
        "mem.router:topic:keyword_scan": "Scanned message for topic keywords",
        "mem.router:topic:llm_fallback": "Asked a small model to pick topics",
        "mem.router:topic:result":     "Topic files chosen",
        "mem.router:trait:start":      "Deciding which trait files to load",
        "mem.router:trait:keyword_scan": "Scanned message for trait keywords",
        "mem.router:trait:llm_fallback": "Asked a small model to pick traits",
        "mem.router:trait:result":     "Trait files chosen",
        "mem.session:created":         "New session record created",
        "mem.session:updated":         "Session record updated",
        "mem.session:deleted":         "Session deleted",
        "mem.session:log_append":     "Wrote a line to the chat log",
        "mem.chat_meta:updated":       "Chat settings updated",
        "mem.summary.refresh:start":   "Refreshing this chat's short summary",
        "mem.summary.refresh:done":    "Chat summary updated",
        "mem.summary.refresh:noop":    "Summary unchanged",
        "mem.summary.refresh:error":   "Summary refresh failed",
        # Trait extractor
        "trait_extractor:start":       "Checking if you shared a stable trait",
        "trait_extractor:result":      "Trait check done",
        "trait_extractor:skip":        "Skipped trait check",
        "trait_extractor:error":       "Trait check failed",
    }

    def _emit(self, event_type: str, **data):
        level = self._EVENT_LEVEL.get(event_type, "normal")
        payload = dict(data)
        payload.setdefault("_level", level)
        if event_type in ("tool_start", "turn.start"):
            # plain sentence + the real budget, so the client stops hardcoding it
            if event_type == "tool_start":
                payload.setdefault("note", _tool_note(data.get("tool"), data.get("args") or {},
                                                      data.get("say") or ""))
                # `say` is already turned into `note` above; keep it out of the
                # args the app prints as the command/code, it is not an input.
                _a = payload.get("args")
                if isinstance(_a, dict) and "say" in _a:
                    payload["args"] = {k: v for k, v in _a.items() if k != "say"}
            payload.setdefault("max_loops", MAX_TOOL_LOOPS)
        # Always carry a human-readable reason so the xUmbra debug view can
        # show WHY every event fired. Call sites that pass reason=... win.
        if event_type in self._DEFAULT_REASONS:
            payload.setdefault("reason", self._DEFAULT_REASONS[event_type])
        # Plain-English title so the debug list can show a short, jargon-free
        # header (e.g. "Wrote down a user fact") instead of the raw event type
        # (e.g. `user_fact_added`). Every event gets one — with an ugly-but-
        # obvious fallback if we ever add an event and forget to name it.
        if event_type in self._TITLES:
            payload.setdefault("title", self._TITLES[event_type])
        else:
            payload.setdefault("title", event_type.replace(":", " · ").replace(".", " · "))
        # Guarantee `reason` is always present. If nothing filled it in above,
        # reuse the title so the debug view never has an empty "why" line.
        payload.setdefault("reason", payload["title"])
        # Tag every event with the current turn index so the client can group
        # replayed events under the right user message.
        if event_type == "turn.start":
            self._turn_idx = getattr(self, "_turn_idx", 0) + 1
            # Reset per-turn elapsed baseline so _dt_ms measures time inside
            # this turn only, not idle time between turns.
            self._last_emit_ts = None
        payload.setdefault("_turn_idx", getattr(self, "_turn_idx", 0))
        # Wall-clock ms since the previous event on this agent. Useful when
        # events are close together in a turn (spots real stalls), but a
        # background thread emitting an event after minutes/hours of idle
        # time between turns produces an inflated number that is not the
        # duration of any operation. Suppress those obvious artifacts:
        #   • > 60_000 ms  → not any real single-step operation on this Pi
        #   • different turn than the previous event → don't bridge turns
        _now = time.time()
        _last = getattr(self, "_last_emit_ts", None)
        _last_turn = getattr(self, "_last_emit_turn", None)
        this_turn = getattr(self, "_turn_idx", 0)
        if _last is not None and _last_turn == this_turn:
            gap_ms = int((_now - _last) * 1000)
            if gap_ms <= 60_000:
                # New name: _wall_gap_ms — describes what it actually is
                # (time since previous event, not operation duration).
                # _dt_ms kept as an alias for older client versions.
                payload.setdefault("_wall_gap_ms", gap_ms)
                payload.setdefault("_dt_ms", gap_ms)
        self._last_emit_ts = _now
        self._last_emit_turn = this_turn
        turn = getattr(self, "_turn", None)
        if turn is not None:
            try:
                if event_type == "tool_start":
                    turn["tools"].append({"name": data.get("tool"), "args": data.get("args"),
                                          "call_id": data.get("call_id"), "t0": _now,
                                          "text": data.get("text") or None,
                                          "note": data.get("note") or payload.get("note")
                                                  or _tool_note(data.get("tool"), data.get("args") or {},
                                                                data.get("say") or "")})
                elif event_type == "tool_end":
                    for t in reversed(turn["tools"]):
                        if t.get("call_id") == data.get("call_id") and "result" not in t:
                            t["result"] = str(data.get("result", ""))[:2500 if t.get("name") == "web_search" else 600]
                            t["ms"] = int((_now - t.pop("t0", _now)) * 1000)
                            break
                elif event_type in self._MEMORY_EVENTS:
                    detail = (data.get("file") or data.get("rule") or data.get("text")
                              or data.get("content_preview") or data.get("summary") or "")
                    turn["memory"].append({"title": payload.get("title"),
                                           "detail": str(detail)[:200]})
            except Exception:
                pass
        if self._event_callback:
            try:
                self._event_callback({"type": event_type, "data": payload})
            except Exception as e:
                logger.warning(f"event callback error: {e}")
        # Append to per-session events log so the client can replay debug
        # bubbles when the chat is reopened.
        if event_type not in self._EVENT_PERSIST_SKIP and self.current_sid:
            try:
                events_dir = os.path.join(BASE_DIR, "sessions", "events")
                os.makedirs(events_dir, exist_ok=True)
                path = os.path.join(events_dir, f"{self.current_sid}.jsonl")
                line = json.dumps({
                    "type": event_type,
                    "data": payload,
                    "ts":   time.time(),
                }, default=str, ensure_ascii=False)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as e:
                logger.warning(f"event persist failed: {e}")

    # ─────────────────────────────────────
    #  System prompt builder with breakdown — layered, on-demand memory
    # ─────────────────────────────────────
    def _build_system_prompt(self, user_message: str = "",
                             include: set | None = None) -> str:
        """Assemble the system prompt from layered memory.

        Order (each becomes one breakdown segment):
            system_prompt          — global identity (always)
            chat_sys_primary       — per-chat override (optional)
            chat_sys_secondary     — appended chat-only rules (optional)
            rules                  — user-defined behavior rules (always)
            system_info            — IPs, paths, hardware, credentials (always)
            topic:<name>           — routed topic packs (0..N, on demand)
            trait:<name>           — routed trait packs (0..N, on demand)
            summary                — rolling session summary (when present)
            summary_rules          — meta-instructions for the agent (always)

        `include` lets callers (CLI test-runs) suppress sections; default = all.
        """
        def want(name: str) -> bool:
            return include is None or name in include

        self._last_prompt_breakdown = {}
        self._last_prompt_segments = {}
        self._last_router_trace = {}
        self._last_trait_router_trace = {}
        parts = []

        def add(key: str, content: str):
            if not content:
                return
            parts.append(content)
            self._last_prompt_breakdown[key] = _count_tokens(content)
            self._last_prompt_segments[key]  = content

        # 2. Per-chat settings + persona options (a "standalone" persona replaces Umbra's own instructions)
        chat_meta = get_chat_meta(self.current_sid) if self.current_sid else {}
        _popts = {}
        try:
            if (chat_meta.get("persona") or "").strip():
                from agent import personas as _pm
                _popts = (_pm.get_persona(chat_meta["persona"].strip()) or {}).get("options") or {}
        except Exception:
            _popts = {}
        self._persona_opts = _popts

        # 1. Umbra's instruction files (memory/core/*.md, in ORDER.txt order)
        if want("system_prompt") and not _popts.get("standalone"):
            from store.manager import core_files
            for _n, _t in core_files():
                add(f"core:{_n[:-3]}", _t + "\n")
        # Proactive-mode flag — exposed to the model so it knows whether it's
        # allowed to schedule a future create_agent that calls back to this
        # chat. Always emitted (cheap), value defaults to disabled.
        if want("proactive_mode"):
            sid_str = self.current_sid or "(no session)"
            if chat_meta.get("allow_proactive"):
                proactive_body = (
                    f"This chat's session_id is: {sid_str}\n"
                    f"You ARE allowed to schedule yourself to message Maicon later "
                    f"via agents(op='create', action.type='prompt', action.push=true, "
                    f"action.session_id='{sid_str}'). "
                    f"Use ONLY when he mentions a concrete future event you should "
                    f"follow up on (dose change, deadline, retorno, plano). "
                    f"Pass the prompt as instructions to your future self, e.g. "
                    f"\"Maicon ia aumentar a dose de GHK-Cu hoje. Cumprimenta "
                    f"ele e pergunta se já aumentou.\" "
                    f"The future-self prompt should make YOU (the future agent) "
                    f"speak to Maicon — not just produce a plan. "
                    f"Pick cron for one-shot future events, interval for recurring."
                )
                mode = "enabled"
            else:
                proactive_body = (
                    "You MUST NOT create agents with push=true for this chat. "
                    "If Maicon asks why, explain proactive mode is off for this chat "
                    "and tell him to toggle it in the chat menu if he wants."
                )
                mode = "disabled"
            add("proactive_mode",
                f"\n## Proactive Mode: {mode}\n{proactive_body}\n")
        if want("chat_sys_primary"):
            # Persona takes precedence over a raw sys_primary string. If both are
            # set, the persona defines identity and sys_primary is appended below.
            persona_name = (chat_meta.get("persona") or "").strip()
            persona_body = ""
            if persona_name:
                from agent.personas import load_persona_for_chat
                persona_body = (load_persona_for_chat(persona_name) or "").strip()
            sp = (chat_meta.get("sys_primary") or "").strip()
            if persona_body:
                add(f"persona:{persona_name}", persona_body)
                if sp:
                    add("chat_sys_primary",
                        f"\n## Chat-specific Identity (in addition to persona)\n{sp}")
            elif sp:
                add("chat_sys_primary", f"\n## Chat-specific Identity\n{sp}")
        if want("chat_sys_secondary"):
            ss = (chat_meta.get("sys_secondary") or "").strip()
            if ss:
                add("chat_sys_secondary", f"\n## Chat-specific Rules\n{ss}")

        # 3. The notebook (memory/notebook.py): who Maicon is and how to talk to
        #    him (always), the map of every page, then the sections this message
        #    is about — matched instantly by titles/nicknames/words, no AI call.
        #    Stable parts first so the cached prompt prefix survives.
        _notes_text = ""
        _private = (_popts.get("notebook") or "") if str(_popts.get("notebook") or "").startswith("private-") else ""
        if _private:
            try:
                from store import notebook as NB
                _pt = NB.private_text(_private)
                add("private_notes", f"\n## Your files ({_private})\n" + (_pt or "(empty — nothing filed yet)") + "\n")
            except Exception as e:
                logger.warning(f"private notebook failed: {e}")
        if _popts.get("standalone"):
            pass
        elif want("rules") or want("traits") or want("topics"):
            try:
                from store import notebook as NB
                about, nb_rules = NB.always_text()
                old_rules = load_rules() or ""          # legacy core/rules.txt, still honoured
                if about:
                    add("about_me", f"\n## About Maicon\n{about}\n")
                if nb_rules or old_rules.strip():
                    add("rules", "\n## How to talk to Maicon (permanent rules)\n"
                        + "\n".join(x for x in (nb_rules, old_rules.strip()) if x) + "\n")
                _dev = NB.devices_text()
                if _dev:
                    add("devices", "\n## Devices (his machines)\n" + _dev)
            except Exception as e:
                logger.warning(f"notebook (about/rules) failed: {e}")
        if want("facts") and not _popts.get("standalone"):
            facts = load_facts()
            if facts:
                add("system_info",
                    f"\n## System Info (IPs, paths, hardware, credentials)\n{facts}")
        if (want("traits") or want("topics")) and not _popts.get("standalone"):
            try:
                from store import notebook as NB
                mp = NB.map_text()
                if mp:
                    add("notebook_map", "\n## Your memory files about Maicon (page: sections)\n" + mp + "\n"
                        "Sections relevant to this message are shown below under 'From your memory'. If the message "
                        "touches another section, open it with memory(op=read, page, section) before answering — "
                        "e.g. before recommending a title that has its own section, check what he thought of it.\n")
                _recent = [(m.get("content") if isinstance(m.get("content"), str) else "")
                           for m in self.conversation[-6:] if m.get("role") == "user"]
                _msg = user_message or ""
                _whole = bool(_RECO_RE.search(_msg))
                # A suggestion/research request ("research theorycrafting games")
                # gets the whole page for that domain, even without a keyword hit:
                # otherwise it recommends something he already plays.
                _intent = _whole or bool(_RESEARCH_RE.search(_msg))
                _force = _domain_pages(_msg) if _intent else []
                _notes_text, _picked = NB.context_for(_msg, _recent[-3:-1], whole_pages=_whole,
                                                      force=_force)
                self._last_router_trace = {"path": "notebook", "picked": _picked}
                self._emit("mem.notebook", picked=_picked)
            except Exception as e:
                logger.warning(f"notebook (map/notes) failed: {e}")
        if _notes_text:
            add("notes", "\n## From your memory (relevant to this message)\n" + _notes_text)

        # 5. Rolling session summary
        if want("summary") and self.current_sid:
            summary = get_summary(self.current_sid)
            if summary:
                add("summary",
                    f"\n## Session [ID:{self.current_sid}] Rolling Summary\n{summary}")

        # 5b. The date — without it the model assumed its training year
        #     ("recent horror movies 2023-2025" in 2026). Kept near the end so
        #     the cached prompt prefix only changes once a day.
        import datetime as _dt
        _now = _dt.datetime.now()
        add("date", f"\n## Now\n{_now.strftime('%A, %d %B %Y')} (Brazil). "
                    "Use this for anything relative ('recent', 'this year', 'next week').\n")

        # 5b'. How he types (laugh, length, abbreviations), learned from his own messages, so replies sound like
        #      someone texting him back (store/texting_style.py). Not for standalone personas.
        if not (getattr(self, "_persona_opts", None) or {}).get("standalone"):
            try:
                from store import texting_style as _ts
                _prof = _ts.profile()
                if _prof:
                    add("texting_style", _prof)
            except Exception:
                pass

        # 5c. Recommendation requests: small models (gpt-5-mini on minimal
        #     effort) skip the rule in the core prompt, so say it right here.
        if _SHOW_RE.search(user_message or "") and not _RECO_RE.search(user_message or "") \
                and not re.search(r"\b(gera|gere|faz|faça|cria|crie|desenh\w*|draw|generate|create|make)\b", user_message or "", re.I):
            if getattr(self, "_turn", None) is not None:
                self._turn["force_tool"] = "find_images"
            add("show_hint", "\n## This message wants to SEE something real\n"
                "Use find_images (kind=photo for an action/object/animal/food or what a word means, query in English; "
                "kind=movie/game/any otherwise) and show the best one or two as ![caption](url). "
                "Do it in this reply, don't ask first. Use generate_image only if Maicon asks you to create or draw it.\n")
        if _TABLE_RE.search(user_message or ""):
            add("table_hint", "\n## This message asks for a list / data\n"
                "If the answer is several items that each have details (numbers, sizes, prices, dates, specs), put them in a "
                "Markdown table, `| Item | Detail (unit) |` with a `|---|---:|` line, numbers right-aligned, sorted by what "
                "matters. Never paste raw command output or space-aligned columns. One short line of takeaway is enough.\n")
        if (_is_casual(user_message) and not (chat_meta.get("persona") or "").strip()
                and not _RECO_RE.search(user_message or "") and not _TABLE_RE.search(user_message or "")
                and not _SHOW_RE.search(user_message or "")):
            add("casual_hint", "\n## This message is casual chat\n"
                "Reply like a friend texting back: 1-3 short lines, react to what he said. No advice, safety tips, "
                "lists or how-to unless he asked; don't end with an offer (\"se quiser…\", \"quer que eu…\"). "
                "If he's telling you how he feels (tired, lazy, bored, down), don't fix it, no tips (water, nap, "
                "stretching…); react like a friend would, maybe one light question. Say it in your own words, "
                "don't reuse wording from your instructions. "
                "Use \"você\"/\"vc\", never \"tu/teu/tua\". No swearing (no pqp, caralho, porra, merda…), and don't "
                "claim you saw or watched something you didn't.\n")
        if _NEWS_RE.search(user_message or "") and not (chat_meta.get("persona") or "").strip():
            add("news_hint", "\n## He's telling you news he's excited about\n"
                "React to HIS excitement first (short, his energy). You haven't seen it: never say you saw/watched it and "
                "never describe its content from imagination, web_search it first if you want to talk about details, "
                "then mention one or two real ones.\n")
        if _RECO_RE.search(user_message or ""):
            add("reco_hint", "\n## This message asks for recommendations\n"
                "Before answering: 1) web_search(query, reddit=true) for current, well-liked picks; "
                "2) find_images with all the names you will recommend (kind=game for games); "
                "3) answer with a **bold** name heading per pick, its ![name](url) picture on its own line, "
                "and 2-3 short lines on why it fits Maicon.\n")

        # 5d. Last line of the prompt = what a small model weighs most: don't trail off into offers.
        if not (getattr(self, "_persona_opts", None) or {}).get("standalone"):
            add("closing", "\n## Before you answer\nAfter using ANY tool (memory, search, etc.), you MUST still answer the user's latest question directly — never reply with only a tool confirmation like \"Anotado\" or \"Pronto\". The tool result is background work; your visible reply must contain the actual answer. Finish when the answer is finished, no closing offer "
                "(\"se quiser…\", \"quer que eu…\", \"posso também…\"), no menu of next steps. No swearing. Don't open with \"Boa pergunta\" / \"Ótima pergunta\" / \"Claro!\". Never use the long dash (\u2014 or \u2013): commas, periods or parentheses instead. "
                "Today is the date in \"## Now\", don't guess the weekday.\n")

        # 6. Meta-instructions


        return "\n".join(parts)

    # ─────────────────────────────────────
    #  Main chat entry point
    # ─────────────────────────────────────
    def chat(self, *args, **kwargs) -> str:
        """Public entry — wraps the real turn in try/finally so the memory
        manager's global event emitter (set below on `M.set_emitter(...)`)
        never outlives the turn. Without this, any later `get_summary(sid)` /
        `load_topic(...)` call from unrelated code paths (e.g. an HTTP handler
        serving the Android app) would piggyback its `memory:read` events onto
        this chat's event log and inflate the debug view."""
        try:
            return self._chat_turn(*args, **kwargs)
        finally:
            try:
                M.set_emitter(None)
            except Exception:
                pass

    def _extend_tool_budget(self, limit: int) -> int:
        """Return the tool-call limit for the next round of this turn.

        When the budget ran out mid-task the turn ended with "I hit my tool
        budget, tell me continue", so every long job needed a manual nudge.
        Extending in place keeps the same turn alive — its ledger, tool
        results and partial reply all stay.
        """
        used = int(self._turn.get("budget_extensions") or 0)
        if used >= MAX_BUDGET_EXTENSIONS or self._cancelled():
            return limit
        self._turn["budget_extensions"] = used + 1
        self._turn["budget_warned"] = False
        self.conversation.append({"role": "user", "content":
            "[system] Your tool budget for this turn ran out and was extended "
            "automatically. Keep going from where you are: use what you already "
            "read, do not re-read files, and finish the task."})
        try:
            self._emit("status", text="Tool budget extended (second round)")
        except Exception:
            pass
        return limit + MAX_TOOL_LOOPS

    def _chat_turn(self, user_message: str,
             include: set | None = None,
             keep_in_memory: bool = True,
             media_paths: list | None = None,
             selected_tools: list | None = None,
             store_user: bool = True) -> str:
        """Run one chat turn.

        include:        optional set of system-prompt section keys to send (None = all).
        keep_in_memory: when False, this turn doesn't write to memory.txt or
                        session log, and won't auto-create a session.
        """
        self._debug = self._load_debug()
        self._this_turn_keep = keep_in_memory
        self._this_turn_include = include
        self._turn_started_at = time.time()
        self._cancel_flag = False
        self._turn = {"tools": [], "memory": [], "media": [], "calls": {},
                       "reads": {}, "reads_total": 0, "writes_total": 0, "read_nudged": {}}
        try:
            from store import usage as _uctx
            _uctx.set_context(self.current_sid)
        except Exception:
            pass
        try:
            from agent import tools as _tm
            _tm.set_cancel_check(self._cancelled)
        except Exception:
            pass

        # Let tools that want chat_meta context (e.g. generate_image reading
        # default_image_model) know which session is asking this turn.
        try:
            from agent import tools as _tools_mod
            _tools_mod.set_current_sid(self.current_sid)
        except Exception:
            pass

        # Route all memory-layer events through this agent's _emit so the app's
        # WS stream sees memory reads, writes, and router decisions.
        M.set_emitter(lambda typ, data: self._emit(f"mem.{typ}", **data))

        self._emit("turn.start",
                   sid=self.current_sid,
                   max_loops=MAX_TOOL_LOOPS,
                   user_message_preview=user_message[:200],
                   user_message_tokens=_count_tokens(user_message))

        self._turn["user_media"] = list(media_paths or [])
        self._turn["user_text"] = user_message or ""
        # store_user is False only when this turn resumes a message that a
        # restart already wrote to the history: writing it again would show
        # the same user bubble twice.
        if store_user:
            self._store("user", user_message,
                        media=[os.path.basename(p) for p in (media_paths or [])] or None,
                        ephemeral=True if not keep_in_memory else None,
                        client_id=getattr(self, "turn_client_id", None),
                        source=getattr(self, "turn_source", None) or "telegram")

        if store_user and self.current_sid and keep_in_memory:
            append_session_log(self.current_sid, "user", user_message)
            # Mark uploaded images in the log so reopening the chat re-renders
            # them above/below the user message (the app reads `[MEDIA: ...]`
            # markers from the session log and turns them into bubble images).
            if media_paths:
                for p in media_paths:
                    append_session_log(self.current_sid, "user",
                                       f"[MEDIA: {os.path.basename(p)}]")

        # A previous turn died on the tool budget: put its saved state in front
        # of this message so the model starts from what it already read instead
        # of re-opening the same files. Consumed once, then deleted.
        _pending = ""
        try:
            if self.current_sid:
                _pending = take_pending_state(self.current_sid)
        except Exception:
            logger.warning("pending state restore failed", exc_info=True)

        if media_paths:
            content = [{"type": "text", "text": user_message}]
            for path in media_paths:
                try:
                    with open(path, "rb") as f:
                        raw = f.read()
                    b64 = base64.b64encode(raw).decode()
                    ext = path.rsplit(".", 1)[-1].lower()
                    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                            "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
                    content.append({"type": "image_url",
                                    "image_url": {"url": f"data:{mime};base64,{b64}"}})
                except Exception as e:
                    logger.warning(f"media attach failed for {path}: {e}")
            if _pending:
                content[0]["text"] = _pending + "\n\n" + user_message
            self.conversation.append({"role": "user", "content": content})
        else:
            _text = (_pending + "\n\n" + user_message) if _pending else user_message
            self.conversation.append({"role": "user", "content": _text})
        self._compact_history()

        self._emit("status", text="Preparing request")

        try:
            # Persona-declared model sits between an explicit user override
            # (highest precedence) and the global default (lowest). This lets
            # e.g. the FBI persona pin a fast/cheap model without preventing
            # the user from forcing a different one via /model in that chat.
            persona_model = None
            try:
                pmeta = get_chat_meta(self.current_sid) if self.current_sid else {}
                pname = (pmeta.get("persona") or "").strip()
                if pname:
                    from agent import personas as personas_mod
                    p = personas_mod.get_persona(pname) or {}
                    raw = (p.get("model") or "").strip()
                    if "/" in raw:
                        prov, mid = raw.split("/", 1)
                        persona_model = (prov.strip(), mid.strip())
            except Exception as _e:
                logger.debug(f"persona-model lookup failed: {_e}")
                persona_model = None

            if self.model_override:
                self._turn_provider = self.model_override["provider"]
                client, model = get_client(
                    self.model_override["provider"], self.model_override["model"]
                )
            elif persona_model:
                self._turn_provider = persona_model[0]
                client, model = get_client(persona_model[0], persona_model[1])
            else:
                _cur = get_current()
                self._turn_provider = (_cur or {}).get("provider") or \
                    CONFIG.get("general", "default_provider", fallback="openrouter")
                client, model = get_client()
        except ConnectionError as e:
            from agent.providers import log_ollama_failure, get_history
            log_ollama_failure(str(e))
            self._store("error", "Could not connect to Ollama. Pick another model for this chat.")
            history = get_history()
            hist_text = "\n".join(f"• `[{h['provider']}] {h['model']}`" for h in history[:5])
            return (
                f"⚠️ *Could not connect to Ollama.*\n\n"
                f"*Recently used models:*\n{hist_text or '(none)'}\n\n"
                f"Switch with: `/model <provider> <model>`"
            )

        system = self._build_system_prompt(user_message, include=self._this_turn_include)

        # Conversation token estimate. We redact image base64 first — otherwise
        # a single phone photo balloons to "millions of tokens" in the breakdown
        # and the prompt inspector crashed trying to render multi-MB strings.
        # The image itself still goes to the LLM via the unredacted self.conversation
        # below; this only affects accounting + the saved snapshot.
        _redacted_conv = _redact_images(self.conversation)
        conv_tokens = _count_tokens(json.dumps(_redacted_conv))
        # Add a per-image surrogate (~800 tokens per image is the OpenAI
        # high-detail ballpark; the actual provider does its own accounting).
        n_images = json.dumps(self.conversation).count("data:image/")
        if n_images:
            conv_tokens += 800 * n_images
        self._last_prompt_breakdown["conversation"] = conv_tokens
        if n_images:
            self._last_prompt_breakdown["images"] = 800 * n_images

        # ── Narrative turn:plan event — explains, in plain text, what the
        # agent is about to do this turn so the debug stream tells a story
        # instead of just listing events.
        try:
            _layers = ", ".join(
                f"{k}({v}t)" for k, v in self._last_prompt_breakdown.items()
                if k not in ("conversation", "images") and v > 0
            ) or "(none)"
            _router_loaded = list((self._last_router_trace or {}).get("loaded") or [])
            _router_scores = (self._last_router_trace or {}).get("scores") or {}
            _why_topics = ""
            if _router_loaded:
                hits = [f"{t}={_router_scores.get(t, '?')}" for t in _router_loaded]
                _why_topics = f" Router loaded topic(s) [{', '.join(_router_loaded)}] (scores: {', '.join(hits)})."
            elif _router_scores:
                _why_topics = " Router matched no topic above threshold."
            _preview = (user_message or "").replace("\n", " ")[:120]
            self._emit("turn:plan",
                       sid=self.current_sid,
                       user_preview=_preview,
                       layers=_layers,
                       router_loaded=_router_loaded,
                       router_scores=_router_scores,
                       narrative=(
                           f"User said: \"{_preview}\". "
                           f"Layers: {_layers}.{_why_topics}"
                       ))
        except Exception as e:
            logger.warning(f"turn:plan emit failed: {e}")

        # Snapshot the conversation length at turn start so turn:end can tell
        # us how many new tool calls / assistant turns happened.
        _conv_len_at_start = len(self.conversation)

        msg_input_tokens  = 0
        msg_output_tokens = 0
        turn_cache_read_total  = 0
        turn_cache_write_total = 0
        turn_uncached_in_total = 0

        # Build tool list for this call.
        # Priority:
        #   1) selected_tools explicitly passed (UI per-turn override)
        #   2) chat_meta.enabled_tools (per-chat persistent default)
        #   3) [] = no non-system tools (matches user's "empty default" preference)
        chat_meta_for_tools = get_chat_meta(self.current_sid) if self.current_sid else {}
        # Full-fidelity debug is always on — the client (xUmbra) manages its own
        # show/hide state for how much of it to render inline; the server always
        # captures everything into sessions/events/*.jsonl so nothing is lost.
        _full_debug = True
        if selected_tools is None:
            persisted = chat_meta_for_tools.get("enabled_tools")
            if persisted is None:
                persisted = []  # empty default per user preference
            effective_tools = list(persisted)
        else:
            effective_tools = list(selected_tools)

        # Tools: `memory` + `load_tools` always; everything else is loaded on
        # demand by the model (load_tools) and stays loaded for the chat, plus
        # whatever the user ticked / the persona declares, plus `agents` when
        # the chat allows reminders.
        loaded = set(effective_tools) | set(chat_meta_for_tools.get("loaded_tools") or [])
        if chat_meta_for_tools.get("allow_proactive"):
            loaded.add("agents")
        self._loaded_tools = toolset.normalize(loaded)
        api_tools = toolset.build(self._loaded_tools)
        all_tool_names = list(toolset.LOADABLE)
        skipped_tool_names = [n for n in all_tool_names if n not in self._loaded_tools]
        effective_tools = sorted(self._loaded_tools)

        # Token cost of the tool schema we're actually sending vs the full set.
        sent_schema_json = json.dumps(api_tools) if api_tools else ""
        full_schema_json = json.dumps(toolset.build(set(toolset.LOADABLE)))
        tools_tokens_sent  = _count_tokens(sent_schema_json)
        tools_tokens_full  = _count_tokens(full_schema_json)
        self._last_prompt_breakdown["tools_schema"] = tools_tokens_sent
        self._last_prompt_segments["tools_schema"]  = (
            json.dumps(api_tools, indent=2) if api_tools else "(no tools sent)"
        )
        self._last_prompt_breakdown["total_estimated"] = sum(
            v for k, v in self._last_prompt_breakdown.items()
            if k != "total_estimated"
        )

        # Detailed debug stream of what the prompt looks like this turn.
        _prompt_extra = {}
        if _full_debug:
            _prompt_extra["segments"] = dict(self._last_prompt_segments)
        self._emit("prompt.assembled",
                   sid=self.current_sid,
                   breakdown=dict(self._last_prompt_breakdown),
                   segments_present=list(self._last_prompt_segments.keys()),
                   router=dict(getattr(self, "_last_router_trace", {})),
                   trait_router=dict(getattr(self, "_last_trait_router_trace", {})),
                   total_tokens=self._last_prompt_breakdown.get("total_estimated", 0),
                   **_prompt_extra)
        self._emit("tools.filter",
                   enabled=sorted(set(effective_tools)),
                   system_tools=["memory", "load_tools"],
                   skipped=skipped_tool_names,
                   tokens_sent=tools_tokens_sent,
                   tokens_if_all=tools_tokens_full,
                   tokens_saved=max(0, tools_tokens_full - tools_tokens_sent))

        # Route to Responses API for models that require it (e.g. gpt-image-1)
        raw_model_name = model.split("/")[-1].lower() if "/" in model else model.lower()
        if raw_model_name in RESPONSES_API_MODELS:
            final_response, msg_input_tokens, msg_output_tokens = \
                self._chat_via_responses_api(client, model, system)
        else:
            loop_count = 0
            final_response = ""
            _call_limit = MAX_TOOL_LOOPS

            while True:
                if loop_count >= _call_limit:
                    _call_limit = self._extend_tool_budget(_call_limit)
                    if loop_count < _call_limit:
                        continue
                    break
                if self._cancelled():
                    break
                loop_count += 1
                # Budget awareness: warn the model when it is nearing the tool limit
                _remaining = _call_limit - loop_count
                if _remaining == 5 and not self._turn.get("budget_warned"):
                    self._turn["budget_warned"] = True
                    self.conversation.append({"role": "user", "content":
                        f"[system] You have {_remaining} tool calls left before hitting the limit. "
                        "If you have not answered the user yet, either answer now with what you know, "
                        "or briefly state what is missing and ask whether to continue investigating."})
                    self._emit("status", text="Budget warning injected (5 calls left)")
                # Code was changed and never built: say so before the model
                # writes "done" purely out of optimism.
                if self._should_nudge_verify():
                    self._turn["verify_nudged"] = True
                    self.conversation.append({"role": "user", "content":
                        "[system] You changed code in this turn and have not built or tested it yet. "
                        "Run the project's build/test (run tool) and read the result before telling "
                        "Maicon it works. If it cannot be run here, say exactly what you changed and "
                        "that you did not verify it."})
                    self._emit("status", text="Unverified edit: asking for a build")
                api_tools = toolset.build(self._loaded_tools)
                _req_extra = {}
                if _full_debug:
                    _req_extra["system_text"] = system
                    _req_extra["messages"] = list(self.conversation)
                    _req_extra["tools_schema"] = api_tools or []
                self._emit("llm_request",
                           model=model,
                           loop=loop_count,
                           prompt_breakdown=dict(self._last_prompt_breakdown),
                           **_req_extra)

                try:
                    # Stream the response so the app can render tokens as they
                    # arrive. _collect_stream returns a synthesized non-streaming
                    # response so the rest of the loop is unchanged.
                    # Only needed when the provider reports no usage; building
                    # it eagerly serialized the whole history on every round.
                    prompt_estimate_text = lambda: system + json.dumps(self.conversation)
                    # Choose system-message shape based on whether explicit
                    # cache_control is needed for this provider+model.
                    _prov = getattr(self, "_turn_provider", "") or ""
                    _cache_mode = _model_caching_status(_prov, model)
                    if _caching_enabled() and _cache_mode == "explicit" and system:
                        system_msg_content = _wrap_system_for_cache(system)
                    else:
                        system_msg_content = system
                    base_kwargs = dict(
                        model=model,
                        messages=[{"role": "system", "content": system_msg_content}] + self.conversation,
                        max_completion_tokens=_max_output_tokens(),
                    )
                    # Reasoning models (gpt-5*, o1/o3/o4-*) default to
                    # medium reasoning effort which burns hundreds of hidden
                    # tokens per turn — a 10s wait even on trivial replies.
                    # "low" is still capable but drops the wait to ~2-3s.
                    _raw = model.split("/")[-1].lower() if "/" in model else model.lower()
                    if _raw.startswith("gpt-5") or _raw.startswith(("o1", "o3", "o4")):
                        # Precedence: chat_meta.reasoning_effort (user picked
                        # in the app) > persona.reasoning_effort (author's
                        # baseline) > "low" default. All three keep the raw
                        # value verbatim if it's in the allowed set.
                        _valid = ("minimal", "low", "medium", "high")
                        _effort = ""
                        try:
                            pmeta = get_chat_meta(self.current_sid) if self.current_sid else {}
                            _chat_effort = (pmeta.get("reasoning_effort") or "").strip().lower()
                            if _chat_effort in _valid:
                                _effort = _chat_effort
                            if not _effort:
                                pname = (pmeta.get("persona") or "").strip()
                                if pname:
                                    from agent import personas as personas_mod
                                    p = personas_mod.get_persona(pname) or {}
                                    _persona_effort = (p.get("reasoning_effort") or "").strip().lower()
                                    if _persona_effort in _valid:
                                        _effort = _persona_effort
                        except Exception:
                            _effort = ""
                        # "minimal" answers in ~1 s; "low" took 2 s and spiked to 17 s
                        base_kwargs["reasoning_effort"] = _effort or "minimal"
                    if api_tools:
                        base_kwargs["tools"] = api_tools
                        base_kwargs["tool_choice"] = "auto"
                        _force = (self._turn or {}).get("force_tool")
                        if loop_count == 1 and _force and any(t["function"]["name"] == _force for t in api_tools):
                            # small models announce "here's a picture" and invent a URL instead of calling the tool
                            base_kwargs["tool_choice"] = {"type": "function", "function": {"name": _force}}
                    if _prov == "openrouter":
                        base_kwargs["extra_body"] = {"usage": {"include": True}}
                    stream_kwargs = {**base_kwargs, "stream": True}
                    try:
                        stream_iter = client.chat.completions.create(
                            **stream_kwargs,
                            stream_options={"include_usage": True},
                        )
                    except Exception:
                        # Some providers reject stream_options.
                        stream_iter = client.chat.completions.create(**stream_kwargs)

                    try:
                        response = _collect_stream(
                            stream_iter,
                            on_text_delta=lambda piece: self._emit("assistant_delta", text=piece),
                            prompt_text_for_estimate=prompt_estimate_text,
                            cancel_check=self._cancelled,
                        )
                    except Exception:
                        # If streaming itself blows up partway, fall back to a
                        # normal non-streaming call so the user still gets an answer.
                        response = client.chat.completions.create(**base_kwargs)
                except Exception as e:
                    logger.error(f"LLM error: {e}")
                    err_str = str(e)
                    # Surface the failure as a visible system bubble at every
                    # debug level (min/normal/max). Previously this was logged
                    # silently and the user got back a generic reply text with
                    # no debug breadcrumb showing the actual API error.
                    self._emit("llm_error",
                               model=model,
                               loop=loop_count,
                               error_type=type(e).__name__,
                               error=err_str[:1200])
                    if "not a valid model" in err_str or "404" in err_str:
                        from agent.providers import _save_used, _load_used
                        used = _load_used()
                        used["current"] = None
                        _save_used(used)
                    _err_text = _format_llm_error(e)
                    self._store("error", _err_text.replace("*", "").replace("`", ""),
                                turn={"provider": getattr(self, "_turn_provider", ""),
                                      "model": model, "error": err_str[:1200]})
                    return _err_text

                usage = getattr(response, "usage", None)
                if usage:
                    msg_out = getattr(usage, "completion_tokens", 0)
                    _uncached_in, _cache_read, _cache_write = _extract_cache_usage(usage)
                    # For session/message totals we count BILLED input tokens
                    # (uncached + cache_read + cache_write). The cost split
                    # happens in usage.record_turn().
                    msg_in_billed = _uncached_in + _cache_read + _cache_write
                    self._turn["context_tokens"] = int(msg_in_billed) + int(msg_out or 0)
                    msg_input_tokens  += msg_in_billed
                    msg_output_tokens += msg_out
                    self.session_input_tokens  += msg_in_billed
                    self.session_output_tokens += msg_out
                    turn_cache_read_total  += _cache_read
                    turn_cache_write_total += _cache_write
                    turn_uncached_in_total += _uncached_in
                    _c = getattr(usage, "cost", None)
                    if _c is None:
                        _c = (getattr(usage, "model_extra", None) or {}).get("cost")
                    if _c is None:
                        self._turn["cost_exact"] = False
                    else:
                        self._turn["actual_cost"] = self._turn.get("actual_cost", 0.0) + float(_c)
                    self._emit("llm_response",
                               model=model,
                               input_tokens=msg_in_billed,
                               output_tokens=msg_out,
                               cache_read_tokens=_cache_read,
                               cache_write_tokens=_cache_write)

                message = response.choices[0].message
                try:
                    # what the model actually answered — for the debug screen
                    self._emit("llm_output", model=model, loop=loop_count,
                               text=(message.content or "")[:1500],
                               tool_calls=[{"name": tc.function.name, "args": (tc.function.arguments or "")[:600]}
                                           for tc in (message.tool_calls or [])])
                except Exception:
                    pass
                if self._cancelled():
                    # half-streamed tool calls would leave the history invalid
                    message.tool_calls = None
                self.conversation.append(message.model_dump(exclude_none=True))

                if not message.tool_calls or not api_tools or self._cancelled():
                    _txt = message.content or ""
                    if (api_tools and not self._cancelled() and not self._turn.get("nudged")
                            and not self._turn.get("tools") and len(_txt.strip()) < 300
                            and _PROMISE_RE.search(_txt[-200:])
                            and not _txt.rstrip().endswith("?")):
                        # small models announce the action and stop; ask once for the call itself
                        self._turn["nudged"] = True
                        self.conversation.append({"role": "user", "content":
                            "[system] You said you'd do it but didn't call the tool. Call it now — no text first."})
                        self._turn["nudge_at"] = len(self.conversation) - 1
                        self._emit("status", text="Calling the tool it promised…")
                        continue
                    final_response = _txt
                    break

                # Fast path: the model already wrote its reply and only saved
                # something to memory alongside it → run the saves, skip the
                # second model call (it would just repeat the same reply).
                _calls = list(message.tool_calls)
                _fast_mem = (bool((message.content or "").strip()) and all(
                    tc.function.name == "memory" and '"trait_history"' not in (tc.function.arguments or "")
                    for tc in _calls))

                # The model often narrates what it is about to do and then calls
                # tools ("Fix 3 is the state note, looking into it"). That text
                # used to be stored as an interim assistant message, which landed
                # in the chat as a loose bubble past the tool round. It belongs
                # to the process, so it now rides on the first tool call of the
                # round and shows up inside that step.
                _narration = (message.content or "").strip() if not _fast_mem else ""
                _narr = [_narration]

                def _take_narr() -> str | None:
                    if _narr and _narr[0]:
                        _v = _narr[0]
                        _narr[0] = ""
                        return _v
                    return None

                if _fast_mem:
                    _res = []
                    for tc in _calls:
                        try:
                            _a = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            _a = {}
                        self._emit("tool_start", tool="memory", args=_a, call_id=tc.id,
                                   say=str(_a.get("say") or ""), text=_take_narr())
                        _r = self._handle_tool("memory", _a)
                        self._emit("tool_end", tool="memory", call_id=tc.id, result=str(_r)[:2000])
                        _res.append({"role": "tool", "tool_call_id": tc.id, "content": str(_r)})
                    self.conversation.extend(_res)
                    final_response = message.content
                    break

                tool_results = []
                _all_blocked = True   # flips if any call in this round really ran
                for tc in message.tool_calls:
                    if self._cancelled():
                        _all_blocked = False
                        tool_results.append({"role": "tool", "tool_call_id": tc.id,
                                             "content": "cancelled by the user"})
                        continue
                    name = tc.function.name
                    _raw_args = tc.function.arguments or ""
                    _bad_json = False
                    try:
                        args = json.loads(_raw_args)
                        if not isinstance(args, dict):
                            args, _bad_json = {}, True
                    except json.JSONDecodeError:
                        args, _bad_json = {}, True
                    if _bad_json:
                        _all_blocked = False
                        self._emit("tool_start", tool=name, args=args, call_id=tc.id,
                                   say=str(args.get("say") or ""), text=_take_narr())
                        result = ("ERROR: the arguments for '%s' did not arrive as valid JSON "
                                  "(the call was probably truncated). Nothing ran. Retry with "
                                  "the arguments split smaller." % name)
                        self._emit("tool_end", tool=name, call_id=tc.id, result=result)
                        tool_results.append({"role": "tool", "tool_call_id": tc.id,
                                             "content": result})
                        continue
                    self._key = _args_key(name, args)
                    _seen = (self._turn.get("calls") or {}).get(self._key, 0)
                    self._emit("tool_start", tool=name, args=args, call_id=tc.id,
                                   say=str(args.get("say") or ""), text=_take_narr())
                    if _seen >= 2:
                        # identical call, third time: the model is looping, stop paying for it
                        _all_blocked = False
                        result = ("SKIPPED: you already ran this exact call %d times this turn and the result "
                                  "is above. Do not retry it, use what you have or change approach."
                                  % _seen)
                        self._emit("tool_end", tool=name, call_id=tc.id, result=result)
                        tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                        continue
                    self._turn.setdefault("calls", {})
                    self._turn["calls"][self._key] = _seen + 1
                    _blocked = self._read_guard(name, args)
                    if _blocked:
                        self._emit("tool_end", tool=name, call_id=tc.id, result=_blocked)
                        tool_results.append({"role": "tool", "tool_call_id": tc.id, "content": _blocked})
                        continue
                    _all_blocked = False
                    result = self._safe_tool(name, args)
                    # generate_image stashes provider/model in a module-level
                    # sidecar so we can caption the resulting image bubble.
                    _img_meta = None
                    if name == "generate_image":
                        try:
                            from agent.tools import pop_last_image_meta
                            _img_meta = pop_last_image_meta()
                        except Exception:
                            _img_meta = None
                    _end_kwargs = dict(tool=name, call_id=tc.id,
                                       result=str(result)[:2000])
                    if _img_meta:
                        _end_kwargs["image"] = _img_meta
                        self._turn["image_cost"] = self._turn.get("image_cost", 0.0) + float(_img_meta.get("cost") or 0); self._turn["image_model"] = _img_meta.get("model"); self._turn["image_note"] = _img_meta.get("note") or self._turn.get("image_note")
                    self._emit("tool_end", **_end_kwargs)
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _cap_tool_output(str(result))
                    })

                self.conversation.extend(tool_results)

                if _all_blocked and tool_results and self._refund_blocked_round():
                    loop_count -= 1
                    self._emit("status", text="Tool round refunded: every call was refused by a guard")

                if (len(message.tool_calls) == 1 and message.tool_calls[0].function.name == "web_search"
                        and _search_direct(self.current_sid) and self._turn.get("last_search")):
                    raw = self._turn["last_search"]
                    if not raw.startswith("Web search failed"):
                        # show the search engine's own answer; no second model call
                        final_response = re.sub(r"^\[web search via [^\]]+\]\n", "", raw).strip()
                        self.conversation.append({"role": "assistant", "content": final_response})
                        self._turn["direct_search"] = True
                        break

            if not final_response:
                if self._cancelled():
                    final_response = ""
                else:
                    final_response = _limit_message()
                    try:
                        self._budget_state_note()   # persists the note for the next turn
                    except Exception:
                        logger.warning("budget state note failed", exc_info=True)

        if self.current_sid and self._this_turn_keep:
            append_session_log(self.current_sid, "umbra", final_response)

        prov_model = f"{getattr(self, '_turn_provider', '') or 'unknown'}/{model}"
        sid_str    = f"ID:{self.current_sid}" if self.current_sid else "no session"

        # session summary for footer
        session_summary = ""
        if self.current_sid:
            raw = get_session_summary(self.current_sid)
            if raw:
                # strip the [ID:XXXX] [date] [STATUS] prefix, keep just the summary text
                parts = raw.split("] ", 3)
                session_summary = parts[-1].strip() if len(parts) >= 4 else raw

        # log token usage — only add a per-message record when the turn is kept
        # in memory (so the record count stays in sync with the session log).
        from store.manager import log_token_usage
        _elapsed = time.time() - getattr(self, "_turn_started_at", time.time())
        log_token_usage(
            sid=self.current_sid or "no_session",
            model=prov_model,
            msg_input=msg_input_tokens,
            msg_output=msg_output_tokens,
            session_input=self.session_input_tokens,
            session_output=self.session_output_tokens,
            prompt_breakdown=self._last_prompt_breakdown,
            elapsed_sec=_elapsed,
            log_message=bool(self.current_sid and self._this_turn_keep),
        )

        # Per-turn cost record (cache-aware). Skipped for the Responses-API
        # path (gpt-image-1 etc.) which doesn't surface usage we can split.
        try:
            from store import usage as _usage_mod
            _provider, _model_id = (prov_model.split("/", 1) + [""])[:2]
            self._turn["cost"] = _usage_mod.record_turn(
                sid=self.current_sid,
                provider=_provider,
                model=_model_id,
                input_tokens=turn_uncached_in_total,
                output_tokens=msg_output_tokens,
                cache_read_tokens=turn_cache_read_total,
                cache_write_tokens=turn_cache_write_total,
                elapsed_sec=_elapsed,
                actual_cost=(self._turn.get("actual_cost")
                             if self._turn.get("cost_exact", True) and "actual_cost" in self._turn else None),
            )
        except Exception as e:
            logger.warning(f"usage record failed: {e}")

        # Save what was actually sent (for in-app inspector). Images go in as
        # `[image ~XXKb redacted]` markers so the saved file stays tiny and the
        # app's prompt inspector doesn't crash trying to render multi-MB strings.
        try:
            save_prompt_snapshot(
                self.current_sid,
                getattr(self, "_last_prompt_segments", {}),
                _redact_images(self.conversation),
                prov_model,
                breakdown=dict(self._last_prompt_breakdown),
                router_trace=dict(getattr(self, "_last_router_trace", {})),
            )
        except Exception as e:
            logger.warning(f"prompt snapshot save failed: {e}")

        footer = (
            f"\n\n`─────────────────────`\n"
            f"`{sid_str}` · `{prov_model}`\n"
        )
        if session_summary:
            footer += f"_{session_summary}_\n"
        footer += (
            f"`msg ↑{msg_input_tokens:,} ↓{msg_output_tokens:,}` · "
            f"`session ↑{self.session_input_tokens:,} ↓{self.session_output_tokens:,} tokens`"
        )

        # ── Narrative turn:end — explains what the model actually did vs
        # what it could have done. Highlights skipped writes (e.g. summary
        # not updated) so the user sees the gaps.
        try:
            new_turns = self.conversation[_conv_len_at_start:]
            tool_calls_made: list[str] = []
            for m in new_turns:
                for tc in (m.get("tool_calls") or []):
                    fn = (tc.get("function") or {}).get("name") or tc.get("name") or "?"
                    tool_calls_made.append(fn)
            tools_str = ", ".join(tool_calls_made) or "(none)"
            updated_session = "update_session" in tool_calls_made
            updated_topic   = any(t in tool_calls_made for t in
                                  ("update_topic", "create_topic", "remember_about_user"))
            saved_fact      = "add_fact" in tool_calls_made
            saved_rule      = "add_rule" in tool_calls_made
            scheduled       = "create_agent" in tool_calls_made
            notes = []
            if not updated_session:
                notes.append("summary NOT updated")
            if not updated_topic and not saved_fact and not saved_rule:
                notes.append("nothing new saved to memory")
            if scheduled:
                notes.append("agent scheduled")
            self._emit("turn:end",
                       sid=self.current_sid,
                       tool_calls=tool_calls_made,
                       updated_session=updated_session,
                       updated_topic=updated_topic,
                       saved_fact=saved_fact,
                       saved_rule=saved_rule,
                       scheduled=scheduled,
                       narrative=(
                           f"Tool calls: {tools_str}. " +
                           (("Notes: " + "; ".join(notes) + ".") if notes else "")
                       ))
        except Exception as e:
            logger.warning(f"turn:end emit failed: {e}")

        final_response = self._drop_invented_images(final_response)
        final_response = _no_long_dashes(final_response)
        # customer-service openers he finds robotic ("Boa pergunta, …"): cut them if they slip through
        _cut = re.sub(r"^\s*(boa|ótima|otima|excelente|great|good)\s+pergunta\b[^\n.!?]*[.!?,:]?\s*", "", final_response, flags=re.I)
        if _cut != final_response and _cut:
            final_response = _cut[:1].upper() + _cut[1:]
        _na = self._turn.get("nudge_at")
        if _na is not None and _na < len(self.conversation) and \
                str((self.conversation[_na] or {}).get("content", "")).startswith("[system] You said you'd do it"):
            del self.conversation[_na]
        _tools_done = [{k: v for k, v in t.items() if k not in ("t0", "call_id")}
                       for t in self._turn["tools"]]
        _rec = self._store(
            "assistant", final_response,
            media=list(self._turn.get("media") or []) or None,
            ephemeral=True if not self._this_turn_keep else None,
            source=getattr(self, "turn_source", None) or "telegram",
            turn={
                "provider": getattr(self, "_turn_provider", ""),
                "model": model,
                "duration_sec": round(time.time() - self._turn_started_at, 2),
                "input_tokens": msg_input_tokens,
                "output_tokens": msg_output_tokens,
                "cache_read_tokens": turn_cache_read_total,
                "cache_write_tokens": turn_cache_write_total,
                "cost_usd": round(float((self._turn.get("cost") or {}).get("total_cost", 0) or 0)
                                  + float(self._turn.get("search_cost", 0) or 0)
                                  + float(self._turn.get("image_cost", 0) or 0), 6),
                "image_cost_usd": round(float(self._turn.get("image_cost", 0) or 0), 6) or None,
                "image_model": self._turn.get("image_model"),
                "image_note": self._turn.get("image_note"),
                "tools": _tools_done,
                "memory": list(self._turn["memory"]),
                "cancelled": True if self._cancelled() else None,
                "breakdown": dict(self._last_prompt_breakdown),
                "context_tokens": self._turn.get("context_tokens"),
                "direct_search": True if self._turn.get("direct_search") else None,
                "context_window": _ctx_window(getattr(self, "_turn_provider", ""), model),
            })
        self._emit("final",
                   message=_rec,
                   text=final_response,
                   session_id=self.current_sid,
                   model=prov_model,
                   msg_input_tokens=msg_input_tokens,
                   msg_output_tokens=msg_output_tokens,
                   session_input_tokens=self.session_input_tokens,
                   session_output_tokens=self.session_output_tokens,
                   cache_read_tokens=turn_cache_read_total,
                   cache_write_tokens=turn_cache_write_total,
                   prompt_breakdown=dict(self._last_prompt_breakdown))

        # Old tool outputs only matter for the turn that ran them: shrink them so later turns don't re-read them.
        for _m in self.conversation:
            if isinstance(_m, dict) and _m.get("role") == "tool" and len(str(_m.get("content") or "")) > 1500:
                _m["content"] = str(_m["content"])[:1200] + "\n…(trimmed after that reply)"

        # The note-taker updates the memory files from this exchange, in the background.
        if (user_message and getattr(self, "_this_turn_keep", True) and not self._cancelled()
                and (getattr(self, "_persona_opts", {}) or {}).get("note_taker", True)):
            _nsid, _nemit = self.current_sid, self._emit

            _nmid = (_rec or {}).get("id") if isinstance(locals().get("_rec"), dict) else None

            def _take_notes(u=user_message, r=final_response, mid=_nmid):
                try:
                    from agent import notetaker
                    from store import usage as _u
                    _u.set_context(_nsid)
                    from store import workers as W
                    W.register("note-taker", "Note-taker", "After each reply, reads what you said and writes anything "
                               "worth remembering into your memory files.", every=None, emoji="📝")
                    with W.run("note-taker") as wr:
                        done = notetaker.take_notes(u, r, sid=_nsid, emit=_nemit)
                        wr.note = f"saved {len(done)} thing(s)" if done else "nothing new"
                    if done and mid:
                        # show under the reply what was written to the memory files
                        rec = msg_store.update(_nsid, mid, notes=[{k: c.get(k) for k in ("op", "page", "section", "text", "old")}
                                                                  for c in done])
                        try:
                            from api.hub import HUB
                            if rec:
                                HUB.publish(_nsid, "message", {"sid": _nsid, "message": rec})
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(f"note-taker crashed: {e}")
            threading.Thread(target=_take_notes, daemon=True, name="notetaker").start()

        # Rolling summary update happens off the response path so the user
        # never waits for it. Skip when the model already invoked
        # update_session in this turn (it just wrote one explicitly).
        try:
            new_turns = self.conversation[_conv_len_at_start:]
            model_called_update = any(
                ((tc.get("function") or {}).get("name") == "update_session"
                 or tc.get("name") == "update_session")
                for m in new_turns
                for tc in (m.get("tool_calls") or [])
            )
            if self.current_sid and not model_called_update:
                summary_worker.schedule(
                    sid=self.current_sid,
                    user_message=user_message,
                    assistant_message=final_response,
                    emit=lambda typ, **data: self._emit(typ, **data),
                )
        except Exception as e:
            logger.warning(f"summary worker schedule failed: {e}")

        self._emit("turn.end",
                   sid=self.current_sid,
                   model=prov_model,
                   duration_sec=round(time.time() - getattr(self, "_turn_started_at", time.time()), 3),
                   msg_input_tokens=msg_input_tokens,
                   msg_output_tokens=msg_output_tokens,
                   session_input_tokens=self.session_input_tokens,
                   session_output_tokens=self.session_output_tokens,
                   router=dict(getattr(self, "_last_router_trace", {})),
                   breakdown=dict(self._last_prompt_breakdown))

        return final_response + footer

    # ─────────────────────────────────────
    #  Responses API path (gpt-image-1 etc.)
    # ─────────────────────────────────────
    def _chat_via_responses_api(self, client, model: str, system: str) -> tuple[str, int, int]:
        """Call the OpenAI Responses API for models that don't support chat/completions."""
        # Convert self.conversation to Responses API input format
        input_items: list = []
        for msg in self.conversation:
            role    = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "tool":
                input_items.append({
                    "type":    "function_call_output",
                    "call_id": msg.get("tool_call_id", "unknown"),
                    "output":  str(content),
                })
                continue

            if role == "assistant" and msg.get("tool_calls"):
                parts: list = []
                if content:
                    parts.append({"type": "output_text", "text": str(content)})
                for tc in msg.get("tool_calls", []):
                    fn = tc.get("function", {})
                    parts.append({
                        "type":      "function_call",
                        "call_id":   tc.get("id", ""),
                        "id":        tc.get("id", ""),
                        "name":      fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    })
                input_items.append({"role": "assistant", "content": parts})
                continue

            if isinstance(content, list):
                parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        key = "input_text" if role == "user" else "output_text"
                        parts.append({"type": key, "text": part.get("text", "")})
                    elif ptype == "image_url":
                        url = part.get("image_url", {}).get("url", "")
                        parts.append({"type": "input_image", "image_url": url})
                input_items.append({"role": role, "content": parts})
            else:
                key = "input_text" if role == "user" else "output_text"
                input_items.append({"role": role,
                                    "content": [{"type": key, "text": str(content)}]})

        # Convert TOOL_DEFINITIONS_COMPACT to Responses API format
        tools = []
        for t in TOOL_DEFINITIONS_COMPACT:
            fn = t.get("function", {})
            tools.append({
                "type":        "function",
                "name":        fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters":  fn.get("parameters", {}),
                "strict":      False,
            })

        msg_input  = 0
        msg_output = 0
        final_text = ""

        loop_count = 0
        _call_limit = MAX_TOOL_LOOPS
        while True:
            if loop_count >= _call_limit:
                _call_limit = self._extend_tool_budget(_call_limit)
                if loop_count < _call_limit:
                    continue
                break
            loop_count += 1
            self._emit("llm_request", model=model, loop=loop_count,
                       prompt_breakdown=dict(self._last_prompt_breakdown))
            try:
                resp = client.responses.create(
                    model=model,
                    instructions=system,
                    input=input_items,
                    tools=tools,
                    tool_choice="auto",
                )
            except Exception as e:
                logger.error(f"Responses API error: {e}")
                return _format_llm_error(e), 0, 0

            usage = getattr(resp, "usage", None)
            if usage:
                _in  = getattr(usage, "input_tokens",  0) or 0
                _out = getattr(usage, "output_tokens", 0) or 0
                msg_input  += _in
                msg_output += _out
                self.session_input_tokens  += _in
                self.session_output_tokens += _out
                self._emit("llm_response", model=model,
                           input_tokens=_in, output_tokens=_out)

            # Process output items
            tool_calls_found: list = []
            assistant_parts:  list = []
            for item in resp.output:
                itype = getattr(item, "type", None)

                if itype == "message":
                    for c in getattr(item, "content", []):
                        if getattr(c, "type", None) == "output_text":
                            delta = c.text or ""
                            if delta:
                                self._emit("assistant_delta", text=delta)
                                final_text += delta
                                assistant_parts.append({"type": "output_text", "text": delta})

                elif itype == "image_generation_call":
                    img_b64 = getattr(item, "result", None)
                    if img_b64:
                        try:
                            img_data = base64.b64decode(img_b64)
                            path = os.path.join(TEMP_DIR, f"img_{int(time.time())}.png")
                            with open(path, "wb") as f:
                                f.write(img_data)
                            self._send_file(path)
                        except Exception as e:
                            logger.warning(f"Responses API image save failed: {e}")

                elif itype == "function_call":
                    tool_calls_found.append(item)

            if not tool_calls_found:
                break

            # Narration written next to a tool call ("Fix 3 is the state note,
            # looking into it") reached the UI as a live bubble, then vanished:
            # the next llm_request wipes that bubble and only the final reply was
            # ever stored. It belongs to the process, so it now rides on the
            # first tool call of the round instead of becoming a loose message.
            _resp_narration = "".join(p.get("text") or "" for p in assistant_parts
                                      if p.get("type") == "output_text").strip()
            _resp_narr = [_resp_narration]

            def _resp_take() -> str | None:
                if _resp_narr and _resp_narr[0]:
                    _v = _resp_narr[0]
                    _resp_narr[0] = ""
                    return _v
                return None

            # Execute tool calls and build next input
            _all_blocked = True   # flips if any call in this round really ran
            for item in tool_calls_found:
                tc_name    = getattr(item, "name", "")
                tc_call_id = getattr(item, "call_id", "")
                _raw_args = getattr(item, "arguments", None) or "{}"
                try:
                    args = json.loads(_raw_args)
                    if not isinstance(args, dict):
                        raise ValueError("not an object")
                except (json.JSONDecodeError, ValueError):
                    _all_blocked = False
                    result = ("ERROR: the arguments for '%s' did not arrive as valid JSON "
                              "(the call was probably truncated). Nothing ran. Retry with "
                              "the arguments split smaller." % tc_name)
                    self._emit("tool_start", tool=tc_name, args={}, call_id=tc_call_id, say="", text=_resp_take())
                    self._emit("tool_end", tool=tc_name, call_id=tc_call_id, result=result)
                    input_items.append({"type": "function_call_output",
                                        "call_id": tc_call_id, "output": result})
                    assistant_parts.append({"type": "function_call", "call_id": tc_call_id,
                                            "id": tc_call_id, "name": tc_name,
                                            "arguments": _raw_args or "{}"})
                    continue
                self._key = _args_key(tc_name, args)
                _seen = (self._turn.get("calls") or {}).get(self._key, 0)
                self._emit("tool_start", tool=tc_name, args=args, call_id=tc_call_id,
                                       say=str(args.get("say") or ""), text=_resp_take())
                if _seen >= 2:
                    _all_blocked = False
                    result = ("SKIPPED: you already ran this exact call %d times this turn and the result "
                              "is above. Do not retry it, use what you have or change approach." % _seen)
                    self._emit("tool_end", tool=tc_name, call_id=tc_call_id, result=result)
                    input_items.append({"type": "function_call_output", "call_id": tc_call_id, "output": result})
                    assistant_parts.append({"type": "function_call", "call_id": tc_call_id, "id": tc_call_id,
                                            "name": tc_name, "arguments": getattr(item, "arguments", "{}") or "{}"})
                    continue
                self._turn.setdefault("calls", {})
                self._turn["calls"][self._key] = _seen + 1
                _blocked = self._read_guard(tc_name, args)
                if _blocked:
                    self._emit("tool_end", tool=tc_name, call_id=tc_call_id, result=_blocked)
                    input_items.append({"type": "function_call_output",
                                        "call_id": tc_call_id, "output": _blocked})
                    assistant_parts.append({"type": "function_call", "call_id": tc_call_id,
                                            "id": tc_call_id, "name": tc_name,
                                            "arguments": getattr(item, "arguments", "{}") or "{}"})
                    continue
                _all_blocked = False
                result = self._safe_tool(tc_name, args)
                _img_meta = None
                if tc_name == "generate_image":
                    try:
                        from agent.tools import pop_last_image_meta
                        _img_meta = pop_last_image_meta()
                    except Exception:
                        _img_meta = None
                _end_kwargs = dict(tool=tc_name, call_id=tc_call_id,
                                   result=str(result)[:2000])
                if _img_meta:
                    _end_kwargs["image"] = _img_meta
                    self._turn["image_cost"] = self._turn.get("image_cost", 0.0) + float(_img_meta.get("cost") or 0); self._turn["image_model"] = _img_meta.get("model"); self._turn["image_note"] = _img_meta.get("note") or self._turn.get("image_note")
                self._emit("tool_end", **_end_kwargs)
                assistant_parts.append({
                    "type":      "function_call",
                    "call_id":   tc_call_id,
                    "id":        tc_call_id,
                    "name":      tc_name,
                    "arguments": getattr(item, "arguments", "{}") or "{}",
                })
                input_items.append({
                    "type":    "function_call_output",
                    "call_id": tc_call_id,
                    "output":  str(result),
                })

            if assistant_parts:
                input_items.append({"role": "assistant", "content": assistant_parts})

            if _all_blocked and tool_calls_found and self._refund_blocked_round():
                loop_count -= 1
                self._emit("status", text="Tool round refunded: every call was refused by a guard")

            final_text = ""  # reset for next iteration

        if not final_text and loop_count >= _call_limit:
            final_text = _limit_message()
            # Same state note the chat-completions path writes: without it the
            # next "continue" starts re-reading the same files from scratch.
            try:
                self._budget_state_note()   # persists the note for the next turn
            except Exception:
                logger.warning("budget state note failed", exc_info=True)

        if final_text and loop_count < _call_limit:
            # Finished on its own: the ledger only bridges a budget cut, so
            # drop it instead of letting it poison the next unrelated task.
            self._reads_ledger = {}
            self._ledger_sid = None
            try:
                clear_read_ledger(self.current_sid)
            except Exception:
                pass

        # Store assistant turn in conversation history
        self.conversation.append({
            "role":    "assistant",
            "content": final_text or "(image generated)",
        })

        return final_text, msg_input, msg_output

    # ─────────────────────────────────────
    #  Tool handler
    # ─────────────────────────────────────
    # ── read ledger (survives across turns) ──────────────────────────────
    def _hydrate_ledger(self) -> None:
        """Pull this session's ledger into the turn counters."""
        sid = self.current_sid
        if not sid:
            return
        if self._ledger_sid != sid:
            self._reads_ledger = load_read_ledger(sid) or {}
            self._ledger_sid = sid
        led = self._reads_ledger or {}
        files = led.get("files") or {}
        if files:
            per_file = self._turn.setdefault("reads_per_file", {})
            for path, n in files.items():
                per_file[path] = max(int(per_file.get(path, 0)), int(n or 0))
        # Slice counters and the read total carry too, so the per-file cap and
        # the "you already read this slice" rule still hold on a "continue".
        slices = led.get("slices") or {}
        if slices:
            seen = self._turn.setdefault("reads", {})
            for key, n in slices.items():
                seen[key] = max(int(seen.get(key, 0)), int(n or 0))
        carried = int(led.get("total", 0) or 0)
        if carried:
            self._turn["reads_total"] = max(int(self._turn.get("reads_total", 0)), carried)
        written = int(led.get("writes", 0) or 0)
        if written:
            self._turn["writes_total"] = max(int(self._turn.get("writes_total", 0)), written)

    def _ledger_bump(self, path: str, rng: str) -> None:
        """Record one read of (file, slice) for the whole session."""
        try:
            led = self._reads_ledger
            led.setdefault("slices", {})
            key = path + "|" + rng
            led["slices"][key] = int(led["slices"].get(key, 0)) + 1
            led.setdefault("files", {})
            led["files"][path] = int(led["files"].get(path, 0)) + 1
            led["total"] = int(self._turn.get("reads_total", 0))
            if self.current_sid:
                save_read_ledger(self.current_sid, led)
        except Exception:
            logger.debug("read ledger bump failed", exc_info=True)

    def _persist_writes(self) -> None:
        """Keep the session's write count in the ledger, for the same reason the
        read counters live there."""
        try:
            self._reads_ledger["writes"] = int(self._turn.get("writes_total", 0))
            if self.current_sid:
                save_read_ledger(self.current_sid, self._reads_ledger)
        except Exception:
            logger.debug("write count persist failed", exc_info=True)

    def _ledger_recent(self) -> list:
        """Files touched this session, most read first (turn counters win)."""
        led = self._reads_ledger or {}
        merged = {k: int(v or 0) for k, v in (led.get("files") or {}).items()}
        for k, v in (self._turn.get("reads_per_file") or {}).items():
            merged[k] = max(merged.get(k, 0), int(v or 0))
        return sorted(merged.items(), key=lambda kv: -kv[1])

    def _budget_state_note(self) -> str:
        """One line written when a turn dies on the tool budget, so the next
        "continue" does not start by re-reading the same files from scratch."""
        files = self._ledger_recent()[:8]
        edits = int(self._turn.get("writes_total", 0))
        parts = [f"{os.path.basename(p)} x{n}" for p, n in files]
        note = ("[system] Previous turn was cut off by the tool budget. "
                f"Files already read (do not re-read them): {', '.join(parts) if parts else 'none'}. "
                f"Files changed: {edits}.")
        if not edits:
            note += (" Nothing was written yet, so start this turn with write_file using the "
                     "content already gathered, not with more exploration.")
        # Persist it where nothing rewrites it: the rolling summary is replaced
        # wholesale by the summary worker and the message store is filtered, so
        # the note has to live in its own file to survive a restart or a
        # "continue" in a new process.
        try:
            if self.current_sid:
                save_pending_state(self.current_sid, note)
        except Exception:
            logger.warning("pending state save failed", exc_info=True)
        return note

    def _is_write(self, name: str, args: dict) -> bool:
        if name in ("write_file", "file_write"):
            return True
        if name != "shell":
            return False
        cmd = str(args.get("command") or "")
        if not cmd:
            return False
        if re.search(r"\bsed\b[^|;]*\s-i|\bawk\b[^|;]*inplace|(?:^|[\s;|&])>+\s*[^\s&|]|\btee\b|\bsort\b[^|;]*\s-o\b", cmd):
            return True
        words = re.split(r"[\s|;&()<>]+", cmd)
        return any(w in _WRITE_VERBS for w in words if w)

    def _refund_blocked_round(self) -> bool:
        """A round whose every tool call was refused by the guard did no work at
        all, so charging it against the tool budget punishes the model for
        obeying the block. Hand the slot back, up to _MAX_REFUNDS, so a model
        stuck on blocked reads still has budget left to answer with what it has.
        The cap keeps a model that never gives up from looping forever."""
        used = int(self._turn.get("budget_refunds", 0))
        if used >= _MAX_REFUNDS:
            return False
        self._turn["budget_refunds"] = used + 1
        return True

    def _read_guard(self, name: str, args: dict) -> str | None:
        """Stops a turn from re-reading the same slice of the same file, and
        nudges the model to start writing once it has burned a whole exploration
        budget without changing anything.

        Counting is per (file, slice): `sed -n '1,120p' f.kt` and
        `sed -n '400,520p' f.kt` are different reads; repeating either of them
        is not. A file is also hard-capped at 8 reads, any slices, so a model
        slicing a big file forever gets stopped instead of eating the whole
        tool budget.

        The counters are task-scoped, not turn-scoped: when a turn dies on the
        tool budget they are restored from the session ledger, otherwise a
        "continue" walks straight back into the same 8 reads. The same counts
        feed the hard write gate at _WRITE_GATE_READS, past which reads are
        refused outright instead of merely discouraged."""
        if self._turn.get("reads") is None:
            # First call of the turn: set up the counters and keep going, or the
            # opening read would never be counted.
            self._turn["reads"] = {}
            self._turn["reads_total"] = 0
            self._turn["reads_per_file"] = {}
            self._hydrate_ledger()
        if self._is_write(name, args):
            self._turn["writes_total"] = int(self._turn.get("writes_total", 0)) + 1
            self._persist_writes()
            return None

        target = _read_targets(name, args)
        if not target:
            return None

        if name in _READ_TOOLS:
            rng = "whole"
        else:
            rng = _range_key(str(args.get("command") or ""))

        # Count the attempt, not the outcome: a blocked re-read still means the
        # model is exploring instead of writing, which is what the nudge is for.
        self._turn["reads_total"] = int(self._turn.get("reads_total", 0)) + 1

        # Hard write gate. The soft nudges at 18 and 40 are advisory and small
        # models answer them by reading once more; past this many reads with
        # nothing written, every read is refused until something is written or
        # the model answers. Counters come from the ledger, so a "continue"
        # after a budget cut does not get a fresh allowance.
        if (self._turn["reads_total"] > _WRITE_GATE_READS
                and not self._turn.get("writes_total")):
            hits = int(self._turn.get("write_gate_hits", 0)) + 1
            self._turn["write_gate_hits"] = hits
            if hits == 1:
                self.conversation.append({"role": "user", "content":
                    "[system] WRITE GATE: %d file reads in this task and nothing written. Reads are now "
                    "closed. Call write_file with the change you already know how to make, or answer "
                    "with what you have and name what is still missing. Another read will be refused."
                    % (self._turn["reads_total"] - 1)})
            self._emit("status", text="Write gate closed after %d reads with no write"
                       % (self._turn["reads_total"] - 1))
            return ("SKIPPED: write gate. %d file reads in this task and nothing written, so reads are "
                    "closed. Call write_file with the change you already know how to make, or answer "
                    "with what you have and name what is still missing."
                    % (self._turn["reads_total"] - 1))

        blocked = None
        for path in target:
            base = os.path.basename(path)
            key = path + "|" + rng
            n = int(self._turn["reads"].get(key, 0)) + 1
            self._turn["reads"][key] = n
            per_file = self._turn.setdefault("reads_per_file", {})
            per_file[path] = int(per_file.get(path, 0)) + 1
            self._ledger_bump(path, rng)
            if n > 2 and not blocked:
                blocked = ("SKIPPED: you already read %s [%s] %d times in this task and the output is in "
                           "the history above. Reading the same slice again changes nothing. Use what "
                           "you have, edit the file with write_file, or read a different line range."
                           % (base, rng, n - 1))
            elif per_file[path] > 8 and not blocked:
                blocked = ("SKIPPED: %s has already been read %d times in this task (different slices). "
                           "You have enough of it. Stop exploring and apply the change with write_file "
                           "using what you already have, or say what is still missing."
                           % (base, per_file[path] - 1))

        if (self._turn["reads_total"] >= 18
                and not self._turn.get("writes_total")
                and not self._turn.get("read_nudged")):
            self._turn["read_nudged"] = True
            self.conversation.append({"role": "user", "content":
                "[system] 18 file reads in this task and nothing written yet. Reads close "
                "soon: apply the change with write_file now using what you already have, or "
                "answer with what you know and name what is still missing."})
            self._emit("status", text="Read budget nudge injected (18 reads, no write)")

        if blocked:
            self._emit("status", text=f"Re-read blocked: {os.path.basename(target[0])} [{rng}]")
        return blocked

    def _safe_tool(self, name: str, args: dict) -> str:
        """Tool call wrapper: whatever goes wrong inside a tool comes back as a
        string for the model, never as an exception that aborts the turn."""
        try:
            result = self._handle_tool(name, args or {})
        except Exception as e:
            logger.exception(f"tool {name} raised")
            result = (f"ERROR: tool '{name}' failed with {type(e).__name__}: {e}. "
                      f"Retry with different arguments or take another approach.")
        try:
            if name in ("memory", "load_tools") and isinstance(result, dict):
                result = json.dumps(result, ensure_ascii=False)
            return result if isinstance(result, str) else str(result)
        except Exception:
            return "ERROR: tool result could not be serialized."

    def _handle_tool(self, name: str, args: dict) -> str:
        """Guarded entry point. A bug or a missing argument inside any tool
        comes back as a string for the model, never as an exception that kills
        the turn (that is what produced "something broke the server 'path'")."""
        try:
            return self._handle_tool_inner(name, args or {})
        except Exception as e:
            logger.exception(f"tool {name} raised")
            return (f"ERROR: tool '{name}' failed with {type(e).__name__}: {e}. "
                    f"Nothing was applied. Retry with different arguments or take another approach.")

    # Tools a sub-agent may use unless the caller asks for a different set.
    SUBAGENT_TOOLS = ["repo_map", "grep", "glob", "read_file", "list_dir",
                      "edit_file", "write_file", "run", "shell"]

    def _run_subtask(self, prompt: str, tools: list | None = None) -> str:
        """Run a piece of work in a fresh agent and return only its report.

        A sub-agent gets a real (temporary) session so the store, the events
        and the cost records behave exactly as in a normal turn, then the
        session is wiped: the chat keeps the report, not the 40 files it read.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return "ERROR: task needs a prompt with everything the sub-agent needs to know."
        if getattr(self, "_is_subagent", False):
            return "ERROR: a sub-agent cannot start another sub-agent. Do the work yourself."
        try:
            sid = create_session("sub: " + re.sub(r"\s+", " ", prompt)[:40])
        except Exception as e:
            return f"ERROR: could not start the sub-agent: {e}"
        sub = UmbraAgent()
        sub._is_subagent = True
        sub.bind_session(sid)
        self._emit("status", text="Sub-agent: " + prompt[:70])
        try:
            text = sub.chat(
                prompt + "\n\n(You are a sub-agent working for Umbra. You cannot ask questions "
                         "and you do not see his chat. Finish with a compact report of at most "
                         "300 words: what you found or changed, with file:line references, and "
                         "anything still open.)",
                selected_tools=(tools or self.SUBAGENT_TOOLS))
        except Exception as e:
            logger.exception("sub-agent failed")
            text = f"ERROR: the sub-agent failed: {type(e).__name__}: {e}"
        finally:
            try:
                from store import messages as _msgs
                from store.manager import delete_session_data
                _msgs.wipe(sid)
                delete_session_data(sid)
            except Exception:
                logger.warning("could not clean up sub-agent session %s", sid, exc_info=True)
        text = (text or "").strip()
        if len(text) > 4000:
            text = text[:4000] + "\n…(report cut at 4000 chars)"
        return "[sub-agent report]\n" + (text or "(it finished without a report)")

    def _should_nudge_verify(self) -> bool:
        """True when code was changed this turn and nothing checked it yet."""
        return bool(self._turn.get("edits") and not self._turn.get("verified")
                    and not self._turn.get("verify_nudged"))

    def _brief_for(self, path) -> str:
        """The project's UMBRA.md / CLAUDE.md, once per turn and per project.

        Read on demand instead of up front: the model only gets it when it
        actually touches that project, and one turn never pays for it twice.
        """
        if not path:
            return ""
        try:
            from store import project_brief
            found = project_brief.find(str(path))
        except Exception:
            return ""
        if not found:
            return ""
        text, src = found
        seen = self._turn.setdefault("briefs", set())
        if src in seen:
            return ""
        seen.add(src)
        return f"[project brief — {src}]\n{text}\n[/project brief]\n\n"

    def _with_brief(self, path, result: str) -> str:
        brief = self._brief_for(path)
        return (brief + str(result)) if brief else result

    def _compact_history(self) -> None:
        """Trim the history, folding what falls out into one short note.

        Dropping the oldest messages silently is how a long coding session
        loses the start of the task. The note carries the chat's summary plus
        what was asked and which tools ran, so the thread survives the cut
        instead of vanishing.
        """
        conv = self.conversation
        kept = trim_history(conv)
        if len(kept) == len(conv):
            self.conversation = kept
            return
        dropped = conv[:len(conv) - len(kept)]
        note = self._compaction_note(dropped)
        self.conversation = ([{"role": "user", "content": note}] if note else []) + kept

    def _compaction_note(self, dropped: list[dict]) -> str:
        summary = ""
        if self.current_sid:
            try:
                summary = (get_summary(self.current_sid, silent=True) or "").strip()
            except Exception:
                summary = ""
        bits: list[str] = []
        tools: list[str] = []
        for m in dropped:
            content = m.get("content")
            if isinstance(content, list):
                text = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
            else:
                text = str(content or "")
            text = re.sub(r"\s+", " ", text).strip()
            if text and not text.startswith("[system]") and not text.startswith("[project brief"):
                bits.append(f"{m.get('role')}: {text[:140]}")
            for call in (m.get("tool_calls") or []):
                name = ((call or {}).get("function") or {}).get("name")
                if name and name not in tools:
                    tools.append(name)
        body = summary[:600]
        if bits:
            body += ("\n" if body else "") + "Earlier: " + " | ".join(bits[-8:])
        if tools:
            body += "\nTools used earlier: " + ", ".join(tools[:12])
        if not body.strip():
            return ""
        return ("[system] Earlier in this chat was compacted out of the transcript. "
                "Keep this in mind:\n" + body[:1800])

    def _handle_tool_inner(self, name: str, args: dict) -> str:
        logger.debug(f"Tool call: {name}({args})")

        # `say` is the sentence the app shows above the bubble, not an input:
        # drop it before the handlers see the arguments. The pre-call bubble was
        # already emitted with it, so nothing is lost here.
        if isinstance(args, dict) and "say" in args:
            args = {k: v for k, v in args.items() if k != "say"}

        if name == "load_tools":
            want = args.get("names") or []
            if isinstance(want, str):
                want = [w.strip() for w in want.split(",")]
            new = toolset.normalize(want) - getattr(self, "_loaded_tools", set())
            unknown = [w for w in want if toolset.LEGACY.get(w, w) not in toolset.LOADABLE]
            self._loaded_tools = set(getattr(self, "_loaded_tools", set())) | new
            if self.current_sid and new:
                prev = set(get_chat_meta(self.current_sid).get("loaded_tools") or [])
                set_chat_meta(self.current_sid, loaded_tools=sorted(prev | new))
            self._emit("tools_loaded", names=sorted(new), reason="model loaded tools on demand")
            msg = (f"Loaded: {', '.join(sorted(new))}. Call them now." if new
                   else "Those tools were already loaded — call them.")
            if unknown:
                msg += f" Unknown: {', '.join(unknown)}."
            return msg

        if name in ("memory", "agents"):
            name, args = toolset.route(name, args)
            if ":" in name:
                return f"Unknown op for {name.split(':')[0]}: {name.split(':')[1]}"

        if name in ("nb_update", "nb_remove", "nb_fact", "nb_note", "nb_rule", "nb_about", "nb_word"):
            from store import notebook as _NBc
            _NBc.set_context(self.current_sid, "umbra")
        if name in ("nb_update", "nb_remove", "nb_fact"):
            from store import notebook as NB
            priv = str((getattr(self, "_persona_opts", {}) or {}).get("notebook") or "")
            page = args.get("page") or (f"{priv}/people" if priv.startswith("private-") else "")
            if name == "nb_fact":
                return NB.add(NB.DEVICES, args.get("section") or "ROCK PI 4B", args.get("fact") or args.get("text") or "",
                              source="umbra")
            if not page:
                return "Give page= (and section=)."
            if NB.is_private(page) and not page.startswith(priv + "/"):
                return "That page belongs to another persona."
            if name == "nb_update":
                return NB.update(page, args.get("section") or "GENERAL", args.get("old") or "", args.get("text") or "", source="umbra")
            return NB.remove(page, args.get("section") or "GENERAL", args.get("old") or args.get("text") or "", source="umbra")

        if name in ("nb_note", "nb_rule", "nb_about", "nb_word"):
            from store import notebook as NB
            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (this message isn't kept in memory)"
            if name == "nb_rule":
                return NB.add(NB.RULES, "GENERAL", args.get("text") or args.get("rule") or "", source="umbra")
            if name == "nb_about":
                return NB.add(NB.ABOUT, "GENERAL", args.get("text") or "", source="umbra")
            if name == "nb_word":
                lang = (args.get("lang") or "words").strip()
                line = f"{args.get('word', '')} — {args.get('translation', '')}".strip(" —")
                if args.get("context"):
                    line += f" (e.g. {args['context']})"
                return NB.add("learning/languages", f"VOCAB {lang}", line, source="umbra")
            _priv = str((getattr(self, "_persona_opts", {}) or {}).get("notebook") or "")
            page = args.get("page") or args.get("topic") or args.get("category") or (
                f"{_priv}/people" if _priv.startswith("private-") else "life/misc")
            if NB.is_private(page) and not page.startswith(_priv + "/"):
                return "That page belongs to another persona."
            if "/" not in page:
                page = next((p for p in NB.SUGGESTED if p.endswith("/" + page)), f"misc/{page}")
            return NB.add(page, args.get("section") or "GENERAL", args.get("text") or args.get("content") or "",
                          args.get("aliases"), source="umbra")

        if name == "read_memory" and (args.get("page") or "/" in str(args.get("topic") or "")):
            from store import notebook as NB
            body = NB.read(args.get("page") or args.get("topic"), args.get("section"))
            return body or f"No notebook page '{args.get('page')}'. Pages: {', '.join(NB.pages())}"

        if name == "read_memory":
            t = (args.get("topic") or "").strip().lower()
            c = (args.get("category") or "").strip().lower()
            if t:
                body = load_topic(t)
                return body or f"No topic file named '{t}'."
            if c:
                body = load_trait(c)
                return body or f"No trait file named '{c}'."
            return "Give topic=... or category=..."

        if name == "find_images":
            from agent import imagefind
            qs = args.get("queries") or ([args["query"]] if args.get("query") else [])
            if isinstance(qs, str):
                qs = [qs]
            self._set_status(f"Finding pictures: {', '.join(map(str, qs))[:80]}")
            res = imagefind.find(qs, kind=args.get("kind", "any"), per=args.get("per", 1))
            urls = self._turn.setdefault("found_urls", set())
            for r in res:
                for im in r["images"]:
                    urls.add(im["url"])
            return imagefind.as_tool_result(res)

        if name == "web_search":
            from agent import websearch
            self._set_status(f"Searching the web: {str(args.get('query', ''))[:80]}")
            meta = get_chat_meta(self.current_sid) if self.current_sid else {}
            eng = meta.get("web_search_model") or None
            mode = _reddit_mode(meta)
            reddit = True if mode == "always" else False if mode == "never" else bool(args.get("reddit"))
            # the stored call shows what really happened (the mode may override the model's choice)
            for t in reversed(self._turn.get("tools") or []):
                if t.get("name") == "web_search" and "result" not in t:
                    t["args"] = dict(t.get("args") or {}, reddit=reddit)
                    break
            q = args.get("query", "")
            out = websearch.search(q, sid=self.current_sid, engine=eng, reddit=reddit,
                                   progress=lambda d: self._emit("search_progress", query=q, **d))
            c = websearch._last.get("cost")
            if c:
                self._turn["search_cost"] = self._turn.get("search_cost", 0.0) + float(c)
            self._turn["last_search"] = out
            return out

        if name == "update_session":
            summary = args.get("summary", "")
            detail  = args.get("detail", "")
            status  = args.get("status")

            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (keep_in_memory=false for this turn)"

            if not self.current_sid:
                self.current_sid = create_session(summary)
                self._send(f"📌 Session `[ID:{self.current_sid}]` started")
                self._emit("session_created", sid=self.current_sid, summary=summary)

            update_session(self.current_sid, summary, status)
            if detail:
                append_memory(self.current_sid, detail)
            self._emit("session_updated", sid=self.current_sid, summary=summary, status=status)

            if self._debug:
                self._send(f"🔍 *Debug* — session `[ID:{self.current_sid}]`: {summary}")

            return f"Session [ID:{self.current_sid}] updated"

        elif name == "switch_topic":
            # New behavior: don't auto-create. Emit a suggestion to the app so
            # the user gets a popup asking whether to start a new chat. They
            # can accept (app creates the new session and navigates) or
            # decline (we keep the current chat).
            suggested = args.get("new_summary", "new chat")
            reason    = args.get("reason", "")
            self._emit("topic_switch_suggested",
                       old_sid=self.current_sid,
                       suggested_name=suggested,
                       reason=reason)
            return (f"Suggestion sent to user: spin up new chat '{suggested}'. "
                    f"The app will show them a popup; they will accept or decline. "
                    f"For now, continue this chat without switching.")

        elif name == "switch_model":
            from agent.providers import set_model, get_client as _gc
            previous = get_current()
            try:
                set_model(args["provider"], args["model"])
                c, m = _gc()
                c.chat.completions.create(
                    model=m,
                    messages=[{"role": "user", "content": "hi"}],
                    max_completion_tokens=5
                )
                self._emit("model_changed",
                           provider=args["provider"], model=args["model"])
                return f"Model switched to [{args['provider']}] {args['model']}"
            except Exception as e:
                if previous:
                    set_model(previous["provider"], previous["model"])
                return f"Failed to switch: {e}. Reverted."

        elif name == "add_rule":
            rule = args.get("rule", "")
            add_rule(rule)
            self._send(f"📋 Rule saved: _{rule}_")
            self._emit("rule_added", rule=rule)
            return f"Rule added: {rule}"

        elif name == "remember_about_user":
            # New shape: forces topic + section so personal info stays in
            # router-loaded topic packs (cheap when not relevant), instead of
            # bloating the always-loaded system.txt.
            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (keep_in_memory=false)"
            topic   = (args.get("topic") or "").strip().lower()
            section = (args.get("section") or "").strip().upper()
            content = (args.get("content") or "").strip()
            keys    = args.get("keys") or []
            # Back-compat: old shape passed just {fact, category}. Translate
            # so old prompts/snapshots don't break, but log a warning.
            if not topic and args.get("fact"):
                logger.warning("remember_about_user called with legacy {fact} shape; "
                               "routing into topic 'eu' for back-compat")
                topic, section, content = "eu", "GENERAL", args["fact"]
            if not topic or not section or not content:
                return ("Missing args. Need: topic (life-domain slug), "
                        "section (SHOUTY-KEBAB-CASE), content (one paragraph).")
            # Sanitize section to SHOUTY-KEBAB-CASE
            section = re.sub(r"[^A-Z0-9-]+", "-", section).strip("-")
            # Create topic if it doesn't exist, then update the section.
            existing = list_topics()
            if topic not in existing:
                seed_keys = keys or [topic]
                create_topic(topic, keys=seed_keys)
                self._emit("user_fact_added_topic_created",
                           topic=topic, keys=seed_keys, section=section)
            update_topic_section(topic, section, content)
            self._emit("user_fact_added",
                       topic=topic, section=section,
                       content_preview=content[:120])
            return f"Saved to topic '{topic}' [{section}]."

        elif name == "add_fact":
            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (keep_in_memory=false)"
            text = (args.get("fact") or args.get("text") or "").strip()
            cat  = args.get("category") or "general"
            if not text:
                return "Missing 'fact'"
            add_fact(text, category=cat, source="agent")
            return f"Fact saved: {text}"

        elif name == "add_trait":
            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (keep_in_memory=false)"
            category = (args.get("category") or "").strip().lower()
            section  = (args.get("section") or "").strip().upper()
            text     = (args.get("text") or args.get("trait") or "").strip()
            if not category or not section or not text:
                return "Missing args. Need: category, section (SHOUTY-KEBAB), text."
            try:
                add_trait(category, section, text, source="agent")
                self._emit("trait_added",
                           category=category, section=section, text=text,
                           reason=f"agent invoked add_trait for [{category}/{section}]")
                return f"Trait saved to traits/{category}.txt [{section}]."
            except Exception as e:
                return f"add_trait failed: {e}"

        elif name == "recall_trait_history":
            category = (args.get("category") or "").strip().lower()
            query    = (args.get("query") or "").strip().lower()
            since    = (args.get("since") or "").strip() or None
            limit    = int(args.get("limit") or 40)
            if not category:
                return "Missing 'category'"
            try:
                entries = read_trait_journal(category, since=since, limit=limit * 4)
                if query:
                    entries = [
                        e for e in entries
                        if query in json.dumps(e, ensure_ascii=False).lower()
                    ]
                entries = entries[-limit:]
                self._emit("trait_history_recalled",
                           category=category, query=query, since=since,
                           returned=len(entries),
                           reason=f"queried traits/{category}.journal.jsonl "
                                  f"(query={query!r} since={since!r})")
                if not entries:
                    return f"No journal entries in traits/{category}.journal.jsonl"
                return json.dumps(entries, ensure_ascii=False, indent=2)[:4000]
            except Exception as e:
                return f"recall_trait_history failed: {e}"

        elif name == "update_topic":
            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (keep_in_memory=false)"
            topic   = (args.get("topic")   or args.get("name") or "").strip().lower()
            section = (args.get("section") or "").strip().upper()
            content = args.get("content") or ""
            if not topic or not section:
                return "Missing 'topic' or 'section'"
            try:
                if topic not in list_topics():
                    keys = args.get("keys") or []
                    create_topic(topic, keys, source="agent")
                update_topic_section(topic, section, content, source="agent")
                return f"topics/{topic}.txt [{section}] updated"
            except Exception as e:
                return f"update_topic failed: {e}"

        elif name == "create_topic":
            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (keep_in_memory=false)"
            topic = (args.get("topic") or args.get("name") or "").strip().lower()
            keys  = args.get("keys") or []
            content = args.get("content") or ""
            if not topic:
                return "Missing 'topic'"
            try:
                create_topic(topic, keys, content=content, source="agent")
                return f"topics/{topic}.txt created with {len(keys)} keys"
            except FileExistsError:
                return f"topic '{topic}' already exists — use update_topic"
            except Exception as e:
                return f"create_topic failed: {e}"

        elif name == "learn_word":
            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (keep_in_memory=false)"
            word = (args.get("word") or "").strip()
            lang = (args.get("lang") or args.get("language") or "").strip().lower()
            if not word or not lang:
                return "Missing 'word' or 'lang'"
            try:
                learn_word(word, lang,
                           translation=args.get("translation", ""),
                           context=args.get("context", ""),
                           section=(args.get("section") or "GENERAL"),
                           source="agent")
                return f"vocab-{lang}.txt: {word} logged"
            except Exception as e:
                return f"learn_word failed: {e}"

        elif name in ("create_agent", "list_agents", "delete_agent",
                       "update_agent", "run_agent_now"):
            from agent import cron as cron_mod
            try:
                if name == "list_agents":
                    return json.dumps(cron_mod.snapshot(), default=str)[:4000]
                if name == "create_agent":
                    # Be lenient with LLM-side arg shapes — Gemini sometimes
                    # passes flat strings instead of nested objects.
                    raw_trigger = args.get("trigger")
                    if isinstance(raw_trigger, str):
                        s = raw_trigger.strip()
                        # cron looks like "0 * * * *" (5 fields, mostly digits/* )
                        if len(s.split()) >= 5:
                            raw_trigger = {"type": "cron", "expr": s}
                        else:
                            # try parsing "30s", "5m", "1h"
                            import re as _re
                            m = _re.fullmatch(r"\s*(\d+)\s*([smh]?)\s*", s)
                            if m:
                                n = int(m.group(1))
                                mul = {"s": 1, "m": 60, "h": 3600, "": 1}[m.group(2)]
                                raw_trigger = {"type": "interval",
                                               "seconds": max(30, n * mul)}
                            else:
                                raw_trigger = {"type": "interval", "seconds": 300}
                    raw_action = args.get("action")
                    if isinstance(raw_action, str):
                        raw_action = {"type": "shell", "command": raw_action}
                    payload = {
                        "name":    args.get("name", "agent"),
                        "trigger": raw_trigger or {},
                        "action":  raw_action or {},
                        "enabled": args.get("enabled", True),
                        "tint":    args.get("tint"),
                    }
                    a = cron_mod.create_agent(payload)
                    self._send(f"🤖 Agent created: *{a['name']}* (id: `{a['id']}`)")
                    logger.info(f"create_agent OK: id={a['id']} name={a['name']}")
                    return f"Agent created id={a['id']}, name={a['name']}"
                if name == "delete_agent":
                    cron_mod.delete_agent(args["id"])
                    return f"Deleted {args['id']}"
                if name == "update_agent":
                    aid = args.pop("id")
                    a = cron_mod.update_agent(aid, args)
                    return f"Updated {aid}"
                if name == "run_agent_now":
                    rec = cron_mod.run_now(args["id"])
                    return f"Run finished: ok={rec.get('ok')} output={rec.get('output','')[:300]}"
            except (KeyError, ValueError) as e:
                # Surface validation failures in the chat so the user sees
                # exactly what went wrong instead of the AI silently failing.
                msg = f"⚠️ {name} failed: {e}"
                self._send(msg)
                logger.warning(f"{name} validation error: {e} · args={args!r}")
                return f"{name} failed: {e}. Args were: {json.dumps(args)[:300]}"
            except Exception as e:
                msg = f"⚠️ {name} error: {e}"
                self._send(msg)
                logger.exception(f"{name} unexpected error")
                return f"{name} error: {e}"

        elif name == "categorize_chat":
            if not self.current_sid:
                return "No active session to categorize"
            tags = args.get("tags") or []
            group = args.get("group")
            if not getattr(self, "_this_turn_keep", True):
                return "Skipped (keep_in_memory=false)"
            set_chat_meta(self.current_sid,
                          tags=tags if tags else None,
                          group=group)
            self._emit("chat_categorized", sid=self.current_sid, tags=tags, group=group)
            return f"Categorized → group={group}, tags={tags}"

        elif name in ("generate_image", "tts"):
            if name == "generate_image":
                args = dict(args, _refs=self._image_refs(args.get("reference")))
                # keep the exact pictures on the stored call ("paint again" reuses them)
                for t in reversed(self._turn.get("tools") or []):
                    if t.get("name") == "generate_image" and "result" not in t:
                        t["args"] = dict(t.get("args") or {}, ref_media=[os.path.basename(r) for r in args["_refs"]])
                        break
                # The model picked in the chat's image bar wins over one the AI chose on
                # its own; a model Maicon named in his message still wins over the bar.
                try:
                    picked = (get_chat_meta(self.current_sid).get("default_image_model") or "") if self.current_sid else ""
                except Exception:
                    picked = ""
                if picked and (args.get("model") or args.get("provider")):
                    said = (self._turn.get("user_text") or "").lower()
                    hint = re.sub(r"[^a-z0-9]+", " ", str(args.get("model") or "").lower()).split()
                    named = any(w in said for w in hint if len(w) > 2 and w not in ("image", "google", "openai", "preview"))
                    if not named:
                        args.pop("model", None); args.pop("provider", None)
            path = dispatch_tool(name, args)
            if os.path.exists(str(path)):
                self._send_file(path)
                kind = "image" if name == "generate_image" else "audio"
                # Explicit return so the LLM doesn't paraphrase this as "I saved
                # it locally" — the file is already in the chat bubble.
                info = ""
                if kind == "image":
                    try:
                        from PIL import Image as _PI
                        with _PI.open(path) as _im:
                            info = f" It is {_im.width}×{_im.height}."
                    except Exception:
                        pass
                try:
                    from agent.tools import peek_last_image_meta
                    _note = (peek_last_image_meta() or {}).get("note") if kind == "image" else None
                except Exception:
                    _note = None
                if _note:
                    info += f" Note: {_note} Tell Maicon this in one short line."
                return (f"OK — the {kind} is already shown in the user's chat.{info} "
                        f"Don't mention the file path; don't invent its size.")
            return str(path)

        elif name == "write_file":
            self._set_status(f"Writing file: {args.get('path', '')}")
            result = dispatch_tool(name, args)
            if _is_code_file(args.get("path")) and not str(result).startswith(("ERROR", "File not found")):
                self._turn["edits"] = int(self._turn.get("edits") or 0) + 1
            if self.current_sid:
                append_memory(self.current_sid,
                    f"File written: {args.get('path', '')} — {len(args.get('content',''))} chars")
            return self._with_brief(args.get("path"), result)

        elif name in ("edit_file", "grep", "glob", "run", "repo_map", "todo", "jobs"):
            self._set_status({
                "edit_file": "Editing " + str(args.get("path", "")),
                "grep": "Searching: " + str(args.get("pattern", ""))[:80],
                "glob": "Finding " + str(args.get("pattern", ""))[:80],
                "run": "Building " + str(args.get("path") or "."),
                "repo_map": "Looking up " + str(args.get("pattern") or args.get("op") or "")[:80],
                "todo": "Updating the task list",
                "jobs": "Background job: " + str(args.get("op") or "list"),
            }[name])
            result = self._with_brief(args.get("path"), dispatch_tool(name, args))
            if (name == "edit_file" and _is_code_file(args.get("path"))
                    and not str(result).startswith(("ERROR", "File not found"))):
                self._turn["edits"] = int(self._turn.get("edits") or 0) + 1
            elif name == "run" and "Couldn't tell" not in str(result):
                self._turn["verified"] = True
            return result

        elif name == "shell":
            cmd = args.get("command", "")
            self._set_status(f"Running terminal command\n`$ {cmd[:120]}`")
            if _is_build_command(cmd):
                self._turn["verified"] = True   # a build/test command counts as checking
            return dispatch_tool(name, args)

        elif name == "python":
            self._set_status("Running Python code...")
            return dispatch_tool(name, args)

        elif name == "browser":
            self._set_status(f"Browser: {args.get('action', '')} {args.get('url', '')[:60]}")
            return dispatch_tool(name, args)

        elif name == "task":
            return self._run_subtask(args.get("prompt") or "", args.get("tools") or None)

        else:
            result = dispatch_tool(name, args)
            if name in ("read_file", "list_dir"):
                result = self._with_brief(args.get("path"), result)
            if not self.current_sid and name in ("shell", "python", "browser"):
                self.current_sid = create_session("task in progress")
                self._send(f"📌 Session `[ID:{self.current_sid}]` started")
            return result

    def reset(self):
        self.conversation = []
        self.current_sid  = None
        self.session_input_tokens  = 0
        self.session_output_tokens = 0
        self._last_prompt_breakdown = {}