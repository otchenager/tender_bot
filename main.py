"""
Railway entry point: Flask dashboard + POST /api/ingest_tender.
No scraper, no APScheduler.
"""

import base64
import os
import threading
from pathlib import Path

from flask import jsonify, request

import db
import file_processor
from config import check_config
from dashboard import app
from logger import get_logger

log = get_logger("main")

INGEST_API_KEY = os.getenv("INGEST_API_KEY", "")


# ---------------------------------------------------------------------------
# Ingest endpoint — called by VPS
# ---------------------------------------------------------------------------

@app.route("/api/ingest_tender", methods=["POST"])
def ingest_tender():
    if not INGEST_API_KEY or request.headers.get("X-API-Key") != INGEST_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    for field in ("external_id", "source"):
        if not data.get(field):
            return jsonify({"error": f"missing field: {field}"}), 400

    source = data["source"]
    ext_id = data["external_id"]

    if db.tender_exists(ext_id, source):
        return jsonify({"status": "duplicate"}), 200

    tender_id = db.save_tender({
        "external_id": ext_id,
        "source":      source,
        "title":       data.get("title"),
        "url":         data.get("url"),
        "region":      data.get("region"),
        "budget_byn":  data.get("budget_byn"),
        "deadline":    data.get("deadline"),
    })

    documents = []
    for doc in data.get("documents", []):
        try:
            filename   = doc.get("filename", "document")
            file_bytes = base64.b64decode(doc["content"])
            documents.append((filename, file_bytes))
        except Exception as e:
            log.warning(f"Failed to decode document: {e}")

    file_processor.analyze_tender(tender_id, documents)
    return jsonify({"status": "ok", "tender_id": tender_id}), 200


# ---------------------------------------------------------------------------
# Price list initialization from Vlad's smeta
# ---------------------------------------------------------------------------

_SMETA_FILE = Path(__file__).parent / "смет_аВлада.pdf"

_CATEGORY_BY_NUM = {
    **{n: "отделка_потолков"   for n in range(1,  8)},
    **{n: "отделка_стен"       for n in range(8,  24)},
    **{n: "облицовка_стен"     for n in range(24, 32)},
    **{n: "полы"               for n in range(33, 48)},
    **{n: "облицовка_полов"    for n in range(48, 56)},
    **{n: "металлоконструкции" for n in range(56, 61)},
    **{n: "двери_окна"         for n in range(61, 68)},
    **{n: "отопление"          for n in range(68, 78)},
    **{n: "электрика"          for n in range(78, 84)},
    84: "леса",
}


def _init_price_list_from_smeta():
    if db.price_items_count() > 0:
        log.info("Price list already populated, skipping smeta init")
        return

    if not _SMETA_FILE.exists():
        log.warning(f"Vlad's smeta not found at {_SMETA_FILE} — add it and restart to auto-populate")
        return

    log.info(f"Initializing price list from {_SMETA_FILE}")
    try:
        file_bytes = _SMETA_FILE.read_bytes()
        text, is_scan = file_processor._pdf_to_text(file_bytes)
        images = file_processor._pdf_to_images_b64(file_bytes) if is_scan else []

        extraction = file_processor._step1_extract(text, images)
        if not extraction:
            log.error("Failed to extract positions from Vlad's smeta")
            return

        items = []
        for pos in extraction.get("positions", []):
            try:
                num = int(str(pos.get("num", 0)).strip())
            except (TypeError, ValueError):
                num = 0
            qty        = float(pos.get("quantity") or 1)
            total      = float(pos.get("total_cost") or 0)
            unit_price = total / qty if qty else total
            category   = _CATEGORY_BY_NUM.get(num, pos.get("category") or "прочее")
            items.append({
                "name":     pos["name"],
                "unit":     pos.get("unit") or "",
                "my_price": round(unit_price, 2),
                "category": category,
            })

        db.insert_price_items_batch(items)
        log.info(f"Price list initialized with {len(items)} positions from Vlad's smeta")
    except Exception as e:
        log.error(f"Smeta init error: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if not check_config():
        return
    db.init_db()
    log.info("Database ready")
    port = int(os.environ.get('PORT', 5000))
    log.info(f"Dashboard starting on http://0.0.0.0:{port}")
    # Start smeta init in background AFTER Flask is ready
    threading.Thread(target=_init_price_list_from_smeta, daemon=True).start()
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
