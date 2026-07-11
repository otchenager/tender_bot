"""
Парсер тендеров с goszakupki.by.

ВАЖНО: сайт доступен только с белорусского IP.
Категория содержит ~20 000+ страниц — полное сканирование запрещено.
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

import alerts
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

# Pagination safety limits — the category has 20k+ pages, we must never
# crawl blindly. First-ever run bootstraps with a shallow scan; every
# subsequent run walks forward from page 1 until it hits the checkpoint.
FIRST_RUN_MAX_PAGES = 20
SAFETY_MAX_PAGES = 30

ACTIVE_LOT_STATUSES = ["подача предложений", "подача документов/сведений"]

# Tender type (taken from the URL path segment before /view/{id}).
# marketing = market research survey, no real budget/documents — skip entirely.
# etrade/limited = real competitions, high priority. single-source/request = normal.
TENDER_TYPE_PRIORITY = {
    "etrade": "high",
    "limited": "high",
    "single-source": "normal",
    "request": "normal",
    "marketing": "skip",
}
SKIP_TENDER_TYPES = {t for t, p in TENDER_TYPE_PRIORITY.items() if p == "skip"}

LABEL_MAP = {
    "title":    ["название"],
    "customer": ["наименование организации", "наименование заказчика"],
    "deadline": ["окончания сведений", "окончания предложений", "окончания документов"],
    "amount":   ["предельная стоимость"],
    "address":  ["адрес", "место нахождения"],
}

# Region is derived from the customer/address text on the detail page,
# not a standalone field. First match wins, order matters (e.g. "минская"
# would otherwise match the "минск" substring inside "г. минск" checks).
REGION_KEYWORDS = [
    (("г. минск", "г.минск"), "г. Минск"),
    (("минская обл", "минск обл"), "Минская"),
    (("витебская",), "Витебская"),
    (("могилевская", "могилёвская"), "Могилевская"),
    (("гродненская",), "Гродненская"),
    (("брестская",), "Брестская"),
    (("гомельская",), "Гомельская"),
]


def _sleep():
    time.sleep(random.uniform(1, 2.5))


# Request-layer seam: every HTTP call goes through `transport` so a rotating
# proxy pool can be plugged in later by swapping in a same-signature callable —
# parsing logic never talks to `requests` directly. The session argument must
# keep being honored: goszakupki depends on the session-cookie flow.
def _default_transport(url, *, session=None, params=None, headers=None, timeout=60):
    requester = session if session is not None else requests
    return requester.get(url, params=params, headers=headers, timeout=timeout, verify=False)


transport = _default_transport


def _get(session: requests.Session, url: str):
    try:
        resp = transport(url, session=session, headers=HEADERS, timeout=60)
    except requests.RequestException as e:
        log.error(f"Request error {url}: {e}")
        return None
    if resp.status_code == 403:
        log.error("HTTP 403 — need Belarusian IP")
        return "STOP"
    if resp.status_code != 200:
        log.warning(f"HTTP {resp.status_code} for {url}")
        return None
    if alerts.looks_like_captcha(resp.text):
        # Explicitly a CAPTCHA, not an empty page — abort the round and tell
        # the operator; retrying immediately would only feed the challenge.
        alerts.alert_captcha("goszakupki.by", url)
        return "STOP"
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
    for keywords, region in REGION_KEYWORDS:
        if any(kw in t for kw in keywords):
            return region
    return None


# Marker that reliably prefixes customer address lines on detail pages,
# e.g. "Республика Беларусь, г. Минск, 220006, ул.Полевая, д.24".
_ADDRESS_HINT = "республика беларусь"

# Detail pages ALSO carry the platform operator's own requisites block
# ("Национальный центр поддержки экспорта", Minsk, info@goszakupki.by) —
# and it comes BEFORE the customer block in document order. Verified live
# on /etrade/view/3512202 (2026-07-11). Blocks mentioning the platform's
# domain are the operator's, never the customer's — skip them, otherwise
# every tender in the country would be labeled "г. Минск".
_OPERATOR_MARKER = "goszakupki.by"


def _find_address_block(soup: BeautifulSoup) -> str | None:
    """Find the CUSTOMER address on a detail page without relying on labels.

    On real detail pages (e.g. /etrade/view/3512202) the address sits in a
    plain <td> with no adjacent label cell, so the LABEL_MAP th/td scan never
    sees it. Take the first short Belarusian-address-looking block that is
    not the platform operator's requisites.
    """
    for tag in soup.find_all(["td", "li", "p"]):
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 300:
            continue
        low = text.lower()
        if _ADDRESS_HINT in low and _OPERATOR_MARKER not in low:
            return text
    return None


def _extract_region_from_page(page_text: str | None) -> str | None:
    """Last-resort region scan: keyword within 250 chars after any
    "Республика Беларусь" mention. Deliberately NOT a whole-page keyword
    scan — the site chrome/footer mentions Minsk and would mislabel every
    tender as "г. Минск". Windows containing the operator's domain are the
    platform's own requisites block — skipped (see _OPERATOR_MARKER).
    Within a window the keyword CLOSEST to the address marker wins (an
    address reads left to right: the region comes right after "Республика
    Беларусь", anything further away is unrelated page text)."""
    if not page_text:
        return None
    low = page_text.lower()
    for m in re.finditer(_ADDRESS_HINT, low):
        window = low[m.end():m.end() + 250]
        if _OPERATOR_MARKER in window:
            continue
        best_pos, best_region = None, None
        for keywords, region in REGION_KEYWORDS:
            for kw in keywords:
                pos = window.find(kw)
                if pos != -1 and (best_pos is None or pos < best_pos):
                    best_pos, best_region = pos, region
        if best_region:
            return best_region
    return None


def _parse_href(href: str):
    """Return (external_id, tender_type) parsed from a real detail-page href.

    Real formats seen on site: /etrade/view/{id}, /single-source/view/{id},
    /limited/view/{id}, /marketing/view/{id}, /request/view/{id}.
    """
    m = re.search(r"/([a-z\-]+)/view/(\d+)", href)
    if not m:
        return None, None
    tender_type = m.group(1)
    external_id = f"{tender_type.replace('-', '_')}_{m.group(2)}"
    return external_id, tender_type


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
        external_id, tender_type = _parse_href(href)
        if not external_id:
            continue
        # Detail URL comes straight from the row's actual <a href>, never
        # constructed manually — the path segment (etrade/limited/single-source/...)
        # varies per tender and can't be guessed.
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        cells = row.find_all("td")
        amount_text = cells[-1].get_text(strip=True) if cells else ""
        deadline_text = cells[-2].get_text(strip=True) if len(cells) >= 2 else ""
        rows.append({
            "external_id": external_id,
            "tender_type": tender_type,
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

    # Address fallback: the label-based scan above misses addresses that sit
    # in an unlabeled <td> (the common case on real detail pages).
    if not data.get("address"):
        block = _find_address_block(soup)
        if block:
            data["address"] = block

    # Raw page text kept for the last-resort region scan and for debuggable
    # logging when nothing matches (never log bare empty strings).
    data["_page_text"] = soup.get_text(" ", strip=True)

    # Documents: real attachments live at /etrade/get-file/{id}?c=detail&f=N
    # — no file extension in the href, the real filename is the link text
    # (verified live on /etrade/view/3512202). Suffix-matched .pdf/.docx
    # links on detail pages are site chrome (regulations, privacy policy),
    # NOT tender documents — sending those to the AI produced ai_error.
    # Bare get-file URLs return JSON metadata; the file bytes come only
    # with &download=1 (verified live: metadata 131 bytes vs %PDF payload).
    for a in soup.select("a[href*='get-file']"):
        href = a.get("href", "")
        if not href:
            continue
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        url += "&download=1" if "?" in url else "?download=1"
        data["documents"].append({
            "url": url,
            "filename": a.get_text(strip=True) or None,
        })

    # Parse amount if still a string
    if isinstance(data.get("amount"), str):
        data["amount"] = _parse_amount(data["amount"])

    return data


def fetch_tenders(checkpoint_external_id: str | None = None,
                  max_pages: int | None = None) -> list[dict]:
    """
    Fetch new tenders from goszakupki.by.

    Incremental checkpoint scan: paginates from newest to oldest (page 1, 2, 3...)
    and stops as soon as checkpoint_external_id is seen — everything after that
    point is already known. With no checkpoint (first run ever), bootstraps with
    a shallow scan of FIRST_RUN_MAX_PAGES pages instead of crawling everything.

    A hard SAFETY_MAX_PAGES cap always applies so a missing/stale checkpoint can
    never trigger a full 20k-page crawl.

    Returns a list of standardized tender dicts. Tenders of a skip-listed type
    (currently "marketing") are included only as minimal placeholders so the
    newest-id checkpoint still advances correctly — callers should skip any
    entry whose "tender_type" is in a skip type before further processing.
    """
    results = []
    seen = set()
    skipped_by_type: dict[str, int] = {}

    session = requests.Session()
    home_resp = _get(session, BASE_URL + "/")
    if home_resp == "STOP":
        return results
    if home_resp is None:
        log.error("goszakupki: failed to establish session via homepage, aborting")
        return results

    is_first_run = not checkpoint_external_id
    page_limit = FIRST_RUN_MAX_PAGES if is_first_run else SAFETY_MAX_PAGES
    if max_pages is not None:
        page_limit = min(page_limit, max_pages)

    log.info(
        "goszakupki run mode: "
        f"{'first run / bootstrap' if is_first_run else f'incremental from checkpoint {checkpoint_external_id}'}"
        f", page_limit={page_limit}"
    )

    newest_id = None
    hit_checkpoint = False
    aborted = False
    last_page = 0

    for page in range(1, page_limit + 1):
        last_page = page
        url = LIST_URL if page == 1 else f"{LIST_URL}&page={page}"
        log.info(f"goszakupki page {page}/{page_limit}: {url}")
        resp = _get(session, url)
        if resp == "STOP":
            aborted = True
            break
        if resp is None:
            _sleep()
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        row_infos = _parse_list_page(soup)
        if not row_infos:
            log.info(f"goszakupki page {page} empty, stopping")
            break

        for row in row_infos:
            ext_id = row["external_id"]
            if ext_id in seen:
                continue
            seen.add(ext_id)

            if newest_id is None:
                newest_id = ext_id

            if checkpoint_external_id and ext_id == checkpoint_external_id:
                log.info(f"goszakupki reached checkpoint {checkpoint_external_id} on page {page}, stopping")
                hit_checkpoint = True
                break

            tender_type = row.get("tender_type")
            if tender_type in SKIP_TENDER_TYPES:
                skipped_by_type[tender_type] = skipped_by_type.get(tender_type, 0) + 1
                log.info(f"skipped_type: {ext_id} ({tender_type} - not a real procurement)")
                # Minimal placeholder so the checkpoint still reflects this id.
                results.append({
                    "external_id": ext_id,
                    "source": "goszakupki",
                    "tender_type": tender_type,
                    "title": row.get("title_hint", ""),
                    "url": row["url"],
                    "region": None,
                    "budget_byn": None,
                    "deadline": None,
                    "doc_urls": [],
                    "_skip": True,
                })
                continue

            _sleep()
            card = _parse_card(session, row["url"])
            if card == "STOP":
                aborted = True
                break
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
            customer = card.get("customer", "")
            region = (
                _extract_region(address)
                or _extract_region(customer)
                or _extract_region_from_page(card.get("_page_text"))
            )
            if region is None:
                # Log the raw block we actually looked at so failures are
                # debuggable — an empty-string log line tells us nothing.
                raw = address or customer or card.get("_page_text", "")
                log.warning(
                    f"goszakupki region not matched for {ext_id}: "
                    f"raw_block={raw[:500]!r}"
                )

            results.append({
                "external_id": ext_id,
                "source": "goszakupki",
                "tender_type": tender_type,
                "title": title.strip(),
                "url": row["url"],
                "region": region,
                "budget_byn": amount,
                "deadline": deadline,
                "doc_urls": card.get("documents", []),
            })
            log.info(f"goszakupki found: {ext_id} — {title[:60]} [{tender_type}]")

        if hit_checkpoint or aborted:
            break
        _sleep()

    if not is_first_run and not hit_checkpoint and not aborted and last_page >= page_limit:
        log.warning(
            f"goszakupki hit safety page limit ({page_limit}) without finding checkpoint "
            f"{checkpoint_external_id} — some tenders may have been missed, investigate"
        )

    passed = sum(1 for r in results if not r.get("_skip"))
    skipped_total = sum(skipped_by_type.values())
    log.info(
        f"goszakupki total fetched: {len(results)} "
        f"(passed_to_filters: {passed}, skipped_by_type: {skipped_total} {skipped_by_type})"
    )

    if newest_id:
        log.info(f"goszakupki newest id this run: {newest_id}")

    return results
