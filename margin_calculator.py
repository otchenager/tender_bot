"""Margin calculation: fuzzy-match smeta positions against the contractor's price list."""

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
    Compare smeta positions against the price list.

    Each smeta position must have the fields from the target extraction schema:
      num, name, category, section, unit, quantity, total_cost (others optional).

    Returns a dict with:
      positions     — matched rows with diff_byn / diff_pct
      total_tender  — sum of tender costs for matched rows
      total_my_cost — sum of my costs for matched rows
      margin_byn    — total_tender - total_my_cost (None if < MIN_MATCHED)
      margin_pct    — margin_byn / total_my_cost * 100 (None if < MIN_MATCHED)
      matched_count — how many positions were matched
      total_count   — total positions in smeta
    """
    positions = []
    total_tender = 0.0
    total_my_cost = 0.0
    matched_count = 0
    total_count = len(smeta_positions or [])

    for pos in smeta_positions or []:
        name = pos.get("name", "")
        unit = pos.get("unit", "")
        section = pos.get("section", "")
        qty = _to_float(pos.get("quantity"), 0.0)
        total_cost = _to_float(pos.get("total_cost"), 0.0)
        tender_price = total_cost / qty if qty else 0.0

        match = _find_best_match(name, price_items)
        if not match:
            continue

        matched_count += 1
        my_price = _to_float(match.get("my_price"), 0.0)
        my_total = my_price * qty
        diff_byn = total_cost - my_total
        diff_pct = (diff_byn / my_total * 100) if my_total else None

        total_tender += total_cost
        total_my_cost += my_total

        positions.append({
            "name": name,
            "unit": unit,
            "section": section,
            "qty": qty,
            "tender_price": round(tender_price, 2),
            "my_price": my_price,
            "diff_byn": round(diff_byn, 2),
            "diff_pct": round(diff_pct, 2) if diff_pct is not None else None,
        })

    margin_byn = None
    margin_pct = None
    if matched_count >= MIN_MATCHED_FOR_MARGIN:
        margin_byn = round(total_tender - total_my_cost, 2)
        margin_pct = (
            round(margin_byn / total_my_cost * 100, 2) if total_my_cost else None
        )

    return {
        "positions": positions,
        "total_tender": round(total_tender, 2),
        "total_my_cost": round(total_my_cost, 2),
        "margin_byn": margin_byn,
        "margin_pct": margin_pct,
        "matched_count": matched_count,
        "total_count": total_count,
    }
