"""Точка входа: запускает дашборд, планировщик и Telegram-бота."""

import asyncio
import threading
import time

import schedule

import db
from ai_agent import analyze_batch
from bot import get_bot, notify, run_bot, send_top_tenders
from config import check_config
from dashboard import run_dashboard
from logger import get_logger
from scraper_goszakupki import scrape_goszakupki
from scraper_icetrade import scrape_icetrade

log = get_logger("main")


async def run_pipeline():
    """Полный цикл: скрапинг -> сохранение -> анализ -> отправка в Telegram."""
    log.info("=== Запуск пайплайна сбора тендеров ===")

    try:
        tenders_gz = scrape_goszakupki(max_pages=5)
    except Exception as e:
        log.error(f"Ошибка скрапинга goszakupki: {e}")
        tenders_gz = []

    try:
        tenders_ice = scrape_icetrade(max_pages=3)
    except Exception as e:
        log.error(f"Ошибка скрапинга icetrade: {e}")
        tenders_ice = []

    all_new = tenders_gz + tenders_ice
    log.info(f"Получено тендеров со скраперов: {len(all_new)}")

    new_tenders = []
    for tender in all_new:
        try:
            if db.save_tender(tender):
                new_tenders.append(tender)
        except Exception as e:
            log.error(f"Ошибка сохранения тендера {tender.get('id')}: {e}")

    bot = get_bot()

    if not new_tenders:
        log.info("Новых релевантных тендеров не найдено")
        await notify(bot, "🔍 Новых релевантных тендеров не найдено.")
        return

    log.info(f"Новых тендеров для анализа: {len(new_tenders)}")
    analyze_batch(new_tenders)

    await send_top_tenders(bot)
    log.info("=== Пайплайн завершён ===")


def pipeline_thread():
    """Запускает run_pipeline() в новом event loop (для вызова из потока планировщика)."""
    try:
        asyncio.run(run_pipeline())
    except Exception as e:
        log.error(f"Ошибка выполнения пайплайна: {e}")


def scheduler_loop():
    """Бесконечный цикл проверки отложенных задач schedule."""
    while True:
        schedule.run_pending()
        time.sleep(30)


def main():
    if not check_config():
        return

    db.init_db()
    log.info("База данных инициализирована")

    threading.Thread(target=run_dashboard, daemon=True).start()
    log.info("Дашборд запущен на http://localhost:5000")

    threading.Thread(target=pipeline_thread, daemon=True).start()

    schedule.every().hour.do(lambda: threading.Thread(target=pipeline_thread, daemon=True).start())
    threading.Thread(target=scheduler_loop, daemon=True).start()

    run_bot()


if __name__ == "__main__":
    main()
