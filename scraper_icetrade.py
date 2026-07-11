"""
Парсер тендеров с icetrade.by (Белорусская универсальная товарная биржа).

Возвращает стандартизированные словари готовые для db.save_tender().
"""

import re
import time
import random
from datetime import datetime

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from logger import get_logger

log = get_logger("scraper_icetrade")

BASE_URL = "https://icetrade.by"
LIST_URL = f"{BASE_URL}/search/auctions"
CARD_URL_TPL = f"{BASE_URL}/tenders/all/view/{{id}}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Construction + security industries
INDUSTRIES = "407.408-442.20.21.22.23"

REGIONS = [
    "Минская", "Витебская", "Могилёвская", "Гродненская",
    "Брестская", "Гомельская", "г. Минск", "Минск",
]

ACTIVE_STATUSES = ["подача предложений", "подача заявок", "открыт"]

LABEL_MAP = {
    "title":    ["краткое описание", "предмет закупки"],
    "customer": ["наименование организатора", "наименование заказчика"],
    "deadline": ["окончания приема предложений", "окончания приёма"],
    "address":  ["место поставки", "адрес"],
    "region":   ["область", "регион"],
}


def _sleep():
    time.sleep(random.uniform(1, 2.5))


# icetrade.by throws intermittent 403s on the same URL (rate limiting, not a
# hard block — a retry seconds later usually returns 200). Retry a couple of
# times with a growing pause before giving up, so transient 403s don't get
# logged as "page empty" / missing cards.
RETRIES_403 = 2
RETRY_403_BASE_WAIT = 5  # seconds; grows per attempt (5s, 10s)


def _get(url: str, params=None):
    for attempt in range(RETRIES_403 + 1):
        try:
            resp = requests.get(url, headers=HEADERS, params=params, timeout=60, verify=False)
        except requests.RequestException as e:
            log.error(f"Request error {url}: {e}")
            return None
        if resp.status_code == 200:
            return resp
        if resp.status_code == 403 and attempt < RETRIES_403:
            wait = RETRY_403_BASE_WAIT * (attempt + 1)
            log.warning(
                f"HTTP 403 for {url} — rate limited, retry "
                f"{attempt + 1}/{RETRIES_403} in {wait}s"
            )
            time.sleep(wait)
            continue
        log.warning(f"HTTP {resp.status_code} for {url}")
        return None
    return None


def _parse_amount(text: str) -> float | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    try:
        return float(cleaned) if cleaned else None
    except ValueError:
        return None


def _parse_deadline(text: str) -> str | None:
    if not text:
        return None
    text = text.strip()
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{1,2}:\d{2}))?", text)
    if not m:
        return text
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2) or '00:00'}", "%d.%m.%Y %H:%M")
        return dt.isoformat()
    except ValueError:
        return text


def _extract_region(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    for r in REGIONS:
        if r.lower() in t:
            return "г. Минск" if r == "Минск" else r
    return None


def _match_label(label: str) -> str | None:
    label_lower = label.lower()
    for field, keywords in LABEL_MAP.items():
        if any(kw in label_lower for kw in keywords):
            return field
    return None


def _extract_ids_from_page(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for a in soup.select("a[href*='/view/']"):
        m = re.search(r"/view/(\d+)", a.get("href", ""))
        if m and m.group(1) not in ids:
            ids.append(m.group(1))
    return ids


def _parse_card(numeric_id: str) -> dict | None:
    url = CARD_URL_TPL.format(id=numeric_id)
    resp = _get(url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    data: dict = {"documents": [], "url": url}

    for tr in soup.select("table.w100 tr.af"):
        lb = tr.select_one("td.lft")
        vl = tr.select_one("td.afv")
        if not lb or not vl:
            continue
        field = _match_label(lb.get_text(strip=True))
        if field and field not in data:
            data[field] = vl.get_text(" ", strip=True)

    page_text = soup.get_text(" ", strip=True).lower()
    data["_active"] = any(s in page_text for s in ACTIVE_STATUSES)

    # Extract max BYN amount from page text
    amount = None
    for m in re.finditer(r"([\d\s]{3,})\s*byn", page_text):
        candidate = _parse_amount(m.group(1))
        if candidate and (amount is None or candidate > amount):
            amount = candidate
    data["amount"] = amount

    for a in soup.select("a[href$='.pdf'], a[href$='.docx']"):
        href = a.get("href", "")
        if href:
            data["documents"].append(
                href if href.startswith("http") else f"{BASE_URL}{href}"
            )

    return data


def fetch_tenders(checkpoint_external_id: str | None = None,
                  max_pages: int = 5) -> list[dict]:
    """
    Fetch new tenders from icetrade.by.

    Paginates from newest to oldest, stopping when checkpoint_external_id is seen.
    Returns list of standardized tender dicts.
    """
    results = []
    seen = set()

    for page in range(1, max_pages + 1):
        params = {
            "industries": INDUSTRIES,
            "zakup_type[1]": 1,
            "zakup_type[2]": 1,
            "sort": "num:desc",
            "onPage": 20,
            "page": page,
        }
        log.info(f"icetrade page {page}")
        resp = _get(LIST_URL, params=params)
        if resp is None:
            continue

        numeric_ids = _extract_ids_from_page(resp.text)
        if not numeric_ids:
            log.info(f"icetrade page {page} empty, stopping")
            break

        stop = False
        for nid in numeric_ids:
            ext_id = f"ice_{nid}"
            if ext_id in seen:
                continue
            seen.add(ext_id)

            if checkpoint_external_id and ext_id == checkpoint_external_id:
                log.info(f"icetrade reached checkpoint {checkpoint_external_id}, stopping")
                stop = True
                break

            _sleep()
            try:
                card = _parse_card(nid)
            except Exception as e:
                log.error(f"icetrade card error {ext_id}: {e}")
                continue
            if card is None:
                continue
            if not card.get("_active", True):
                continue

            title = card.get("title", "").strip()
            if not title:
                continue

            address = card.get("address", "")
            region = _extract_region(address) or _extract_region(card.get("customer", ""))

            results.append({
                "external_id": ext_id,
                "source": "icetrade",
                "title": title,
                "url": card["url"],
                "region": region,
                "budget_byn": card.get("amount"),
                "deadline": _parse_deadline(card.get("deadline", "")),
                "doc_urls": card.get("documents", []),
            })
            log.info(f"icetrade found: {ext_id} — {title[:60]}")

        if stop:
            break
        _sleep()

    log.info(f"icetrade total fetched: {len(results)}")
    return results
