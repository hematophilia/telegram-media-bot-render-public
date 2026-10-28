import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from aiogram import Bot

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
URL = os.getenv("TELEGRAM_WEBHOOK_URL", "https://hematophilia.pythonanywhere.com/webhook")


async def main() -> None:
    if not TOKEN or not SECRET:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_WEBHOOK_SECRET in .env")
    bot = Bot(token=TOKEN)
    try:
        await bot.set_webhook(
            url=URL,
            secret_token=SECRET,
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
        if info.url != URL:
            raise SystemExit("Telegram did not confirm the requested webhook URL")
        print("Telegram webhook configured successfully.")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
