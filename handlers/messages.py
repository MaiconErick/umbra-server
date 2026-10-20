"""
Telegram message handler for Umbra.
"""
import asyncio, logging, os
from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

_active_task: asyncio.Task | None = None
_cancel_requested = False
_pending_file_instruction: str | None = None
_agent = None  # set by umbra.py

CANCEL_WORDS = {"cancel", "cancelar", "/cancel"}

FILE_INTENT_HINTS = [
    "vou te mandar", "vou enviar", "vou te enviar",
    "sending you", "i'll send", "will send", "gonna send",
    "i'm sending", "here's a file", "vou mandar",
]

TICK_INTERVALS = [3] * 1000


# ─────────────────────────────────────────
#  Processing message
# ─────────────────────────────────────────
async def _send_processing(update: Update) -> int:
    msg = await update.message.reply_text(
        "⚙ _processing\\.\\.\\. \\[0s\\]_",
        parse_mode="MarkdownV2"
    )
    return msg.message_id

def _build_processing_text(elapsed: int, status_line: str = "") -> str:
    def esc(s: str) -> str:
        for c in r"_*[]()~`>#+-=|{}.!\\":
            s = s.replace(c, f"\\{c}")
        return s

    header = f"⚙ _processing\\.\\.\\. \\[{elapsed}s\\]_"
    if not status_line:
        return header

    lines = [header, ""]
    for line in status_line.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("`") or stripped.startswith("$"):
            clean = stripped.strip("`").strip("$ ")
            lines.append(f"`{clean[:80]}`")
        elif stripped.startswith("_") and stripped.endswith("_"):
            lines.append(stripped)
        else:
            lines.append(f"◈ _{esc(stripped[:80])}_")

    lines.append("")
    lines.append("_/cancel to interrupt_")
    return "\n".join(lines)

async def _update_processing(context, chat_id: int, message_id: int,
                              elapsed: int, status: str = ""):
    try:
        text = _build_processing_text(elapsed, status)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="MarkdownV2"
        )
    except Exception:
        pass

async def _delete_processing(context, chat_id: int, message_id: int):
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def _ticker(context, chat_id: int, message_id: int,
                  done: asyncio.Event, status_ref: list, elapsed_ref: list):
    for interval in TICK_INTERVALS:
        await asyncio.sleep(interval)
        if done.is_set():
            break
        elapsed_ref[0] += interval
        await _update_processing(
            context, chat_id, message_id,
            elapsed_ref[0],
            status_ref[0] if status_ref else ""
        )


# ─────────────────────────────────────────
#  Message splitting
# ─────────────────────────────────────────
def _split_message(text: str, limit: int = 4096) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text[:limit].rfind("\n")
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


# ─────────────────────────────────────────
#  Cancel
# ─────────────────────────────────────────
async def _handle_cancel(update: Update):
    global _active_task, _cancel_requested
    if _active_task and not _active_task.done():
        _cancel_requested = True
        _active_task.cancel()
        try:
            if _agent:
                _agent.reset()
        except Exception:
            pass
        await update.message.reply_text("*Cancelled\\.*", parse_mode="MarkdownV2")
    else:
        await update.message.reply_text("_Nothing running\\._", parse_mode="MarkdownV2")


# ─────────────────────────────────────────
#  Pending file
# ─────────────────────────────────────────
def get_and_clear_pending() -> str | None:
    global _pending_file_instruction
    val = _pending_file_instruction
    _pending_file_instruction = None
    return val


# ─────────────────────────────────────────
#  Main handler factory
# ─────────────────────────────────────────
def make_message_handler(agent):

    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global _active_task, _cancel_requested, _pending_file_instruction

        user_text = update.message.text
        chat_id   = update.effective_chat.id

        if user_text.strip().lower() in CANCEL_WORDS:
            await _handle_cancel(update)
            return

        lower = user_text.lower()
        if any(hint in lower for hint in FILE_INTENT_HINTS):
            _pending_file_instruction = user_text
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: agent.chat(user_text))
            await update.message.reply_text(
                "_Got it\\. Send the file whenever you're ready\\._",
                parse_mode="MarkdownV2"
            )
            return

        if _active_task and not _active_task.done():
            await update.message.reply_text(
                "_Already processing\\. Send /cancel to interrupt\\._",
                parse_mode="MarkdownV2"
            )
            return

        _cancel_requested = False
        done        = asyncio.Event()
        status_ref  = [""]
        elapsed_ref = [0]

        # get loop before defining callbacks
        loop = asyncio.get_event_loop()

        # send processing message FIRST so processing_id exists when callbacks fire
        processing_id = await _send_processing(update)

        def on_output_line(line: str):
            if line.strip():
                status_ref[0] = line.strip()

        def on_status(text: str):
            status_ref[0] = text
            asyncio.run_coroutine_threadsafe(
                _update_processing(context, chat_id, processing_id, elapsed_ref[0], text),
                loop
            )

        from agent.tools import set_output_callback
        set_output_callback(on_output_line)
        agent.set_status_callback(on_status)

        ticker_task = asyncio.create_task(
            _ticker(context, chat_id, processing_id, done, status_ref, elapsed_ref)
        )

        async def send_file(path: str):
            try:
                with open(path, "rb") as f:
                    fname = os.path.basename(path)
                    ext   = fname.rsplit(".", 1)[-1].lower()
                    if ext in ("mp3", "ogg", "wav"):
                        await context.bot.send_audio(chat_id=chat_id, audio=f, filename=fname)
                    elif ext in ("png", "jpg", "jpeg", "webp"):
                        await context.bot.send_photo(chat_id=chat_id, photo=f)
                    else:
                        await context.bot.send_document(chat_id=chat_id, document=f, filename=fname)
            except Exception as e:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"*Failed to send file:* `{e}`",
                    parse_mode="Markdown"
                )

        agent.set_send_callback(
            lambda text: asyncio.run_coroutine_threadsafe(
                context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown"),
                loop
            )
        )
        agent.set_file_callback(
            lambda path: asyncio.run_coroutine_threadsafe(send_file(path), loop)
        )

        _active_task = asyncio.current_task()

        try:
            response = await loop.run_in_executor(
                None, lambda: agent.chat(user_text)
            )

            done.set()
            ticker_task.cancel()
            await _delete_processing(context, chat_id, processing_id)

            if _cancel_requested:
                return

            for chunk in _split_message(response):
                try:
                    await update.message.reply_text(chunk, parse_mode="Markdown")
                except Exception:
                    await update.message.reply_text(chunk)

        except asyncio.CancelledError:
            done.set()
            ticker_task.cancel()
            await _delete_processing(context, chat_id, processing_id)

        except Exception as e:
            done.set()
            ticker_task.cancel()
            await _delete_processing(context, chat_id, processing_id)
            logger.exception("Error in handle_message")
            await update.message.reply_text(
                f"*Error:*\n`{str(e)[:500]}`", parse_mode="Markdown"
            )

        finally:
            _active_task = None
            set_output_callback(None)
            agent.set_status_callback(None)

    return handle_message