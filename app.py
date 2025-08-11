import os
from fastapi import FastAPI, Request, Header, HTTPException
from aiogram.types import Update

# Импортируем твой код как модуль: он создаст bot, dp и зарегистрирует хэндлеры,
# но polling НЕ запустится (т.к. __name__ != "__main__")
import Kinopoisk3_bot as kb

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # задай в переменных окружения на Render

app = FastAPI()

@app.get("/")
async def health():
    return {"ok": True}

@app.post("/webhook/{secret}")
async def telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(None)
):
    # Двойная проверка секрета: и в URL, и в заголовке от Telegram
    if secret != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    data = await request.json()

    # Aiogram v3 + Pydantic v2
    try:
        update = Update.model_validate(data)
    except Exception:
        # На всякий случай fallback
        update = Update(**data)

    await kb.dp.feed_update(kb.bot, update)
    return {"ok": True}
