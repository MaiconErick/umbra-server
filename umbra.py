"""
Umbra — Telegram bot entry point.
"""
import logging, os, asyncio, threading
from dotenv import load_dotenv
from telegram.ext import (
    Application, MessageHandler, CommandHandler, filters
)

from agent.core        import UmbraAgent
from handlers.messages import make_message_handler
from handlers.files    import make_file_handler
from handlers.commands import (
    cmd_model, cmd_debug, cmd_status, cmd_sessions,
    cmd_memory, cmd_rules, cmd_cancel, cmd_help, make_cmd_persona
)

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
XUMBRA_ENABLED   = os.getenv("XUMBRA_API_ENABLED", "true").lower() != "false"
XUMBRA_PORT      = int(os.getenv("XUMBRA_API_PORT", "8765"))


def _setup_logging():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(BASE_DIR, "logs", "umbra.log"))
        ]
    )
    # silence noisy libs
    for lib in ("httpx", "httpcore", "telegram", "openai", "urllib3"):
        logging.getLogger(lib).setLevel(logging.WARNING)


# ─────────────────────────────────────────
#  Chat ID guard middleware
# ─────────────────────────────────────────
def _authorized(update) -> bool:
    return update.effective_chat.id == TELEGRAM_CHAT_ID


async def _reject_unauthorized(update, context):
    pass  # silently ignore


# ─────────────────────────────────────────
#  Watchdog — restarts on fatal crash
# ─────────────────────────────────────────
import subprocess, sys, time

def run_with_watchdog():
    while True:
        proc = subprocess.run([sys.executable, __file__, "--no-watchdog"])
        logging.error(f"Umbra exited with code {proc.returncode}. Restarting in 5s...")
        time.sleep(5)


# ─────────────────────────────────────────
#  Main
# ─────────────────────────────────────────
def _start_api_server_thread():
    try:
        from api.server import run as run_api
    except Exception as e:
        logging.error(f"Could not import xUmbra API server: {e}")
        return
    t = threading.Thread(
        target=run_api,
        kwargs={"host": "0.0.0.0", "port": XUMBRA_PORT},
        name="xumbra-api",
        daemon=True,
    )
    t.start()
    logging.info(f"xUmbra API thread started on port {XUMBRA_PORT}")


def main():
    _setup_logging()
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)

    # Record successful boot so the phone's restart inspector can see whether
    # a previous restart request actually came back up.
    try:
        import json, time as _t
        prev_step = None
        rs_path = os.path.join(BASE_DIR, "logs", "restart_status.json")
        if os.path.exists(rs_path):
            try:
                with open(rs_path) as f:
                    prev_step = json.load(f).get("step")
            except Exception:
                prev_step = None
        with open(rs_path, "w") as f:
            json.dump({"step": "restored", "ts": _t.time(),
                       "pid": os.getpid(),
                       "previous_step": prev_step}, f)
        logging.info(f"Umbra boot OK (prev restart step: {prev_step})")
    except Exception as e:
        logging.warning(f"restart_status write failed: {e}")

    if XUMBRA_ENABLED:
        _start_api_server_thread()

    agent = UmbraAgent()

    handle_message = make_message_handler(agent)
    handle_file    = make_file_handler(agent)

    # wrap handlers with auth guard
    async def guarded_message(update, context):
        if not _authorized(update):
            return await _reject_unauthorized(update, context)
        await handle_message(update, context)

    async def guarded_file(update, context):
        if not _authorized(update):
            return
        await handle_file(update, context)

    # why was Umbra down? (read before the heartbeat overwrites the old state)
    from store import lifecycle
    online_text = lifecycle.diagnose()
    logging.info("online message: " + online_text.replace(chr(10), " | "))

    def _tg_send(text: str):
        import requests
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=20)
    lifecycle.start(send=_tg_send)
    try:
        from store import agents_bridge          # agents listed in data/settings/agents.json
        agents_bridge.start()
    except Exception as e:
        print(f"agents bridge failed: {e}")
    try:
        from store import netwatch                # who's on the home network → 🛜 Network chat
        netwatch.start()
    except Exception as e:
        print(f"netwatch failed: {e}")

    async def post_init(app):
        me = await app.bot.get_me()
        logging.info(f"Umbra online as @{me.username}")
        try:
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=online_text, parse_mode="Markdown")
        except Exception:
            await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=online_text.replace("*", "").replace("`", ""))

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()

    # commands
    app.add_handler(CommandHandler("model",    cmd_model))
    app.add_handler(CommandHandler("debug",    cmd_debug))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("memory",   cmd_memory))
    app.add_handler(CommandHandler("rules",    cmd_rules))
    app.add_handler(CommandHandler("cancel",   cmd_cancel))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("persona",  make_cmd_persona(agent)))

    # files
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.PHOTO | filters.AUDIO | filters.VOICE,
        guarded_file
    ))

    # text
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        guarded_message
    ))

    app.run_polling(drop_pending_updates=True)
    lifecycle.mark_stopped()   # only reached on a normal stop (systemctl stop/restart, shutdown)


if __name__ == "__main__":
    if "--no-watchdog" in sys.argv:
        main()
    else:
        run_with_watchdog()
