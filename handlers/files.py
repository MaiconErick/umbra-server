"""
Telegram file handler for Umbra.
Downloads any incoming file to /temp and passes it to the agent.
"""
import os, logging
from telegram import Update
from telegram.ext import ContextTypes
from handlers.messages import get_and_clear_pending

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP_DIR = os.path.join(BASE_DIR, "temp")
os.makedirs(TEMP_DIR, exist_ok=True)


def make_file_handler(agent):

    async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg     = update.message
        caption = msg.caption

        # detect file type
        if msg.document:
            tg_file  = await msg.document.get_file()
            filename = msg.document.file_name
        elif msg.photo:
            tg_file  = await msg.photo[-1].get_file()
            filename = "image.jpg"
        elif msg.audio:
            tg_file  = await msg.audio.get_file()
            filename = msg.audio.file_name or "audio.mp3"
        elif msg.voice:
            tg_file  = await msg.voice.get_file()
            filename = "voice.ogg"
        else:
            await msg.reply_text("_Unrecognized file type._", parse_mode="Markdown")
            return

        dest = os.path.join(TEMP_DIR, filename)
        await tg_file.download_to_drive(dest)

        instruction = caption or get_and_clear_pending() or \
            "Analyze this file and tell me what's relevant."

        await msg.reply_text(
            f"_Received_ `{filename}`_. Processing..._",
            parse_mode="Markdown"
        )

        prompt = f"{instruction}\nFile saved at: {dest}"

        try:
            loop = __import__("asyncio").get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: agent.chat(prompt)
            )
            for i in range(0, len(response), 4096):
                try:
                    await msg.reply_text(response[i:i+4096], parse_mode="Markdown")
                except Exception:
                    await msg.reply_text(response[i:i+4096])
        except Exception as e:
            logger.exception("Error in handle_file")
            await msg.reply_text(f"*Error:*\n`{str(e)[:500]}`", parse_mode="Markdown")

    return handle_file
