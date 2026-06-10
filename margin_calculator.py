"""Расчёт маржинальности тендера на основе позиций сметы и цен заказчика."""

import re
from difflib import SequenceMatcher

MIN_MATCHED_FOR_MARGIN = 3
FUZZY_THRESHOLD = 0.6


def _normalize(name: str) -> str:
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", " ", name, flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _find_best_match(name: str, price_items: list):
    """Находит наиболее похожую позицию из price_items по нечёткому совпадению."""
    best_item = None
    best_score = 0.0

    for item in price_items:
        if item.get("my_price") is None:
            continue
        score = _similarity(name, item.get("name", ""))
        if score > best_score:
            best_score = score
            best_item = item

    if best_item and best_score >= FUZZY_THRESHOLD:
        return best_item
    return None


def _to_float(value, default=0.0):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def calculate_margin(smeta_positions: list, price_items: list) -> dict:
    """
    Сравнивает позиции сметы тендера с ценами заказчика и считает маржу.

    Возвращает словарь со списком сопоставленных позиций и сводными
    показателями маржи. Если совпадений меньше MIN_MATCHED_FOR_MARGIN,
    margin_byn и margin_pct устанавливаются в None.
    """
    positions = []
    total_tender = 0.0
    total_my_cost = 0.0
    matched_count = 0
    total_count = len(smeta_positions or [])

    for pos in smeta_positions or []:
        name = pos.get("name", "")
        unit = pos.get("unit", "")
        qty = _to_float(pos.get("quantity"), 0.0)
        tender_price = _to_float(pos.get("unit_price"), None)
        if tender_price is None:
            total_price = _to_float(pos.get("total_price"), 0.0)
            tender_price = total_price / qty if qty else 0.0

        match = _find_best_match(name, price_items)
        if not match:
            continue

        matched_count += 1
        my_price = _to_float(match.get("my_price"), 0.0)

        tender_total = tender_price * qty
        my_total = my_price * qty
        diff_byn = tender_total - my_total
        diff_pct = (diff_byn / my_total * 100) if my_total else None

        total_tender += tender_total
        total_my_cost += my_total

        positions.append({
            "name": name,
            "unit": unit,
            "qty": qty,
            "tender_price": tender_price,
            "my_price": my_price,
            "diff_byn": round(diff_byn, 2),
            "diff_pct": round(diff_pct, 2) if diff_pct is not None else None,
        })

    margin_byn = None
    margin_pct = None

    if matched_count >= MIN_MATCHED_FOR_MARGIN:
        margin_byn = round(total_tender - total_my_cost, 2)
        margin_pct = round(margin_byn / total_my_cost * 100, 2) if total_my_cost else None

    return {
        "positions": positions,
        "total_tender": round(total_tender, 2),
        "total_my_cost": round(total_my_cost, 2),
        "margin_byn": margin_byn,
        "margin_pct": margin_pct,
        "matched_count": matched_count,
        "total_count": total_count,
    }
