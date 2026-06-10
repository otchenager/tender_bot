"""Telegram-бот: отправка карточек тендеров и сбор обратной связи."""

import json
from datetime import datetime

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import db
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from logger import get_logger

log = get_logger("bot")

VERDICT_EMOJI = {
    "Перспективный": "🟢",
    "Сомнительный": "🟡",
    "Пропустить": "🔴",
}

REASON_LABELS = {
    "small_amount": "Маленькая сумма",
    "wrong_type": "Не наш вид",
    "unreal_deadline": "Нереальные сроки",
    "unreliable_customer": "Ненадёжный заказчик",
    "strict_requirements": "Жёсткие требования",
    "other": "Другое",
}

LEARNING_THRESHOLD = 10


def _days_left(deadline: str):
    if not deadline:
        return None
    try:
        dt = datetime.fromisoformat(deadline)
    except ValueError:
        return None
    return (dt - datetime.now()).days


def _format_amount(amount):
    if amount is None:
        return "не указана"
    return f"{amount:,.2f}".replace(",", " ").rstrip("0").rstrip(".")


def build_tender_message(tender: dict) -> str:
    """Формирует текст сообщения с карточкой тендера."""
    verdict = tender.get("verdict") or "Сомнительный"
    emoji = VERDICT_EMOJI.get(verdict, "🟡")
    score = tender.get("score")

    days = _days_left(tender.get("deadline"))
    deadline_str = tender.get("deadline") or "не указан"
    if days is not None:
        deadline_str = f"{deadline_str} ({days} дн.)"

    lines = [
        f"{emoji} {verdict} · Оценка: {score}/10",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📋 {tender.get('title')}",
        "",
        f"💰 {_format_amount(tender.get('amount'))} BYN",
        f"📅 Подача до: {deadline_str}",
        f"🏢 {tender.get('customer') or 'не указан'}",
        f"🏗 {tender.get('category') or tender.get('matched_group') or '-'}",
        f"📍 {tender.get('source')}",
    ]

    margin_byn = tender.get("margin_byn")
    margin_pct = tender.get("margin_pct")
    if margin_byn is not None:
        lines.append(f"💵 Маржа: {margin_byn} BYN ({margin_pct}%)")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("🤖 Анализ:")
    lines.append(tender.get("analysis") or "")
    lines.append("")
    lines.append(f"🔗 [Открыть тендер]({tender.get('url')})")

    return "\n".join(lines)


def build_reaction_keyboard(tender_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("👍 Интересно", callback_data=f"fb|{tender_id}|like"),
            InlineKeyboardButton("🤷 Может быть", callback_data=f"fb|{tender_id}|maybe"),
            InlineKeyboardButton("👎 Не интересно", callback_data=f"fb|{tender_id}|dislike"),
        ]
    ])


def build_reason_keyboard(tender_id: str) -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for code, label in REASON_LABELS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"reason|{tender_id}|{code}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def get_bot() -> Bot:
    """Возвращает отдельный экземпляр Bot для отправки сообщений вне polling-цикла."""
    return Bot(token=TELEGRAM_BOT_TOKEN)


async def send_top_tenders(bot):
    """Отправляет топ-3 непросмотренных проанализированных тендера в Telegram."""
    tenders = db.get_unsent_analyzed(limit=3)

    if not tenders:
        log.info("Нет новых проанализированных тендеров для отправки")
        return

    for tender in tenders:
        try:
            text = build_tender_message(tender)
            keyboard = build_reaction_keyboard(tender["id"])

            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )

            db.mark_sent(tender["id"])
            log.info(f"Отправлен тендер {tender['id']} (оценка {tender.get('score')})")

        except Exception as e:
            log.error(f"Ошибка отправки тендера {tender.get('id')}: {e}")


async def notify(bot, text: str):
    """Отправляет произвольное текстовое уведомление в чат."""
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text)
    except Exception as e:
        log.error(f"Ошибка отправки уведомления: {e}")


# ---------------------------------------------------------------------------
# Обработчики команд и callback-кнопок
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    parts = data.split("|")

    if len(parts) != 3:
        return

    kind, tender_id, value = parts

    if kind == "fb":
        if value == "dislike":
            await query.edit_message_reply_markup(
                reply_markup=build_reason_keyboard(tender_id)
            )
            return

        reaction_map = {"like": "👍", "maybe": "🤷"}
        reaction = reaction_map.get(value, value)
        db.save_feedback(tender_id, reaction)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.message.reply_text(f"Спасибо за оценку: {reaction}")

    elif kind == "reason":
        reason_label = REASON_LABELS.get(value, value)
        db.save_feedback(tender_id, "👎", reason=reason_label)

        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.message.reply_text(f"Спасибо за оценку: 👎 ({reason_label})")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stats = db.get_feedback_stats()

    likes = sum(r["count"] for r in stats if r["reaction"] == "👍")
    maybes = sum(r["count"] for r in stats if r["reaction"] == "🤷")
    dislikes = sum(r["count"] for r in stats if r["reaction"] == "👎")
    total = likes + maybes + dislikes

    progress = min(total, LEARNING_THRESHOLD)

    lines = [
        "📊 Статистика обратной связи",
        "",
        f"Всего оценок: {total}",
        f"👍 Интересно: {likes}",
        f"🤷 Может быть: {maybes}",
        f"👎 Не интересно: {dislikes}",
        "",
    ]

    if total >= LEARNING_THRESHOLD:
        lines.append("🧠 Обучение активно: AI учитывает ваши паттерны оценок.")
    else:
        lines.append(f"🧠 Прогресс обучения: {progress}/{LEARNING_THRESHOLD} оценок")

    if dislikes:
        reasons = {}
        for row in stats:
            if row["reaction"] == "👎" and row["reason"]:
                reasons[row["reason"]] = reasons.get(row["reason"], 0) + row["count"]
        if reasons:
            lines.append("")
            lines.append("Топ причин отказа:")
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
                lines.append(f"  • {reason}: {count}")

    await update.message.reply_text("\n".join(lines))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 Тендерный бот\n\n"
        "Каждый час я проверяю goszakupki.by и icetrade.by на новые "
        "релевантные тендеры, анализирую их с помощью AI и присылаю топ-3 "
        "самых перспективных.\n\n"
        "Оцените присланные тендеры кнопками 👍/🤷/👎 - это помогает мне "
        "точнее подбирать тендеры в будущем.\n\n"
        "Команды:\n"
        "/stats - статистика по оценкам и прогресс обучения\n"
        "/help - эта справка\n\n"
        f"Дашборд со всеми тендерами: http://localhost:5000"
    )
    await update.message.reply_text(text)


def build_application() -> Application:
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("stats", cmd_stats))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("start", cmd_help))
    application.add_handler(CallbackQueryHandler(handle_callback))

    return application


def run_bot():
    """Запускает Telegram-бота в режиме polling (блокирующий вызов)."""
    application = build_application()
    log.info("Telegram-бот запущен (polling)")
    application.run_polling(drop_pending_updates=True)
