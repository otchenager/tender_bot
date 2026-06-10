"""
Парсер тендеров с goszakupki.by (Республиканский портал государственных закупок).

ВАЖНО: сайт доступен только с белорусского IP. При получении 403 скрипт
выводит сообщение и прекращает работу.
"""

import re
import time
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup

from categories import is_relevant
from logger import get_logger

log = get_logger("scraper_goszakupki")

BASE_URL = "https://goszakupki.by"
LIST_URL = f"{BASE_URL}/tenders/posted"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

REQUEST_DELAY = (1, 2)  # сек, пауза между запросами

ACTIVE_LOT_STATUSES = [
    "подача предложений",
    "подача документов/сведений",
]

MIN_AMOUNT = 5000  # BYN
MIN_DAYS_LEFT = 2

LABEL_MAP = {
    "title": ["название"],
    "category": ["отрасль"],
    "customer": ["наименование организации", "наименование заказчика"],
    "deadline": ["окончания сведений", "окончания предложений", "окончания документов"],
    "amount": ["предельная стоимость"],
    "contact": ["контактные данные", "фамили"],
    "financing": ["финансирования"],
    "okrb_code": ["окрб"],
    "payment_terms": ["способ расчет", "порядок оплат"],
}


def _sleep():
    time.sleep(REQUEST_DELAY[0] + (REQUEST_DELAY[1] - REQUEST_DELAY[0]) * 0.5)


def _get(url: str):
    """Выполняет GET-запрос. При 403 сообщает о необходимости BY IP и возвращает None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        log.error(f"Ошибка запроса {url}: {e}")
        return None

    if resp.status_code == 403:
        print("Запусти с белорусского IP")
        return "STOP"

    if resp.status_code != 200:
        log.warning(f"Неожиданный статус {resp.status_code} для {url}")
        return None

    return resp


def _parse_amount(text: str):
    """Парсит сумму вида '12 345 BYN' -> 12345.0"""
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
    """Пытается распарсить дату из текста в формате dd.mm.yyyy [HH:MM]."""
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


def _days_left(deadline_iso: str):
    if not deadline_iso:
        return None
    try:
        dt = datetime.fromisoformat(deadline_iso)
    except ValueError:
        return None
    return (dt - datetime.now()).total_seconds() / 86400.0


def _build_tender_id(href: str):
    """
    Извлекает tender_id из ссылки вида /single-source/view/3440188
    -> 'single_source_3440188'
    """
    match = re.search(r"/([a-z\-]+)/view/(\d+)", href)
    if not match:
        return None
    section = match.group(1).replace("-", "_")
    tender_num = match.group(2)
    return f"{section}_{tender_num}"


def _list_rows(max_pages: int):
    """Генератор строк таблицы списка тендеров по страницам."""
    for page in range(1, max_pages + 1):
        url = LIST_URL if page == 1 else f"{LIST_URL}?page={page}"
        log.info(f"Загрузка списка тендеров: {url}")
        resp = _get(url)
        if resp == "STOP":
            return
        if resp is None:
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        table = soup.select_one("table.table.table-hover.table-tds--word-break")
        if not table:
            log.warning(f"Таблица тендеров не найдена на странице {page}")
            continue

        rows = table.select("tbody tr[data-key]")
        if not rows:
            log.info(f"Страница {page} пуста, останавливаемся")
            break

        for row in rows:
            yield row

        _sleep()


def _parse_list_row(row):
    """Извлекает базовую информацию (ссылка, сумма, дата) из строки списка."""
    link_tag = row.select_one("td.word-break a[href*='/view/']")
    if not link_tag:
        return None

    href = link_tag.get("href", "")
    tender_id = _build_tender_id(href)
    if not tender_id:
        return None

    url = href if href.startswith("http") else f"{BASE_URL}{href}"

    cells = row.find_all("td")
    if not cells:
        return None

    amount_text = cells[-1].get_text(strip=True)
    deadline_text = cells[-2].get_text(strip=True) if len(cells) >= 2 else ""

    amount = _parse_amount(amount_text)
    deadline_iso = _parse_deadline(deadline_text)

    return {
        "tender_id": tender_id,
        "url": url,
        "title_hint": link_tag.get_text(strip=True),
        "amount": amount,
        "deadline": deadline_iso,
    }


def _match_label(label: str):
    label_lower = label.lower()
    for field, keywords in LABEL_MAP.items():
        for kw in keywords:
            if kw in label_lower:
                return field
    return None


def _parse_card(url: str):
    """Парсит карточку тендера и возвращает словарь с данными."""
    resp = _get(url)
    if resp == "STOP":
        return "STOP"
    if resp is None:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    data = {"documents": []}

    # Заголовок (первая попавшаяся панель)
    title_tag = soup.select_one("div.panel.panel-primary div.panel-heading b")
    if title_tag:
        data["title"] = title_tag.get_text(strip=True)

    # Таблицы данных: th (метка) + td (значение)
    for table in soup.select("table.table-striped"):
        for tr in table.select("tbody tr"):
            th = tr.find("th")
            td = tr.find("td")
            if not th or not td:
                continue
            label = th.get_text(strip=True)
            value = td.get_text(" ", strip=True)
            field = _match_label(label)
            if field and field not in data:
                data[field] = value
            elif field == "title" and not data.get("title"):
                data["title"] = value

    # Лоты
    lots_table = soup.select_one("table#lotsList")
    active_lot_found = False
    earliest_deadline = None

    if lots_table:
        for lot_row in lots_table.select("tbody tr.lot-row"):
            status_tag = lot_row.select_one("td.lot-status span.badge")
            status_text = status_tag.get_text(strip=True).lower() if status_tag else ""
            is_active = any(s in status_text for s in ACTIVE_LOT_STATUSES)
            if is_active:
                active_lot_found = True

        for lot_inf in lots_table.select("tr.lot-inf ul.list-group li.list-group-item"):
            label_tag = lot_inf.select_one("b.col-md-6")
            value_tag = lot_inf.select_one("span.col-md-6")
            if not label_tag or not value_tag:
                continue
            label = label_tag.get_text(strip=True)
            value = value_tag.get_text(" ", strip=True)
            field = _match_label(label)
            if field == "deadline":
                deadline_iso = _parse_deadline(value)
                if deadline_iso and (earliest_deadline is None or deadline_iso < earliest_deadline):
                    earliest_deadline = deadline_iso
            elif field and field not in data:
                data[field] = value
    else:
        active_lot_found = True  # нет таблицы лотов - не блокируем по статусу

    if earliest_deadline:
        data["deadline"] = earliest_deadline

    data["_active_lot_found"] = active_lot_found

    # Документы
    doc_links = soup.select("a[href$='.pdf'], a[href$='.docx']")
    for a in doc_links:
        href = a.get("href", "")
        if href:
            doc_url = href if href.startswith("http") else f"{BASE_URL}{href}"
            data["documents"].append(doc_url)

    if "amount" in data and isinstance(data["amount"], str):
        amount_val = _parse_amount(data["amount"])
        if amount_val is not None:
            data["amount"] = amount_val

    return data


def scrape_goszakupki(max_pages: int = 5) -> list:
    """
    Скрапит тендеры с goszakupki.by.

    Возвращает список словарей, готовых для db.save_tender().
    """
    results = []
    seen_ids = set()

    for row in _list_rows(max_pages):
        try:
            row_info = _parse_list_row(row)
        except Exception as e:
            log.error(f"Ошибка парсинга строки списка: {e}")
            continue

        if not row_info:
            continue

        tender_id = row_info["tender_id"]
        if tender_id in seen_ids:
            continue
        seen_ids.add(tender_id)

        # Пре-фильтр: сумма
        if row_info["amount"] is not None and row_info["amount"] < MIN_AMOUNT:
            continue

        # Пре-фильтр: срок подачи
        days_left = _days_left(row_info["deadline"])
        if days_left is not None and days_left < MIN_DAYS_LEFT:
            continue

        try:
            card = _parse_card(row_info["url"])
        except Exception as e:
            log.error(f"Ошибка парсинга карточки {row_info['url']}: {e}")
            continue

        if card == "STOP":
            return results
        if card is None:
            continue

        if not card.get("_active_lot_found", True):
            continue

        title = card.get("title") or row_info["title_hint"]
        description = card.get("category", "")

        relevant, matched_group, priority = is_relevant(title, description)
        if not relevant:
            continue

        amount = card.get("amount", row_info["amount"])
        deadline = card.get("deadline", row_info["deadline"])

        days_left_final = _days_left(deadline)
        if days_left_final is not None and days_left_final < MIN_DAYS_LEFT:
            continue
        if amount is not None and amount < MIN_AMOUNT:
            continue

        tender = {
            "id": tender_id,
            "source": "goszakupki",
            "title": title,
            "description": description,
            "amount": amount,
            "deadline": deadline,
            "posted_at": card.get("posted_at"),
            "customer": card.get("customer"),
            "customer_address": card.get("customer_address"),
            "url": row_info["url"],
            "category": card.get("category"),
            "matched_group": matched_group,
            "priority": priority,
            "okrb_code": card.get("okrb_code"),
            "financing": card.get("financing"),
            "payment_terms": card.get("payment_terms"),
            "tender_type": card.get("tender_type"),
            "raw_data": card,
        }

        results.append(tender)
        log.info(f"Найден релевантный тендер: {tender_id} - {title[:60]}")

    log.info(f"goszakupki: найдено {len(results)} релевантных тендеров")
    return results


if __name__ == "__main__":
    for t in scrape_goszakupki(max_pages=1):
        print(t["id"], t["title"], t["amount"], t["deadline"])
