import asyncio
import hmac
import hashlib
import logging
import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, request
from aiogram import Bot
from aiogram.types import Update

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

from main import dp, _configure_logging  # noqa: E402

_configure_logging()
logger = logging.getLogger(__name__)
app = Flask(__name__)


def _telegram_webhook_secret() -> str:
    raw_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    return hashlib.sha256(raw_secret.encode("utf-8")).hexdigest() if raw_secret else ""


_loop = None
_loop_ready = threading.Event()
_webhook_registered = False
_webhook_error = None


async def _register_webhook() -> None:
    global _webhook_registered, _webhook_error
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    secret = _telegram_webhook_secret()
    url = os.getenv("TELEGRAM_WEBHOOK_URL")
    if not url:
        render_url = os.getenv("RENDER_EXTERNAL_URL")
        if render_url:
            url = f"{render_url.rstrip('/')}/webhook"
    if not token or not secret or not url:
        logger.warning("Webhook registration skipped: configuration is incomplete")
        return

    bot = Bot(token=token)
    try:
        for attempt in range(3):
            try:
                await bot.set_webhook(
                    url=url,
                    secret_token=secret,
                    drop_pending_updates=False,
                )
                info = await bot.get_webhook_info()
                if info.url != url:
                    raise RuntimeError("Telegram did not confirm the configured webhook URL")
                _webhook_registered = True
                _webhook_error = None
                logger.info("Telegram webhook registered successfully")
                return
            except Exception as exc:
                _webhook_registered = False
                _webhook_error = type(exc).__name__
                logger.error(
                    "Telegram webhook registration attempt %s/3 failed (%s)",
                    attempt + 1,
                    _webhook_error,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** (attempt + 1))
    finally:
        await bot.session.close()


def _run_loop() -> None:
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop_ready.set()
    if os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_WEBHOOK_SECRET"):
        _loop.create_task(_register_webhook())
    _loop.run_forever()


if os.getenv("TELEGRAM_BOT_TOKEN"):
    threading.Thread(target=_run_loop, name="telegram-webhook-loop", daemon=True).start()


async def _process_update(payload: dict) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    bot = Bot(token=token)
    try:
        update = Update.model_validate(payload, context={"bot": bot})
        await dp.feed_update(bot, update)
    finally:
        await bot.session.close()


def _log_task_result(future) -> None:
    try:
        future.result()
    except Exception as exc:
        logger.error(
            "Webhook update processing failed (%s)",
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
        )


@app.get("/")
def index():
    return "Telegram bot webhook is installed.\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.get("/healthz")
def healthz():
    return jsonify(status="ok"), 200


@app.get("/health")
def health():
    configured = bool(os.getenv("TELEGRAM_BOT_TOKEN")) and bool(
        os.getenv("TELEGRAM_WEBHOOK_SECRET")
    )
    registered = configured and _webhook_registered
    status = "ready" if registered else ("registering" if configured else "needs_configuration")
    return jsonify(
        status=status,
        webhook_registered=bool(registered),
        registration_error=_webhook_error,
    ), (200 if registered else 503)


@app.post("/webhook")
def webhook():
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    secret = _telegram_webhook_secret()
    if not token or not secret or _loop is None:
        abort(503)

    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied_secret, secret):
        abort(403)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)

    if not _loop_ready.wait(timeout=5):
        abort(503)
    asyncio.run_coroutine_threadsafe(_process_update(payload), _loop).add_done_callback(
        _log_task_result
    )
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}
