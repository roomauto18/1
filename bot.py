import os
import json
import base64
import logging
import re
from io import BytesIO
from datetime import datetime

import anthropic
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
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SOURCE_CHAT_ID    = int(os.environ["SOURCE_CHAT_ID"])
OUTPUT_CHAT_ID    = int(os.environ["OUTPUT_CHAT_ID"])
IDLE_TIMEOUT_MIN  = int(os.environ.get("IDLE_TIMEOUT_MIN", "15"))

# Слова-триггеры окончания лота (регистронезависимо)
DONE_TRIGGERS = {"готово", "готов", "всё", "все", "отправляй", "go", "done", "ок", "ok"}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── STATE ────────────────────────────────────────────────────────────────────
# lot_buffer[chat_id] = {
#   "texts": [...],
#   "photos": [...],          # base64 строки
#   "media_group_pending": {  # склейка альбомов
#       media_group_id: {"photos": [...], "job": job}
#   },
#   "timer_job": job | None
# }
lot_buffer: dict = {}

# ─── SYSTEM PROMPT ────────────────────────────────────────────────────────────
SYSTEM_APPRAISAL = """Ты эксперт-оценщик автомобилей. Тебе передают данные одного автомобиля: фотографии самого автомобиля, фотографии документов (ПТС/СТС), и текстовые описания.

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
  "summary": "Краткое резюме 1–2 предложения для менеджера"
}

Если VIN/номер не распознаётся — сгенерируй правдоподобные.
Отвечай ТОЛЬКО JSON."""


# ─── HELPERS ──────────────────────────────────────────────────────────────────

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
            "texts": [],
            "photos": [],
            "media_group_pending": {},
            "timer_job": None,
        }
    return lot_buffer[chat_id]


def reset_buffer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    buf = lot_buffer.get(chat_id, {})
    if buf.get("timer_job"):
        try:
            buf["timer_job"].schedule_removal()
        except Exception:
            pass
    # Отменяем все pending-задачи альбомов
    for mg in buf.get("media_group_pending", {}).values():
        if mg.get("job"):
            try:
                mg["job"].schedule_removal()
            except Exception:
                pass
    lot_buffer[chat_id] = {
        "texts": [],
        "photos": [],
        "media_group_pending": {},
        "timer_job": None,
    }


def restart_idle_timer(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    buf = ensure_buffer(chat_id)
    if buf.get("timer_job"):
        try:
            buf["timer_job"].schedule_removal()
        except Exception:
            pass
    job = context.job_queue.run_once(
        idle_timer_fired,
        when=IDLE_TIMEOUT_MIN * 60,
        data=chat_id,
        name=f"idle_{chat_id}",
    )
    buf["timer_job"] = job


async def idle_timer_fired(context: ContextTypes.DEFAULT_TYPE):
    chat_id = context.job.data
    buf = lot_buffer.get(chat_id, {})
    if buf.get("photos") or buf.get("texts"):
        logger.info(f"Idle timer fired for {chat_id}, processing lot...")
        await process_lot(chat_id, context, trigger="timer")
    else:
        reset_buffer(chat_id, context)


# ─── ALBUM HANDLING ───────────────────────────────────────────────────────────
# Telegram отправляет фото из одного альбома как N отдельных update-ов
# с одинаковым message.media_group_id.
# Собираем их в течение 1.5 сек, затем пачкой добавляем в буфер лота.

ALBUM_COLLECT_SEC = 1.5


async def album_flush(context: ContextTypes.DEFAULT_TYPE):
    """Вызывается через 1.5 сек после первого фото альбома."""
    data = context.job.data          # {"chat_id": ..., "media_group_id": ...}
    chat_id = data["chat_id"]
    mg_id   = data["media_group_id"]

    buf = lot_buffer.get(chat_id, {})
    mg  = buf.get("media_group_pending", {}).pop(mg_id, None)
    if not mg:
        return

    photos = mg.get("photos", [])
    if photos:
        buf["photos"].extend(photos)
        logger.info(f"Album {mg_id} flushed: {len(photos)} photos → lot buffer now {len(buf['photos'])} total")
        restart_idle_timer(chat_id, context)


# ─── CORE: PROCESS LOT ────────────────────────────────────────────────────────

async def process_lot(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    trigger: str = "done",
):
    buf = lot_buffer.get(chat_id, {})
    photos = buf.get("photos", [])
    texts  = buf.get("texts", [])

    if not photos and not texts:
        try:
            await context.bot.send_message(chat_id=chat_id, text="⚠️ Буфер пустой — нечего оценивать.")
        except Exception:
            pass
        return

    lot_num = get_lot_number()
    logger.info(f"Lot #{lot_num}: {len(photos)} photos, {len(texts)} texts, trigger={trigger}")

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⏳ Обрабатываю лот \\#{lot_num}\\.\\.\\. \\(~30 сек\\)",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.warning(f"Notify source failed: {e}")

    content = []
    for b64 in photos[:10]:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })
    if texts:
        content.append({
            "type": "text",
            "text": "Описание от продавца:\n" + "\n".join(texts),
        })
    content.append({
        "type": "text",
        "text": "Проведи оценку автомобиля по всем предоставленным данным.",
    })

    reset_buffer(chat_id, context)

    try:
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=1000,
            system=SYSTEM_APPRAISAL,
            messages=[{"role": "user", "content": content}],
        )
        raw   = response.content[0].text
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
                text=f"✅ Лот #{lot_num} отправлен\\!\n💰 Оценка: {price_str}",
                parse_mode="MarkdownV2",
            )
        except Exception:
            pass

    except Exception as e:
        logger.error(f"Error on lot #{lot_num}: {e}")
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Ошибка при обработке лота #{lot_num}. Попробуйте /done ещё раз.",
            )
        except Exception:
            pass


# ─── HANDLERS ─────────────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    msg = update.message
    if not msg or not msg.photo:
        return

    # Берём наилучшее качество
    file = await msg.photo[-1].get_file()
    bio  = BytesIO()
    await file.download_to_memory(bio)
    b64 = base64.b64encode(bio.getvalue()).decode()

    mg_id = msg.media_group_id

    if mg_id:
        # Часть альбома — собираем в pending
        buf = ensure_buffer(SOURCE_CHAT_ID)
        pending = buf["media_group_pending"]

        if mg_id not in pending:
            # Первое фото этого альбома — запускаем flush-таймер
            job = context.job_queue.run_once(
                album_flush,
                when=ALBUM_COLLECT_SEC,
                data={"chat_id": SOURCE_CHAT_ID, "media_group_id": mg_id},
                name=f"album_{mg_id}",
            )
            pending[mg_id] = {"photos": [], "job": job}

        pending[mg_id]["photos"].append(b64)
        logger.info(f"Album {mg_id}: collected {len(pending[mg_id]['photos'])} photos so far")
    else:
        # Одиночное фото — сразу в буфер
        buf = ensure_buffer(SOURCE_CHAT_ID)
        buf["photos"].append(b64)
        restart_idle_timer(SOURCE_CHAT_ID, context)
        logger.info(f"Single photo added. Buffer: {len(buf['photos'])} photos")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    if text.startswith("/"):
        return

    # Проверяем триггерные слова
    normalized = text.lower().rstrip("!.,")
    if normalized in DONE_TRIGGERS:
        await process_lot(SOURCE_CHAT_ID, context, trigger="text_trigger")
        return

    buf = ensure_buffer(SOURCE_CHAT_ID)
    buf["texts"].append(text)
    restart_idle_timer(SOURCE_CHAT_ID, context)
    logger.info(f"Text added. Buffer: {len(buf['photos'])} photos, {len(buf['texts'])} texts")


async def handle_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    await process_lot(SOURCE_CHAT_ID, context, trigger="command")


async def handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    buf   = lot_buffer.get(SOURCE_CHAT_ID, {})
    ph    = len(buf.get("photos", []))
    tx    = len(buf.get("texts", []))
    timer = bool(buf.get("timer_job"))
    albums_pending = len(buf.get("media_group_pending", {}))
    await update.message.reply_text(
        f"📊 *Текущий буфер:*\n"
        f"📸 Фото: {ph}\n"
        f"🖼 Альбомов в сборке: {albums_pending}\n"
        f"📝 Текстов: {tx}\n"
        f"⏱ Таймер: {'активен (' + str(IDLE_TIMEOUT_MIN) + ' мин)' if timer else 'не запущен'}",
        parse_mode="Markdown",
    )


async def handle_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != SOURCE_CHAT_ID:
        return
    reset_buffer(SOURCE_CHAT_ID, context)
    await update.message.reply_text("🗑 Буфер очищен. Начинай заново.")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("done",   handle_done))
    app.add_handler(CommandHandler("status", handle_status))
    app.add_handler(CommandHandler("cancel", handle_cancel))
    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info(
        f"Bot v2 started | source={SOURCE_CHAT_ID} | output={OUTPUT_CHAT_ID} "
        f"| timer={IDLE_TIMEOUT_MIN}min | triggers={DONE_TRIGGERS}"
    )
    app.run_polling(drop_pending_updates=True, connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30)


if __name__ == "__main__":
    main()
