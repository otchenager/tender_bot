"""
Парсер тендеров с goszakupki.by.

ВАЖНО: сайт доступен только с белорусского IP.
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

log = get_logger("scraper_goszakupki")

BASE_URL = "https://goszakupki.by"
LIST_URL = (
    f"{BASE_URL}/tenders/posted"
    f"?TendersSearch%5Bindustry%5D="
    f"407%2C408%2C409%2C410%2C411%2C412%2C413%2C414%2C415%2C416%2C417%2C418"
    f"%2C419%2C420%2C421%2C422%2C423%2C424%2C425%2C426%2C427%2C428%2C429%2C430"
    f"%2C431%2C432%2C433%2C434%2C435%2C436%2C437%2C438%2C439%2C440%2C441%2C442"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

REGIONS = [
    "Минская", "Витебская", "Могилёвская", "Гродненская",
    "Брестская", "Гомельская", "г. Минск", "Минск",
]

ACTIVE_LOT_STATUSES = ["подача предложений", "подача документов/сведений"]

LABEL_MAP = {
    "title":    ["название"],
    "customer": ["наименование организации", "наименование заказчика"],
    "deadline": ["окончания сведений", "окончания предложений", "окончания документов"],
    "amount":   ["предельная стоимость"],
    "address":  ["адрес", "место нахождения"],
    "region":   ["область", "регион"],
}


def _sleep():
    time.sleep(random.uniform(1, 2.5))


def _get(session: requests.Session, url: str):
    try:
        resp = session.get(url, headers=HEADERS, timeout=60, verify=False)
    except requests.RequestException as e:
        log.error(f"Request error {url}: {e}")
        return None
    if resp.status_code == 403:
        log.error("HTTP 403 — need Belarusian IP")
        return "STOP"
    if resp.status_code != 200:
        log.warning(f"HTTP {resp.status_code} for {url}")
        return None
    return resp


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
    m = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{1,2}:\d{2}))?", text.strip())
    if not m:
        return text.strip()
    try:
        dt = datetime.strptime(f"{m.group(1)} {m.group(2) or '00:00'}", "%d.%m.%Y %H:%M")
        return dt.isoformat()
    except ValueError:
        return text.strip()


def _extract_region(text: str | None) -> str | None:
    if not text:
        return None
    t = text.lower()
    for r in REGIONS:
        if r.lower() in t:
            return "г. Минск" if r == "Минск" else r
    return None


def _build_external_id(href: str) -> str | None:
    m = re.search(r"/([a-z\-]+)/view/(\d+)", href)
    if not m:
        return None
    return f"{m.group(1).replace('-', '_')}_{m.group(2)}"


def _match_label(label: str) -> str | None:
    label_lower = label.lower()
    for field, keywords in LABEL_MAP.items():
        if any(kw in label_lower for kw in keywords):
            return field
    return None


def _parse_list_page(soup: BeautifulSoup):
    table = soup.select_one("table.table.table-hover.table-tds--word-break")
    if not table:
        return []
    rows = []
    for row in table.select("tbody tr[data-key]"):
        link = row.select_one("td.word-break a[href*='/view/']")
        if not link:
            continue
        href = link.get("href", "")
        external_id = _build_external_id(href)
        if not external_id:
            continue
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        cells = row.find_all("td")
        amount_text = cells[-1].get_text(strip=True) if cells else ""
        deadline_text = cells[-2].get_text(strip=True) if len(cells) >= 2 else ""
        rows.append({
            "external_id": external_id,
            "url": url,
            "title_hint": link.get_text(strip=True),
            "amount": _parse_amount(amount_text),
            "deadline": _parse_deadline(deadline_text),
        })
    return rows


def _parse_card(session: requests.Session, url: str) -> dict | None:
    resp = _get(session, url)
    if resp == "STOP":
        return "STOP"
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    data: dict = {"documents": [], "_active_lot": True}

    # h1 fallback title
    h1 = soup.select_one("h1")
    if h1:
        raw = h1.get_text(strip=True)
        for prefix in ["Просмотр конкурса", "Просмотр заявки", "Просмотр запроса",
                       "Просмотр электронного аукциона", "Просмотр процедуры закупки"]:
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        data["title_h1"] = raw

    # Data tables
    for table in soup.select("table.table-striped"):
        for tr in table.select("tbody tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not th or not td:
                continue
            field = _match_label(th.get_text(strip=True))
            if field and field not in data:
                data[field] = td.get_text(" ", strip=True)

    # Lots
    lots_table = soup.select_one("table#lotsList")
    if lots_table:
        active = any(
            any(s in (t.select_one("td.lot-status span.badge") or
                      type("", (), {"get_text": lambda *a, **k: ""})()).get_text(strip=True).lower()
                for s in ACTIVE_LOT_STATUSES)
            for t in lots_table.select("tbody tr.lot-row")
        )
        data["_active_lot"] = active

        earliest = None
        for li in lots_table.select("tr.lot-inf ul.list-group li.list-group-item"):
            lb = li.select_one("b.col-md-6")
            vl = li.select_one("span.col-md-6")
            if lb and vl and _match_label(lb.get_text(strip=True)) == "deadline":
                iso = _parse_deadline(vl.get_text(" ", strip=True))
                if iso and (earliest is None or iso < earliest):
                    earliest = iso
        if earliest:
            data["deadline"] = earliest

    # Documents
    for a in soup.select("a[href$='.pdf'], a[href$='.docx']"):
        href = a.get("href", "")
        if href:
            data["documents"].append(
                href if href.startswith("http") else f"{BASE_URL}{href}"
            )

    # Parse amount if still a string
    if isinstance(data.get("amount"), str):
        data["amount"] = _parse_amount(data["amount"])

    return data


def fetch_tenders(checkpoint_external_id: str | None = None,
                  max_pages: int = 10) -> list[dict]:
    """
    Fetch new tenders from goszakupki.by.

    Paginates from newest to oldest, stopping when checkpoint_external_id is seen.
    Returns list of standardized tender dicts.
    """
    results = []
    seen = set()

    session = requests.Session()
    home_resp = _get(session, BASE_URL + "/")
    if home_resp == "STOP":
        return results
    if home_resp is None:
        log.error("goszakupki: failed to establish session via homepage, aborting")
        return results

    for page in range(1, max_pages + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}&page={page}"
        log.info(f"goszakupki page {page}: {url}")
        resp = _get(session, url)
        if resp == "STOP":
            break
        if resp is None:
            _sleep()
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        row_infos = _parse_list_page(soup)
        if not row_infos:
            log.info(f"goszakupki page {page} empty, stopping")
            break

        stop = False
        for row in row_infos:
            ext_id = row["external_id"]
            if ext_id in seen:
                continue
            seen.add(ext_id)

            if checkpoint_external_id and ext_id == checkpoint_external_id:
                log.info(f"goszakupki reached checkpoint {checkpoint_external_id}, stopping")
                stop = True
                break

            _sleep()
            card = _parse_card(session, row["url"])
            if card == "STOP":
                return results
            if card is None:
                continue
            if not card.get("_active_lot", True):
                continue

            title = card.get("title") or card.get("title_h1") or row.get("title_hint", "")
            if not title or len(title.strip()) < 5:
                continue

            amount = card.get("amount") or row.get("amount")
            deadline = card.get("deadline") or row.get("deadline")
            address = card.get("address", "")
            region = _extract_region(address) or _extract_region(card.get("customer", ""))

            results.append({
                "external_id": ext_id,
                "source": "goszakupki",
                "title": title.strip(),
                "url": row["url"],
                "region": region,
                "budget_byn": amount,
                "deadline": deadline,
                "doc_urls": card.get("documents", []),
            })
            log.info(f"goszakupki found: {ext_id} — {title[:60]}")

        if stop:
            break
        _sleep()

    log.info(f"goszakupki total fetched: {len(results)}")
    return results
