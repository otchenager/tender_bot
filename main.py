"""
Entry point:
  1. Init PostgreSQL schema
  2. Migrate existing SQLite data (once)
  3. Initialize price list from Vlad's smeta (once, if empty)
  4. Start APScheduler parser loop (random 3-16 min interval)
  5. Start Flask dashboard on port 5000
"""

import random
import time
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

import db
import file_processor
import scraper_goszakupki
import scraper_icetrade
from config import check_config
from dashboard import app
from logger import get_logger

log = get_logger("main")

scheduler = BackgroundScheduler()


# ---------------------------------------------------------------------------
# Parser loop
# ---------------------------------------------------------------------------

def parse_new_tenders():
    """Run one round of scraping + filtering + AI analysis for both sources."""
    log.info("=== Parser round started ===")
    settings = db.get_search_settings()

    for source_name, scraper_module in [
        ("goszakupki", scraper_goszakupki),
        ("icetrade",   scraper_icetrade),
    ]:
        checkpoint = db.get_last_external_id(source_name)
        log.info(f"{source_name} checkpoint: {checkpoint or 'none (full scan)'}")

        try:
            raw_tenders = scraper_module.fetch_tenders(
                checkpoint_external_id=checkpoint,
                max_pages=10,
            )
        except Exception as e:
            log.error(f"{source_name} scraper error: {e}")
            continue

        log.info(f"{source_name}: fetched {len(raw_tenders)} tenders from site")

        for raw in raw_tenders:
            ext_id = raw["external_id"]

            # DEDUPLICATION — by (external_id, source) only
            if db.tender_exists(ext_id, source_name):
                continue

            tender_id = db.save_tender(raw)
            log.info(f"Saved tender {tender_id} ({ext_id})")

            # FILTER I — keyword match (any active price item in title)
            title = (raw.get("title") or "").lower()
            active_keywords = db.get_active_price_item_names()
            if not any(kw.lower() in title for kw in active_keywords):
                db.reject_tender(tender_id, "failed_I")
                log.info(f"  → failed_I (no price item keyword in title)")
                continue

            # FILTER B — minimum budget
            budget = raw.get("budget_byn") or 0
            if budget < settings["min_budget"]:
                db.reject_tender(tender_id, "failed_B")
                log.info(f"  → failed_B (budget {budget} < {settings['min_budget']})")
                continue

            # FILTER R — region
            region = raw.get("region")
            if region not in settings["regions"]:
                db.reject_tender(tender_id, "failed_R")
                log.info(f"  → failed_R (region '{region}' not in {settings['regions']})")
                continue

            log.info(f"  → passed I/B/R, downloading documents and analyzing")

            # Download documents and run AI pipeline
            doc_urls = raw.get("doc_urls", [])
            documents = file_processor.download_documents(doc_urls)
            file_processor.analyze_tender(tender_id, documents)

    log.info("=== Parser round complete ===")


def _parser_job():
    try:
        parse_new_tenders()
    except Exception as e:
        log.error(f"Parser job error: {e}")
    finally:
        # Reschedule with a new random interval
        next_secs = random.randint(180, 960)
        log.info(f"Next parse in {next_secs}s ({next_secs // 60}m {next_secs % 60}s)")
        scheduler.reschedule_job("parser", trigger="interval", seconds=next_secs)


# ---------------------------------------------------------------------------
# Price list initialization from Vlad's smeta
# ---------------------------------------------------------------------------

_SMETA_FILE = Path(__file__).parent / "смет_аВлада.pdf"

_CATEGORY_BY_NUM = {
    **{n: "отделка_потолков"   for n in range(1,  8)},
    **{n: "отделка_стен"       for n in range(8,  24)},
    **{n: "облицовка_стен"     for n in range(24, 32)},
    **{n: "полы"               for n in range(33, 48)},
    **{n: "облицовка_полов"    for n in range(48, 56)},
    **{n: "металлоконструкции" for n in range(56, 61)},
    **{n: "двери_окна"         for n in range(61, 68)},
    **{n: "отопление"          for n in range(68, 78)},
    **{n: "электрика"          for n in range(78, 84)},
    84: "леса",
}


def _init_price_list_from_smeta():
    """Populate price_items once from Vlad's smeta PDF. Skip if table already has data."""
    if db.price_items_count() > 0:
        log.info("Price list already populated, skipping smeta init")
        return

    if not _SMETA_FILE.exists():
        log.warning(f"Vlad's smeta not found at {_SMETA_FILE} — add it and restart to auto-populate")
        return

    log.info(f"Initializing price list from {_SMETA_FILE}")
    try:
        file_bytes = _SMETA_FILE.read_bytes()
        text, is_scan = file_processor._pdf_to_text(file_bytes)
        images = []
        if is_scan:
            images = file_processor._pdf_to_images_b64(file_bytes)

        extraction = file_processor._step1_extract(text, images)
        if not extraction:
            log.error("Failed to extract positions from Vlad's smeta")
            return

        items = []
        for pos in extraction.get("positions", []):
            try:
                num = int(str(pos.get("num", 0)).strip())
            except (TypeError, ValueError):
                num = 0
            qty = float(pos.get("quantity") or 1)
            total = float(pos.get("total_cost") or 0)
            unit_price = total / qty if qty else total
            category = _CATEGORY_BY_NUM.get(num, pos.get("category") or "прочее")
            items.append({
                "name":     pos["name"],
                "unit":     pos.get("unit") or "",
                "my_price": round(unit_price, 2),
                "category": category,
            })

        db.insert_price_items_batch(items)
        log.info(f"Price list initialized with {len(items)} positions from Vlad's smeta")
    except Exception as e:
        log.error(f"Smeta init error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not check_config():
        return

    db.init_db()
    log.info("Database ready")

    _init_price_list_from_smeta()

    # Start parser scheduler (first run after 10s, then random interval)
    scheduler.add_job(
        _parser_job,
        trigger="interval",
        seconds=10,
        id="parser",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("Parser scheduler started")

    log.info("Dashboard starting on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
