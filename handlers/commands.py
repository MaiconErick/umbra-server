"""
Telegram command handlers for Umbra.
/model, /debug, /cancel, /status, /sessions, /rules, /schedule
"""
import os, configparser, logging
from telegram import Update
from telegram.ext import ContextTypes
from agent.providers import fetch_models, set_model, get_current, get_history
from store.manager  import (
    get_rules, get_all_sessions, get_memory, get_failed_commands,
    create_session, set_chat_meta, get_chat_meta,
)
from agent.tools     import tool_system_snapshot

logger  = logging.getLogger(__name__)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.ini")


# ─────────────────────────────────────────
#  /model
# ─────────────────────────────────────────
async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args

    if not args:
        current = get_current()
        history = get_history()[:5]
        hist_text = "\n".join(f"  • `[{e['provider']}] {e['model']}`" for e in history)
        cur_text  = f"`[{current['provider']}] {current['model']}`" if current else "_not set_"
        await update.message.reply_text(
            f"*Current model:* {cur_text}\n\n"
            f"*Recently used:*\n{hist_text or '  (none)'}\n\n"
            f"Commands:\n"
            f"`/model fetch` — list all available\n"
            f"`/model <provider> <model>` — switch",
            parse_mode="Markdown"
        )
        return

    if args[0] == "fetch":
        await update.message.reply_text("_Fetching models from all providers..._", parse_mode="Markdown")
        models = fetch_models()
        for provider, ids in models.items():
            if ids and not str(ids[0]).startswith("ERROR") and ids[0] != "NOT REACHABLE":
                lista = "\n".join(f"  `{m}`" for m in ids[:30])
                extra = f"\n  _...and {len(ids)-30} more_" if len(ids) > 30 else ""
                await update.message.reply_text(
                    f"*{provider.upper()}* ({len(ids)} models):\n{lista}{extra}",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(
                    f"*{provider.upper()}:* `{ids[0]}`",
                    parse_mode="Markdown"
                )

        # if ollama not reachable, offer history
        if models.get("ollama", [""])[0] == "NOT REACHABLE":
            history = get_history()
            if history:
                hist = "\n".join(f"  `[{e['provider']}] {e['model']}`" for e in history[:5])
                await update.message.reply_text(
                    f"⚠️ *Ollama not reachable.*\n\n*Recently used models:*\n{hist}\n\n"
                    f"Use `/model <provider> <model>` to switch.",
                    parse_mode="Markdown"
                )
        await update.message.reply_text(
            "Usage: `/model <provider> <model_id>`\nExample: `/model openrouter anthropic/claude-3.5-sonnet`",
            parse_mode="Markdown"
        )
        return

    if len(args) >= 2:
        provider = args[0]
        model_id = args[1]
        await update.message.reply_text("_Testing model..._", parse_mode="Markdown")
        try:
            from agent.providers import set_model, PROVIDER_CONFIG
            import os
            from openai import OpenAI
            set_model(provider, model_id)
            # quick test call
            cfg = PROVIDER_CONFIG.get(provider, {})
            client = OpenAI(
                base_url=cfg.get("base_url", ""),
                api_key=cfg.get("api_key", "sk-test")
            )
            client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": "hi"}],
                max_completion_tokens=5
            )
            await update.message.reply_text(
                f"✅ Model set: `[{provider}] {model_id}`",
                parse_mode="Markdown"
            )
        except Exception as e:
            from agent.providers import _save_used, _load_used
            used = _load_used()
            used["current"] = None
            _save_used(used)
            await update.message.reply_text(
                f"❌ *Model rejected:* `{model_id}`\n`{str(e)[:300]}`",
                parse_mode="Markdown"
            )
        return

    await update.message.reply_text(
        "Usage: `/model fetch` or `/model <provider> <model_id>`",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
#  /debug
# ─────────────────────────────────────────
async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)

    args = context.args
    if args and args[0].lower() in ("on", "off", "true", "false", "1", "0"):
        value = args[0].lower() in ("on", "true", "1")
    else:
        # toggle
        value = not cfg.getboolean("general", "debug", fallback=False)

    if not cfg.has_section("general"):
        cfg.add_section("general")
    cfg.set("general", "debug", str(value).lower())

    with open(CONFIG_PATH, "w") as f:
        cfg.write(f)

    state = "ON 🟢" if value else "OFF 🔴"
    await update.message.reply_text(f"*Debug mode:* {state}", parse_mode="Markdown")


# ─────────────────────────────────────────
#  /status
# ─────────────────────────────────────────
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = configparser.ConfigParser()
    cfg.read(CONFIG_PATH)
    debug = cfg.getboolean("general", "debug", fallback=False)

    current = get_current()
    model_text = f"`[{current['provider']}] {current['model']}`" if current else "_not set_"

    try:
        snap = tool_system_snapshot()
    except Exception as e:
        snap = f"Error: {e}"

    rules = get_rules()
    rules_preview = "\n".join(rules.splitlines()[-3:]) if rules else "(none)"

    failed = get_failed_commands()[-3:]
    failed_text = "\n".join(f"  `{c}`" for c in failed) if failed else "  (none)"

    await update.message.reply_text(
        f"*Umbra Status*\n\n"
        f"*Model:* {model_text}\n"
        f"*Debug:* {'ON 🟢' if debug else 'OFF 🔴'}\n\n"
        f"*System:*\n```\n{snap}\n```\n\n"
        f"*Last 3 rules:*\n{rules_preview}\n\n"
        f"*Last 3 failed commands:*\n{failed_text}",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
#  /sessions
# ─────────────────────────────────────────
async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sessions = get_all_sessions()
    if not sessions or sessions == "(no sessions yet)":
        await update.message.reply_text("_No sessions yet._", parse_mode="Markdown")
        return

    lines = sessions.splitlines()[-20:]  # last 20
    text  = "\n".join(lines)
    await update.message.reply_text(
        f"*Recent Sessions:*\n```\n{text}\n```",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
#  /memory <id>
# ─────────────────────────────────────────
async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: `/memory <session_id>` — e.g. `/memory 0001`",
            parse_mode="Markdown"
        )
        return

    sid = args[0].zfill(4)
    mem = get_memory(sid)
    if not mem:
        await update.message.reply_text(f"_No memory found for ID:{sid}_", parse_mode="Markdown")
        return

    text = f"*Memory [ID:{sid}]:*\n```\n{mem[:3500]}\n```"
    await update.message.reply_text(text, parse_mode="Markdown")


# ─────────────────────────────────────────
#  /rules
# ─────────────────────────────────────────
async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = get_rules()
    if not rules:
        await update.message.reply_text("_No permanent rules yet._", parse_mode="Markdown")
        return
    await update.message.reply_text(
        f"*Permanent Rules:*\n```\n{rules[:3500]}\n```",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
#  /cancel
# ─────────────────────────────────────────
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # re-uses the logic in messages.py via the cancel word detection
    # this handler exists for explicit /cancel command
    from handlers.messages import CANCEL_WORDS
    update.message.text = "cancel"
    # handled by message handler — just inform
    await update.message.reply_text(
        "_Send_ `cancel` _or_ `/cancel` _as a message to interrupt running tasks._",
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────
#  /persona — switch which persona this Telegram chat uses
# ─────────────────────────────────────────
def make_cmd_persona(agent):
    """Factory so the handler can reach the singleton Telegram-side agent."""
    async def cmd_persona(update: Update, context: ContextTypes.DEFAULT_TYPE):
        from agent import personas as personas_mod
        args = context.args
        # Ensure we have a session bound; create one lazily on first use so
        # the persona has a chat_meta row to attach itself to.
        sid = agent.current_sid
        if sid is None:
            sid = create_session("[telegram] persona chat")
            agent.bind_session(sid)

        if not args:
            available = [p["name"] for p in personas_mod.list_personas()]
            cur = get_chat_meta(sid).get("persona") or "(none)"
            avail_text = ", ".join(f"`{n}`" for n in available) or "_no personas installed_"
            await update.message.reply_text(
                f"*Personas:* {avail_text}\n"
                f"*Current:* `{cur}`  (session #{sid})\n\n"
                f"`/persona <name>` — switch\n"
                f"`/persona off`    — clear",
                parse_mode="Markdown",
            )
            return

        name = args[0].strip().lower()
        if name in ("off", "clear", "none", "-"):
            set_chat_meta(sid, persona="")
            await update.message.reply_text("_Persona cleared._", parse_mode="Markdown")
            return

        if personas_mod._persona_dir(name) is None:
            avail = ", ".join(p["name"] for p in personas_mod.list_personas())
            await update.message.reply_text(
                f"_No persona named `{name}`._ Available: {avail}",
                parse_mode="Markdown",
            )
            return

        set_chat_meta(sid, persona=name)
        # Mirror the API's auto-plumbing: proactive mode + tool enablement.
        meta = personas_mod._persona_meta(name) or {}
        if meta.get("needs_proactive"):
            set_chat_meta(sid, allow_proactive=True)
        wanted = meta.get("enable_tools") or []
        if wanted:
            existing = get_chat_meta(sid).get("enabled_tools") or []
            merged = sorted(set(existing) | set(wanted))
            set_chat_meta(sid, enabled_tools=merged)

        model = meta.get("model") or "(global model)"
        await update.message.reply_text(
            f"_Persona set to_ `{name}` _on session #{sid}._\n"
            f"Model: `{model}`",
            parse_mode="Markdown",
        )
    return cmd_persona


# ─────────────────────────────────────────
#  /help
# ─────────────────────────────────────────
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "*Umbra Commands*\n\n"
        "`/model` — show current model\n"
        "`/model fetch` — list all available models\n"
        "`/model <provider> <id>` — switch model\n\n"
        "`/persona` — list personas / show current\n"
        "`/persona <name>` — switch to persona (general, fbi, events, image)\n"
        "`/persona off` — clear persona\n\n"
        "`/debug` — toggle debug mode\n"
        "`/status` — system status + model info\n"
        "`/sessions` — list recent sessions\n"
        "`/memory <id>` — show session details\n"
        "`/rules` — show permanent rules\n"
        "`/help` — this message\n\n"
        "Send `cancel` or `cancelar` at any time to stop a running command.\n\n"
        "Send a file to have Umbra analyze or process it.\n"
        "Say _'I'll send you a file to...'_ first to give context.",
        parse_mode="Markdown"
    )
