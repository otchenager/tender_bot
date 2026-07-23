"""
VPS entry point: scraper + APScheduler only.
No Flask, no PostgreSQL, no AI agent.

"Dumb" by design: only knows tender TYPE (skip marketing placeholders) and
LOT STATUS (skip inactive/closed lots — checked fresh every scrape, inside
the scraper modules). Every other decision — budget (B), region (R), and
later scoring — is Railway's job, computed live against whatever
search_settings currently say. Every non-skipped tender is POSTed raw to
/api/ingest_raw; Railway tells itself (via tenders_raw) which ones are
worth fetching documents for, and this process polls /api/pending_documents
each cycle to find out.
"""

import base64
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import resource  # POSIX only (VPS runs Linux) — memory instrumentation below
except ImportError:
    resource = None

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

CHECKPOINT_FILE = Path(__file__).parent / "checkpoint.json"

MINSK_TZ = ZoneInfo("Europe/Minsk")
# Real human analysts check tenders more often during Minsk business hours
# and less often overnight — vary the polling interval accordingly instead
# of a flat random range around the clock.
NIGHT_INTERVAL_RANGE = (1800, 3600)  # 23:00-07:00 Minsk
DAY_INTERVAL_RANGE = (180, 960)      # 07:00-23:00 Minsk

scheduler = BackgroundScheduler()

_DOWNLOAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
}


# ---------------------------------------------------------------------------
# Memory instrumentation — investigating a leak that has been triggering the
# OOM killer roughly every 30-40 minutes on the VPS's ~956MB. Logged at the
# start/end of every round and after each source, so per-round and
# per-source growth is visible in the logs without guessing.
# ---------------------------------------------------------------------------

def _rss_mb() -> float | None:
    """Current resident memory in MB. /proc/self/status (Linux, actual
    CURRENT RSS) is preferred; resource.getrusage falls back to the
    process's peak RSS (POSIX, monotonic — never decreases) when /proc
    isn't available."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    if resource is not None:
        try:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except Exception:
            pass
    return None


def _log_rss(label: str):
    rss = _rss_mb()
    if rss is not None:
        log.info(f"RSS {label}: {rss:.1f} MB")


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

def _download_documents(doc_entries: list) -> list[tuple[str, bytes]]:
    """Entries are either plain URL strings (icetrade) or {url, filename}
    dicts (goszakupki get-file links, whose URLs carry no extension — the
    filename comes from the link text). The AI pipeline picks its parser by
    filename extension, so a real name matters."""
    results = []
    for entry in doc_entries:
        if isinstance(entry, dict):
            url, filename = entry.get("url"), entry.get("filename")
        else:
            url, filename = entry, None
        if not url:
            continue
        try:
            resp = requests.get(url, headers=_DOWNLOAD_HEADERS, timeout=60, verify=False)
            if resp.status_code != 200:
                log.warning(f"Download failed {url}: HTTP {resp.status_code}")
                continue
            if not filename:
                cd = resp.headers.get("Content-Disposition", "")
                m = re.search(r"filename\*?=\"?([^\";]+)", cd)
                if m:
                    filename = m.group(1).split("''")[-1].strip()
            if not filename:
                filename = url.rsplit("/", 1)[-1].split("?")[0] or "document"
            results.append((filename, resp.content))
        except Exception as e:
            log.error(f"Download error {url}: {e}")
    return results


# ---------------------------------------------------------------------------
# Railway raw-ingest sender (retry × 3) — every non-skipped tender, no docs.
# Railway decides B/R against live search_settings and stores the verdict in
# tenders_raw; this process never sees that verdict directly (it finds out
# what to fetch documents for via /api/pending_documents instead).
# ---------------------------------------------------------------------------

def _send_raw_tender(tender: dict) -> bool:
    payload = {
        "external_id": tender["external_id"],
        "source":      tender["source"],
        "title":       tender.get("title"),
        "url":         tender.get("url"),
        "region":      tender.get("region"),
        "budget_byn":  tender.get("budget_byn"),
        "deadline":    tender.get("deadline"),
        "tender_type": tender.get("tender_type"),
    }

    headers = {
        "X-API-Key":    INGEST_API_KEY,
        "Content-Type": "application/json",
    }
    url = f"{RAILWAY_URL}/api/ingest_raw"

    for attempt in range(3):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            if resp.status_code in (200, 201):
                return True
            log.warning(f"Ingest_raw HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.error(f"Send_raw attempt {attempt + 1} failed: {e}")
        if attempt < 2:
            time.sleep(5)

    return False


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
            resp = requests.post(url, json=payload, headers=headers, timeout=180)
            if resp.status_code in (200, 201):
                return True
            log.warning(f"Ingest HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            log.error(f"Send attempt {attempt + 1} failed: {e}")
        if attempt < 2:
            time.sleep(10)

    return False


# ---------------------------------------------------------------------------
# Pending document fetches — Railway tells us, via tenders_raw, which raw
# tenders passed live B/R and still need documents fetched + the full
# ingest_tender pipeline. Reuses the existing document downloader and
# _send_tender() as-is, strictly one tender at a time (download → send →
# next; nothing for multiple tenders is ever held in memory simultaneously).
#
# Capped per round (PENDING_FETCH_CAP) because the backlog can arrive faster
# than it drains — a settings loosening or a big first-run raw-ingest batch
# can make dozens of rows "passed" at once. Processing all of them in one
# uninterrupted burst (previously: no cap, no pacing) is what turned a slow
# pre-existing memory creep into an OOM every ~90-100s: BeautifulSoup trees
# hold circular parent/child references, so they're only reclaimed by
# Python's cyclic GC, not plain refcounting — a tight burst of large-page
# parses can out-run that collector. Capping + the same _sleep() pacing the
# normal scrape loop already uses between card fetches fixes both the spike
# and (as a side effect) avoids hammering the source site. The backlog just
# waits in tenders_raw with status='passed' in the meantime — nothing is
# lost, it drains a bit more each round.
# ---------------------------------------------------------------------------

PENDING_FETCH_CAP = 15


def _fetch_and_send_pending_documents():
    for source_name, scraper_module, _checkpoint_key, _max_pages in SOURCES:
        try:
            resp = requests.get(
                f"{RAILWAY_URL}/api/pending_documents",
                params={"source": source_name},
                headers={"X-API-Key": INGEST_API_KEY},
                timeout=60,
            )
            if resp.status_code != 200:
                log.warning(f"pending_documents HTTP {resp.status_code} for {source_name}")
                continue
            pending = resp.json().get("pending", [])
        except Exception as e:
            log.error(f"pending_documents fetch error for {source_name}: {e}")
            continue

        if not pending:
            continue

        backlog_total = len(pending)
        pending = pending[:PENDING_FETCH_CAP]
        log.info(
            f"{source_name}: {backlog_total} pending document fetch(es) "
            f"(processing {len(pending)} this round)"
        )

        # goszakupki's detail-card parser needs a session bootstrapped via
        # the homepage first (session-cookie flow) — same as fetch_tenders().
        session = None
        if source_name == "goszakupki":
            session = requests.Session()
            home = scraper_goszakupki._get(session, scraper_goszakupki.BASE_URL + "/")
            if home is None or home == "STOP":
                log.error(f"{source_name}: failed session bootstrap for pending doc fetch")
                continue

        for row in pending:
            ext_id = row["external_id"]
            url = row.get("url")
            if not url:
                continue

            # Same per-card pacing the normal scrape loop uses — this loop
            # previously had none, firing every card fetch back-to-back.
            scraper_module._sleep()

            try:
                if source_name == "goszakupki":
                    card = scraper_goszakupki._parse_card(session, url)
                    if card == "STOP":
                        log.warning(f"{source_name}: STOP mid pending-fetch (CAPTCHA/403), abandoning rest of backlog this round")
                        break
                    doc_urls = card.get("documents", []) if isinstance(card, dict) else []
                else:
                    numeric_id = ext_id.split("_", 1)[-1]
                    card = scraper_icetrade._parse_card(numeric_id)
                    doc_urls = card.get("documents", []) if card else []
            except Exception as e:
                log.error(f"pending doc card fetch error {ext_id}: {e}")
                continue

            documents = _download_documents(doc_urls)
            ok = _send_tender(row, documents)
            log.info(f"  → pending doc {'sent' if ok else 'FAILED to send'}: {ext_id}")
            # documents/card go out of scope here, before the next iteration
            # starts downloading the next tender's files.


# ---------------------------------------------------------------------------
# Freshness revalidation — a tender's active/closed status on the source
# site can change after we've already stored it (and possibly shown it to
# the contractor as 'suitable'). Only runs every REVALIDATE_EVERY_N_ROUNDS
# parser cycles: unlike raw-ingest/pending-fetch, staleness here is a
# slow-moving concern (deadlines are days/weeks out), so hourly-ish
# checking is plenty and keeps this extra detail-page traffic light on top
# of the normal scrape + pending-fetch load. Capped and paced the same way
# as the pending-document fetch, for the same reasons (see above).
# ---------------------------------------------------------------------------

REVALIDATE_EVERY_N_ROUNDS = 10
REVALIDATE_CAP = 15


def revalidate_active_tenders():
    try:
        resp = requests.get(
            f"{RAILWAY_URL}/api/tenders_to_revalidate",
            headers={"X-API-Key": INGEST_API_KEY},
            timeout=60,
        )
        if resp.status_code != 200:
            log.warning(f"tenders_to_revalidate HTTP {resp.status_code}")
            return
        candidates = resp.json().get("tenders", [])
    except Exception as e:
        log.error(f"tenders_to_revalidate fetch error: {e}")
        return

    if not candidates:
        return

    backlog_total = len(candidates)
    candidates = candidates[:REVALIDATE_CAP]
    log.info(f"revalidation: {backlog_total} candidate(s) (checking {len(candidates)} this round)")

    goszakupki_session = None

    for row in candidates:
        source_name = row.get("source")
        ext_id = row.get("external_id")
        url = row.get("url")
        if not ext_id or not url:
            continue

        try:
            if source_name == "goszakupki":
                if goszakupki_session is None:
                    goszakupki_session = requests.Session()
                    home = scraper_goszakupki._get(goszakupki_session, scraper_goszakupki.BASE_URL + "/")
                    if home is None or home == "STOP":
                        log.error("revalidation: goszakupki session bootstrap failed, abandoning goszakupki rows this round")
                        goszakupki_session = False
                if goszakupki_session is False:
                    continue

                scraper_goszakupki._sleep()
                card = scraper_goszakupki._parse_card(goszakupki_session, url)
                if card == "STOP":
                    log.warning("revalidation: STOP mid-batch (CAPTCHA/403), abandoning rest of backlog this round")
                    break
                if not isinstance(card, dict):
                    continue
                still_active = bool(card.get("_active_lot", True))

            elif source_name == "icetrade":
                scraper_icetrade._sleep()
                numeric_id = ext_id.split("_", 1)[-1]
                card = scraper_icetrade._parse_card(numeric_id)
                if not card:
                    continue
                still_active = bool(card.get("_active", True))

            else:
                continue
        except Exception as e:
            log.error(f"revalidation card fetch error {ext_id}: {e}")
            continue

        checked_at = datetime.now(timezone.utc).isoformat()
        ok = _send_freshness_update(ext_id, source_name, still_active, checked_at)
        log.info(
            f"  → revalidated {ext_id}: still_active={still_active} "
            f"({'sent' if ok else 'FAILED to send'})"
        )


def _send_freshness_update(external_id: str, source: str, still_active: bool, checked_at: str) -> bool:
    payload = {
        "external_id": external_id,
        "source": source,
        "still_active": still_active,
        "checked_at": checked_at,
    }
    headers = {"X-API-Key": INGEST_API_KEY, "Content-Type": "application/json"}
    try:
        resp = requests.post(
            f"{RAILWAY_URL}/api/update_tender_freshness",
            json=payload, headers=headers, timeout=30,
        )
        if resp.status_code in (200, 201):
            return True
        log.warning(f"update_tender_freshness HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        log.error(f"update_tender_freshness error for {external_id}: {e}")
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


_round_counter = 0


def parse_and_send():
    global _round_counter
    log.info("=== Parser round started ===")
    _log_rss("at round start")
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
        sent_count = 0
        for raw in raw_tenders:
            ext_id = raw["external_id"]

            # Tenders the scraper flagged as not real procurements
            # (e.g. goszakupki "marketing" type) — placeholder only, skip.
            # Lot-status filtering (inactive/closed) already happened inside
            # the scraper itself, checked fresh this cycle.
            if raw.get("_skip"):
                skipped_type_count += 1
                continue

            sent_count += 1
            ok = _send_raw_tender(raw)
            log.info(f"  → raw {'sent' if ok else 'FAILED to send'}: {ext_id}")

        # Advance checkpoint to the newest seen id (first in list = most recent)
        new_checkpoint = raw_tenders[0]["external_id"] if raw_tenders else last_id

        log.info(
            f"{source_name} summary: fetched={len(raw_tenders)}, "
            f"skipped_by_type={skipped_type_count}, sent_raw={sent_count}, "
            f"checkpoint={new_checkpoint or 'none'}"
        )

        if new_checkpoint != last_id:
            checkpoint[checkpoint_key] = new_checkpoint
            _save_checkpoint(checkpoint)

        _log_rss(f"after {source_name}")

    _fetch_and_send_pending_documents()
    _log_rss("after pending-document fetch")

    _round_counter += 1
    if _round_counter % REVALIDATE_EVERY_N_ROUNDS == 0:
        revalidate_active_tenders()
        _log_rss("after revalidation")

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
        minsk_hour = datetime.now(MINSK_TZ).hour
        is_night = minsk_hour >= 23 or minsk_hour < 7
        low, high = NIGHT_INTERVAL_RANGE if is_night else DAY_INTERVAL_RANGE
        next_secs = random.randint(low, high)
        log.info(
            f"Interval mode: {'night' if is_night else 'day'} (Minsk hour={minsk_hour}) — "
            f"next parse in {next_secs}s ({next_secs // 60}m {next_secs % 60}s)"
        )
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
