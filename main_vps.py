"""
VPS entry point: scraper + APScheduler only.
No Flask, no PostgreSQL, no AI agent.

Filters I/B/R in Python, downloads documents, POSTs to Railway.
"""

import base64
import json
import os
import random
import time
from pathlib import Path

import requests
import urllib3
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv

load_dotenv()

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import scraper_goszakupki
import scraper_icetrade
from logger import get_logger

log = get_logger("main_vps")

RAILWAY_URL    = os.getenv("RAILWAY_URL", "").rstrip("/")
INGEST_API_KEY = os.getenv("INGEST_API_KEY", "")
MIN_BUDGET     = float(os.getenv("MIN_BUDGET", "36000"))
REGIONS        = [r.strip() for r in os.getenv("REGIONS", "Минская,г. Минск").split(",") if r.strip()]
KEYWORDS       = [k.strip().lower() for k in os.getenv("VPS_KEYWORDS", "").split(",") if k.strip()]

CHECKPOINT_FILE = Path(__file__).parent / "checkpoint.json"

scheduler = BackgroundScheduler()

_DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
}


# ---------------------------------------------------------------------------
# Checkpoint helpers
# ---------------------------------------------------------------------------

def _load_checkpoint() -> dict:
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_checkpoint(data: dict):
    CHECKPOINT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Document downloader
# ---------------------------------------------------------------------------

def _download_documents(doc_urls: list[str]) -> list[tuple[str, bytes]]:
    results = []
    for url in doc_urls:
        try:
            resp = requests.get(url, headers=_DOWNLOAD_HEADERS, timeout=60, verify=False)
            if resp.status_code == 200:
                filename = url.rsplit("/", 1)[-1].split("?")[0] or "document"
                results.append((filename, resp.content))
            else:
                log.warning(f"Download failed {url}: HTTP {resp.status_code}")
        except Exception as e:
            log.error(f"Download error {url}: {e}")
    return results


# ---------------------------------------------------------------------------
# Railway ingest sender (retry × 3)
# ---------------------------------------------------------------------------

def _send_tender(tender: dict, documents: list[tuple[str, bytes]]) -> bool:
    payload = {
        "external_id": tender["external_id"],
        "source":      tender["source"],
        "title":       tender.get("title"),
        "budget_byn":  tender.get("budget_byn"),
        "region":      tender.get("region"),
        "deadline":    tender.get("deadline"),
        "url":         tender.get("url"),
        "documents": [
            {
                "filename": filename,
                "content":  base64.b64encode(file_bytes).decode("ascii"),
            }
            for filename, file_bytes in documents
        ],
    }

    headers = {
        "X-API-Key":    INGEST_API_KEY,
        "Content-Type": "application/json",
    }
    url = f"{RAILWAY_URL}/api/ingest_tender"

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
            if resp.status_code in (200, 201):
                return True
            log.warning(f"Ingest HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.error(f"Send attempt {attempt + 1} failed: {e}")
        if attempt < 2:
            time.sleep(10)

    return False


# ---------------------------------------------------------------------------
# Main parse-and-send round
# ---------------------------------------------------------------------------

# (source_name, module, checkpoint key, max_pages override)
# goszakupki manages its own bootstrap/incremental page limits internally
# (see FIRST_RUN_MAX_PAGES / SAFETY_MAX_PAGES there) — pass None to let it.
SOURCES = [
    ("goszakupki", scraper_goszakupki, "goszakupki_last_id", None),
    ("icetrade",   scraper_icetrade,   "icetrade_last_id",   10),
]


def parse_and_send():
    log.info("=== Parser round started ===")
    checkpoint = _load_checkpoint()

    for source_name, scraper_module, checkpoint_key, max_pages in SOURCES:
        # Migrate old-style bare-source-name checkpoint keys if present.
        if checkpoint_key not in checkpoint and source_name in checkpoint:
            checkpoint[checkpoint_key] = checkpoint.pop(source_name)

        last_id = checkpoint.get(checkpoint_key)
        log.info(f"{source_name} checkpoint: {last_id or 'none (first run / bootstrap)'}")

        fetch_kwargs = {"checkpoint_external_id": last_id}
        if max_pages is not None:
            fetch_kwargs["max_pages"] = max_pages

        try:
            raw_tenders = scraper_module.fetch_tenders(**fetch_kwargs)
        except Exception as e:
            log.error(f"{source_name} scraper error: {e}")
            continue

        log.info(f"{source_name}: fetched {len(raw_tenders)} tenders")

        skipped_type_count = 0
        passed_count = 0
        for raw in raw_tenders:
            ext_id = raw["external_id"]

            # Tenders the scraper flagged as not real procurements
            # (e.g. goszakupki "marketing" type) — placeholder only, skip.
            if raw.get("_skip"):
                skipped_type_count += 1
                continue

            # Filter I — keyword match
            title = (raw.get("title") or "").lower()
            if KEYWORDS and not any(kw in title for kw in KEYWORDS):
                log.info(f"  → failed_I: {ext_id}")
                continue

            # Filter B — minimum budget
            budget = raw.get("budget_byn") or 0
            if budget < MIN_BUDGET:
                log.info(f"  → failed_B: {ext_id} (budget={budget})")
                continue

            # Filter R — region
            region = raw.get("region")
            if region not in REGIONS:
                log.info(f"  → failed_R: {ext_id} (region={region!r})")
                continue

            passed_count += 1
            log.info(f"  → passed I/B/R: {ext_id}, downloading docs")
            documents = _download_documents(raw.get("doc_urls", []))

            ok = _send_tender(raw, documents)
            log.info(f"  → {'sent' if ok else 'FAILED to send'}: {ext_id}")

        # Advance checkpoint to the newest seen id (first in list = most recent)
        new_checkpoint = raw_tenders[0]["external_id"] if raw_tenders else last_id

        log.info(
            f"{source_name} summary: fetched={len(raw_tenders)}, "
            f"skipped_by_type={skipped_type_count}, passed_I/B/R={passed_count}, "
            f"checkpoint={new_checkpoint or 'none'}"
        )

        if new_checkpoint != last_id:
            checkpoint[checkpoint_key] = new_checkpoint
            _save_checkpoint(checkpoint)

    log.info("=== Parser round complete ===")


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def _parser_job():
    try:
        parse_and_send()
    except Exception as e:
        log.error(f"Parser job error: {e}")
    finally:
        next_secs = random.randint(180, 960)
        log.info(f"Next parse in {next_secs}s ({next_secs // 60}m {next_secs % 60}s)")
        scheduler.reschedule_job("parser", trigger="interval", seconds=next_secs)


def main():
    if not RAILWAY_URL:
        print("[main_vps] RAILWAY_URL not set in .env")
        return
    if not INGEST_API_KEY:
        print("[main_vps] INGEST_API_KEY not set in .env")
        return

    scheduler.add_job(
        _parser_job,
        trigger="interval",
        seconds=10,
        id="parser",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("VPS parser scheduler started")

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        log.info("VPS parser stopped")


if __name__ == "__main__":
    main()
