import os
import logging
from typing import Optional

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import PlainTextResponse, Response, JSONResponse
from aiogram.types import Update

# Импортируем твой бота: создаются kb.bot и kb.dp, хэндлеры регистрируются.
# Polling не запускается, т.к. __name__ != "__main__"
import Kinopoisk3_bot as kb

# ====== ЛОГИ ======
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# Секрет для проверки URL и заголовка от Telegram
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET не задан в переменных окружения")

app = FastAPI(title="Telegram Bot Webhook")

# ---------- HEALTH ----------
@app.get("/", response_class=PlainTextResponse)
async def health() -> str:
    # Простой ответ для браузера и UptimeRobot
    return "Bot is alive"

@app.head("/")
async def health_head() -> Response:
    # UptimeRobot иногда делает HEAD — возвращаем 200
    return Response(status_code=200)

@app.get("/ping", response_class=PlainTextResponse)
async def ping() -> str:
    # Точка для мониторинга (можно использовать в UptimeRobot)
    return "OK"

# ---------- WEBHOOK ----------
@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    # Двойная проверка: секрет в URL и в заголовке
    if secret != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        log.warning("Forbidden webhook call: url_secret=%s header=%s", secret, x_telegram_bot_api_secret_token)
        raise HTTPException(status_code=403, detail="forbidden")

    try:
        data = await request.json()
    except Exception:
        log.exception("Invalid JSON in webhook")
        raise HTTPException(status_code=400, detail="invalid json")

    # Aiogram v3 + Pydantic v2: сначала пробуем model_validate
    try:
        update = Update.model_validate(data)
    except Exception:
        update = Update(**data)

    try:
        await kb.dp.feed_update(kb.bot, update)
    except Exception:
        log.exception("Error while processing update")
        # Возвращаем 200, чтобы Telegram не спамил ретраями — ошибка залогирована
        return JSONResponse({"ok": False}, status_code=200)

    return {"ok": True}
