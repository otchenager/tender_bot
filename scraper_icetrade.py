"""
Парсер тендеров с icetrade.by (Белорусская универсальная товарная биржа).
"""

import re
import time
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from categories import is_relevant
from logger import get_logger

log = get_logger("scraper_icetrade")

BASE_URL = "https://icetrade.by"
LIST_URL = f"{BASE_URL}/search/auctions"
CARD_URL = f"{BASE_URL}/tenders/all/view/{{id}}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

REQUEST_DELAY = (1, 2)

# 407-442 = Строительство/архитектура и подкатегории
# 20-23 = Безопасность + Видеонаблюдение
INDUSTRIES = "407.408-442.20.21.22.23"

ACTIVE_STATUSES = [
    "подача предложений",
    "подача заявок",
    "открыт",
]

LABEL_MAP = {
    "category": ["отрасль"],
    "title": ["краткое описание", "предмет закупки"],
    "customer": ["наименование организатора", "наименование заказчика"],
    "deadline": ["окончания приема предложений", "окончания приёма"],
    "posted_at": ["дата размещения"],
    "financing": ["источник финансирования"],
}


def _sleep():
    time.sleep(REQUEST_DELAY[0] + (REQUEST_DELAY[1] - REQUEST_DELAY[0]) * 0.5)


def _get(url: str, params=None):
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    except requests.RequestException as e:
        log.error(f"Ошибка запроса {url}: {e}")
        return None

    if resp.status_code != 200:
        log.warning(f"Неожиданный статус {resp.status_code} для {url}")
        return None

    return resp


def _parse_amount(text: str):
    if not text:
        return None
    cleaned = re.sub(r"[^\d.,]", "", text).replace(",", ".")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_deadline(text: str):
    if not text:
        return None
    text = text.strip()
    match = re.search(r"(\d{2}\.\d{2}\.\d{4})(?:\s+(\d{1,2}:\d{2}))?", text)
    if not match:
        return text
    date_part = match.group(1)
    time_part = match.group(2) or "00:00"
    try:
        dt = datetime.strptime(f"{date_part} {time_part}", "%d.%m.%Y %H:%M")
        return dt.isoformat()
    except ValueError:
        return text


def _match_label(label: str):
    label_lower = label.lower()
    for field, keywords in LABEL_MAP.items():
        for kw in keywords:
            if kw in label_lower:
                return field
    return None


def _extract_ids_from_list(html: str):
    """Извлекает числовые ID тендеров из страницы списка."""
    soup = BeautifulSoup(html, "html.parser")
    ids = []
    for a in soup.select("a[href*='/view/']"):
        href = a.get("href", "")
        match = re.search(r"/view/(\d+)", href)
        if match:
            tid = match.group(1)
            if tid not in ids:
                ids.append(tid)
    return ids


def _parse_card(numeric_id: str):
    url = CARD_URL.format(id=numeric_id)
    resp = _get(url)
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    data = {"documents": []}

    type_tag = soup.select_one("table.w100 tr.fst b")
    if type_tag:
        data["tender_type"] = type_tag.get_text(strip=True)

    for tr in soup.select("table.w100 tr.af"):
        label_td = tr.select_one("td.lft")
        value_td = tr.select_one("td.afv")
        if not label_td or not value_td:
            continue
        label = label_td.get_text(strip=True)
        value = value_td.get_text(" ", strip=True)
        field = _match_label(label)
        if field and field not in data:
            data[field] = value

    # Поиск цены и статуса лотов
    page_text = soup.get_text(" ", strip=True).lower()
    is_active = any(s in page_text for s in ACTIVE_STATUSES)
    data["_active"] = is_active

    amount = None
    for m in re.finditer(r"([\d\s]{3,})\s*byn", page_text):
        candidate = _parse_amount(m.group(1))
        if candidate and (amount is None or candidate > amount):
            amount = candidate
    data["amount"] = amount

    # Документы
    for a in soup.select("a[href$='.pdf'], a[href$='.docx']"):
        href = a.get("href", "")
        if href:
            doc_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            data["documents"].append(doc_url)

    data["url"] = url
    return data


def scrape_icetrade(max_pages: int = 3) -> list:
    """
    Скрапит тендеры с icetrade.by.

    Возвращает список словарей, готовых для db.save_tender().
    """
    results = []
    seen_ids = set()

    for page in range(1, max_pages + 1):
        params = {
            "industries": INDUSTRIES,
            "zakup_type[1]": 1,
            "zakup_type[2]": 1,
            "sort": "num:desc",
            "onPage": 20,
            "page": page,
        }
        log.info(f"Загрузка списка icetrade, страница {page}")
        resp = _get(LIST_URL, params=params)
        if resp is None:
            continue

        ids = _extract_ids_from_list(resp.text)
        if not ids:
            log.info(f"Страница {page} icetrade пуста, останавливаемся")
            break

        for numeric_id in ids:
            if numeric_id in seen_ids:
                continue
            seen_ids.add(numeric_id)

            tender_id = f"ice_{numeric_id}"

            try:
                card = _parse_card(numeric_id)
            except Exception as e:
                log.error(f"Ошибка парсинга карточки ice_{numeric_id}: {e}")
                continue

            _sleep()

            if card is None:
                continue

            if not card.get("_active", True):
                continue

            title = card.get("title", "")
            description = card.get("category", "")

            relevant, matched_group, priority = is_relevant(title, description)
            if not relevant:
                continue

            deadline_iso = _parse_deadline(card.get("deadline", ""))
            posted_at_iso = _parse_deadline(card.get("posted_at", ""))

            tender = {
                "id": tender_id,
                "source": "icetrade",
                "title": title,
                "description": description,
                "amount": card.get("amount"),
                "deadline": deadline_iso,
                "posted_at": posted_at_iso,
                "customer": card.get("customer"),
                "customer_address": None,
                "url": card["url"],
                "category": card.get("category"),
                "matched_group": matched_group,
                "priority": priority,
                "okrb_code": None,
                "financing": card.get("financing"),
                "payment_terms": None,
                "tender_type": card.get("tender_type"),
                "raw_data": card,
            }

            results.append(tender)
            log.info(f"Найден релевантный тендер: {tender_id} - {title[:60]}")

        _sleep()

    log.info(f"icetrade: найдено {len(results)} релевантных тендеров")
    return results


if __name__ == "__main__":
    for t in scrape_icetrade(max_pages=1):
        print(t["id"], t["title"], t["amount"], t["deadline"])
