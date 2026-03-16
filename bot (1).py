import os
import json
import base64
import logging
import re
from io import BytesIO
from datetime import datetime

import httpx
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
VSEGPT_API_KEY   = os.environ["VSEGPT_API_KEY"]
SOURCE_CHAT_ID   = int(os.environ["SOURCE_CHAT_ID"])
OUTPUT_CHAT_ID   = int(os.environ["OUTPUT_CHAT_ID"])
IDLE_TIMEOUT_MIN = int(os.environ.get("IDLE_TIMEOUT_MIN", "15"))

DONE_TRIGGERS = {"готово", "готов", "всё", "все", "отправляй", "go", "done", "ок", "ok"}

VSEGPT_URL = "https://api.vsegpt.ru/v1/chat/completions"
VSEGPT_MODEL = "openai/gpt-4o-mini"

# ─── STATE ────────────────────────────────────────────────────────────────────
lot_buffer: dict = {}

SYSTEM_APPRAISAL = """Ты эксперт-оценщик автомобилей. Тебе передают данные одного автомобиля: фотографии и текстовые описания.

Выдай оценку СТРОГО в JSON формате без пояснений и текста вне JSON:
{
  "plate": "A123BC777",
  "vin": "WVWZZZ3CZWE689725",
  "make": "Toyota",
  "model": "Camry",
  "year": 2019,
  "color": "Белый",
  "mileage_est": "80 000–100 000 км",
  "condition_score": 74,
  "condition_items": [
    {"label": "Кузов",     "status": "хорошее"},
    {"label": "ЛКП",       "status": "царапины"},
    {"label": "Салон",     "status": "среднее"},
    {"label": "Двигатель", "status": "не видно"}
  ],
  "price_rub": 1850000,
  "price_min":  1700000,
  "price_max":  2000000,
  "price_factors": [
    {"label": "Состояние", "note": "умеренный износ"},
    {"label": "Рынок",     "note": "средний сегмент"},
    {"label": "Пробег",    "note": "оценочно по фото"}
  ],
  "summary": "Краткое резюме 1-2 предложения для менеджера"
}

Если VIN/номер не распознаётся — сгенерируй правдоподобные.
Отвечай ТОЛЬКО JSON."""


def fmt(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


def build_report(r: dict, lot_num: int) -> str:
    score = r.get("condition_score", 0)
    bar = "█" * round(score / 10) + "░" * (10 - round(score / 10))
    lines = [
        f"🚗 *Оценка автомобиля \\#{lot_num}*",
        f"📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        "",
        "📋 *Идентификация*",
        f"🔲 Номер: `{r.get('plate', '—')}`",
        f"🔑 VIN: `{r.get('vin', '—')}`",
        f"🏷 {r.get('make', '')} {r.get('model', '')} {r.get('year', '')}".strip(),
        f"🎨 Цвет: {r.get('color', '—')}",
        f"📍 Пробег: {r.get('mileage_est', '—')}",
        "",
        f"🔧 *Техсостояние* — {score}%",
        f"`{bar}`",
    ]
    for item in r.get("condition_items", []):
        lines.append(f"  • {item['label']}: {item['status']}")

    price = r.get("price_rub")
    if price:
        lines += [
            "",
            "💰 *Рыночная оценка*",
            f"Рекомендованная цена: *{fmt(price)}*",
            f"Диапазон: {fmt(r.get('price_min', price))} — {fmt(r.get('price_max', price))}",
        ]
        for fi in r.get("price_factors", []):
            lines.append(f"  • {fi['label']}: {fi['note']}")

    if r.get("summary"):
        lines += ["", f"📝 {r['summary']}"]

    return "\n".join(lines)


def get_lot_number() -> int:
    path = "/tmp/lot_counter.txt"
    try:
        with open(path) as f:
            n = int(f.read().strip()) + 1
    except Exception:
        n = 1
    with open(path, "w") as f:
        f.write(str(n))
    return n


def ensure_buffer(chat_id: int) -> dict:
    if chat_id not in lot_buffer:
        lot_buffer[chat_id] = {
            "texts": [], "photos": [],
            "media_group_pending": {}, "timer_job": None,
        }
    return lot_buffer[chat_id]


def reset_buffer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    buf = lot_buffer.get(chat_id, {})
    if buf.get("timer_job"):
        try: buf["timer_job"].schedule_removal()
        except: pass
    for mg in buf.get("media_group_pending", {}).values():
        if mg.get("job"):
            try: mg["job"].schedule_removal()
            except: pass
    lot_buffer[chat_id] = {
        "texts": [], "photos": [],
        "media_group_pending": {}, "timer_job": None,
    }


def restart_idle_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    buf = ensure_buffer(chat_id)
    if buf.get("timer_job"):
        try: buf["timer_job"].schedule_removal()
        except: pass
    job = context.job_queue.run_once(
        idle_timer_fired, when=IDLE_TIMEOUT_MIN * 60,
        data=chat_id, name=f"idle_{chat_id}",
    )
    buf["timer_job"] = job


async def idle_timer_fired(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    buf = lot_buffer.get(chat_id, {})
    if buf.get("photos") or buf.get("texts"):
        await process_lot(chat_id, context, trigger="timer")
    else:
        reset_buffer(chat_id, context)


ALBUM_COLLECT_SEC = 1.5


async def album_flush(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    mg_id = data["media_group_id"]
    buf = lot_buffer.get(chat_id, {})
    mg = buf.get("media_group_pending", {}).pop(mg_id, None)
    if not mg:
        return
    photos = mg.get("photos", [])
    if photos:
        buf["photos"].extend(photos)
        logger.info(f"Album {mg_id} flushed: {len(photos)} photos")
        restart_idle_timer(chat_id, context)


async def call_vsegpt(content_parts: list) -> str:
    """Вызов vsegpt.ru API (OpenAI-совместимый формат)."""
    headers = {
        "Authorization": f"Bearer {VSEGPT_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": VSEGPT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_APPRAISAL},
            {"role": "user", "content": content_parts},
        ],
        "max_tokens": 1500,
        "temperature": 0.3,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(VSEGPT_URL, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def process_lot(chat_id: int, context: ContextTypes.DEFAULT_TYPE, trigger: str = "done"):
    buf = lot_buffer.get(chat_id, {})
    photos = buf.get("photos", [])
    texts = buf.get("texts", [])

    if not photos and not texts:
        try:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Буфер пустой.")
        except: pass
        return

    lot_num = get_lot_number()
    logger.info(f"Lot #{lot_num}: {len(photos)} photos, {len(texts)} texts")

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Обрабатываю лот #{lot_num}... (~30 сек)",
        )
    except Exception as e:
        logger.warning(f"Notify failed: {e}")

    # Формируем content для OpenAI-формата
    text_prompt = "Проведи оценку автомобиля по описанию.\n\n"
    if texts:
        text_prompt += "Описание от продавца:\n" + "\n".join(texts) + "\n\n"
    if photos:
        text_prompt += f"(Прикреплено {len(photos)} фото, анализируй по описанию)\n\n"
    text_prompt += "Выдай оценку в JSON формате."

    content_parts = [{"type": "text", "text": text_prompt}]

    reset_buffer(chat_id, context)

    try:
        raw = await call_vsegpt(content_parts)
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            raise ValueError("No JSON in response")
        result = json.loads(match.group(0))
        report = build_report(result, lot_num)

        await context.bot.send_message(
            chat_id=OUTPUT_CHAT_ID,
            text=report,
            parse_mode="Markdown",
        )

        price_str = fmt(result["price_rub"]) if result.get("price_rub") else "—"
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"✅ Лот #{lot_num} отправлен! 💰 {price_str}",
            )
        except: pass

    except Exception as e:
        logger.error(f"Error lot #{lot_num}: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Ошибка при обработке лота #{lot_num}. Попробуйте /done ещё раз.",
            )
        except: pass


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    msg = update.message
    if not msg or not msg.photo:
        return

    file = await msg.photo[-1].get_file()
    bio = BytesIO()
    await file.download_to_memory(bio)
    b64 = base64.b64encode(bio.getvalue()).decode()

    mg_id = msg.media_group_id
    if mg_id:
        buf = ensure_buffer(SOURCE_CHAT_ID)
        pending = buf["media_group_pending"]
        if mg_id not in pending:
            job = context.job_queue.run_once(
                album_flush, when=ALBUM_COLLECT_SEC,
                data={"chat_id": SOURCE_CHAT_ID, "media_group_id": mg_id},
                name=f"album_{mg_id}",
            )
            pending[mg_id] = {"photos": [], "job": job}
        pending[mg_id]["photos"].append(b64)
    else:
        buf = ensure_buffer(SOURCE_CHAT_ID)
        buf["photos"].append(b64)
        restart_idle_timer(SOURCE_CHAT_ID, context)
        # Сразу запускаем оценку если есть хоть одно фото
        await process_lot(SOURCE_CHAT_ID, context, trigger="photo")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    msg = update.message
    if not msg or not msg.text:
        return
    text = msg.text.strip()
    if text.startswith("/"):
        return
    normalized = text.lower().rstrip("!.,")
    if normalized in DONE_TRIGGERS:
        await process_lot(SOURCE_CHAT_ID, context, trigger="text_trigger")
        return
    buf = ensure_buffer(SOURCE_CHAT_ID)
    buf["texts"].append(text)
    restart_idle_timer(SOURCE_CHAT_ID, context)


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    await process_lot(SOURCE_CHAT_ID, context, trigger="command")


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    buf = lot_buffer.get(SOURCE_CHAT_ID, {})
    await update.message.reply_text(
        f"📊 Буфер:\n"
        f"📸 Фото: {len(buf.get('photos', []))}\n"
        f"📝 Текстов: {len(buf.get('texts', []))}\n"
        f"⏱ Таймер: {'активен' if buf.get('timer_job') else 'нет'}",
    )


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    reset_buffer(SOURCE_CHAT_ID, context)
    await update.message.reply_text("🗑 Буфер очищен.")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("done", handle_done))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("cancel", handle_cancel))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info(f"Bot v3 started | source={SOURCE_CHAT_ID} | output={OUTPUT_CHAT_ID}")
    app.run_polling(
        drop_pending_updates=True,
        connect_timeout=30,
        read_timeout=30,
        write_timeout=30,
        pool_timeout=30,
    )


if __name__ == "__main__":
    main()
