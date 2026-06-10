"""Поиск и извлечение позиций из сметных документов тендера через Claude API."""

import json
import re

import httpx

from config import ANTHROPIC_API_KEY
import db
from logger import get_logger

log = get_logger("smeta_parser")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = "claude-sonnet-4-20250514"

SMETA_PROMPT = (
    "Извлеки все позиции из сметы. Для каждой верни JSON:\n"
    '{"name": ..., "unit": ..., "quantity": ..., "unit_price": ..., "total_price": ...}\n'
    "Отвечай только JSON массивом без пояснений."
)

PRIORITY_1_PATTERNS = ["sm1", "sm2", "смета1", "смета2", "локальная"]
PRIORITY_2_PATTERNS = ["sm", "смета", "свод"]
PRIORITY_3_PATTERNS = ["dogovor", "договор"]


def find_smeta_files(documents: list) -> list:
    """
    Ищет сметные документы среди списка ссылок на файлы.

    Возвращает список словарей: {"url": ..., "priority": int}
    отсортированных по приоритету (1 - локальные сметы, выше всего).
    """
    found = []

    for url in documents or []:
        filename = url.rsplit("/", 1)[-1].lower()

        if any(p in filename for p in PRIORITY_1_PATTERNS):
            found.append({"url": url, "priority": 1})
        elif any(p in filename for p in PRIORITY_2_PATTERNS):
            found.append({"url": url, "priority": 2})
        elif any(p in filename for p in PRIORITY_3_PATTERNS):
            found.append({"url": url, "priority": 3})

    found.sort(key=lambda x: x["priority"])
    return found


def _extract_json_array(text: str):
    """Извлекает JSON-массив из ответа модели (на случай лишнего текста)."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
    return []


def extract_smeta_positions(pdf_url: str) -> list:
    """
    Скачивает PDF по ссылке и отправляет в Claude API для извлечения позиций сметы.

    Возвращает список словарей {name, unit, quantity, unit_price, total_price}.
    При любой ошибке возвращает [].
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0",
        }
        pdf_resp = httpx.get(pdf_url, headers=headers, timeout=60, follow_redirects=True)
        pdf_resp.raise_for_status()
        pdf_bytes = pdf_resp.content
    except Exception as e:
        log.error(f"Не удалось скачать смету {pdf_url}: {e}")
        return []

    try:
        import base64
        pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")

        payload = {
            "model": ANTHROPIC_MODEL,
            "max_tokens": 8000,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": pdf_b64,
                            },
                        },
                        {"type": "text", "text": SMETA_PROMPT},
                    ],
                }
            ],
        }

        headers = {
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        resp = httpx.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        result = resp.json()

        content = result.get("content", [])
        text = ""
        for block in content:
            if block.get("type") == "text":
                text += block.get("text", "")

        positions = _extract_json_array(text)
        if not isinstance(positions, list):
            return []
        return positions

    except Exception as e:
        log.error(f"Ошибка извлечения позиций сметы из {pdf_url}: {e}")
        return []


def process_tender_smeta(tender: dict) -> list:
    """
    Находит сметные документы тендера, извлекает позиции и сохраняет их
    в price_items (увеличивая счётчик встречаемости).

    Возвращает список найденных позиций.
    """
    raw_data = tender.get("raw_data")
    if isinstance(raw_data, str):
        try:
            raw_data = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            raw_data = {}

    documents = (raw_data or {}).get("documents", [])
    smeta_files = find_smeta_files(documents)

    if not smeta_files:
        return []

    all_positions = []
    for smeta_file in smeta_files:
        positions = extract_smeta_positions(smeta_file["url"])
        if positions:
            all_positions.extend(positions)
            break  # достаточно первого найденного документа с приоритетом

    for pos in all_positions:
        name = pos.get("name")
        if name:
            db.update_price_item_occurrence(name)

    return all_positions
