"""Flask dashboard — 4 tabs: suitable tenders, price list, settings, AI chat."""

import json

import anthropic
from flask import Flask, jsonify, redirect, render_template, request, url_for

import db
import file_processor
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from logger import get_logger

log = get_logger("dashboard")
app = Flask(__name__)

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=2)

ALL_REGIONS = [
    "Минская", "Витебская", "Могилёвская", "Гродненская",
    "Брестская", "Гомельская", "г. Минск",
]


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

@app.template_filter("fmt_money")
def fmt_money(value):
    if value is None:
        return "—"
    try:
        return "{:,.0f}".format(float(value)).replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


@app.template_filter("fmt_pct")
def fmt_pct(value):
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


@app.template_filter("fmt_date")
def fmt_date(value):
    """ISO timestamp → dd.mm.yyyy HH:MM (human-readable, local wording)."""
    if not value:
        return "—"
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(value))
        return dt.strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(value)[:16]


# ---------------------------------------------------------------------------
# Tab 1 — Suitable tenders
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    tenders = db.get_suitable_tenders()
    rejected = db.get_rejected_counts(hours=24)
    params_updated_at = db.get_search_settings().get("params_updated_at")
    return render_template(
        "index.html",
        tenders=tenders,
        rejected=rejected,
        params_updated_at=params_updated_at,
    )


@app.route("/tender/<int:tender_id>")
def tender_detail(tender_id):
    tender = db.get_tender(tender_id)
    if not tender:
        return "Тендер не найден", 404
    positions = db.get_tender_positions(tender_id)
    return render_template("tender.html", tender=tender, positions=positions)


# ---------------------------------------------------------------------------
# Tab 2 — Price list
# ---------------------------------------------------------------------------

@app.route("/prices")
def prices():
    items = db.get_all_price_items()
    return render_template("prices.html", items=items)


@app.route("/prices/item/<int:item_id>/price", methods=["POST"])
def prices_update_price(item_id):
    item = db.get_price_item(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404
    try:
        new_price = float(request.json.get("price", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid price"}), 400
    db.save_price_item(
        item_id, item["name"], item["unit"], new_price,
        item["category"], item["is_active"],
    )
    return jsonify({"ok": True})


@app.route("/prices/item/<int:item_id>/toggle", methods=["POST"])
def prices_toggle(item_id):
    item = db.get_price_item(item_id)
    if not item:
        return jsonify({"error": "not found"}), 404
    db.toggle_price_item(item_id, not item["is_active"])
    return jsonify({"is_active": not item["is_active"]})


@app.route("/prices/item/<int:item_id>/delete", methods=["POST"])
def prices_delete(item_id):
    db.delete_price_item(item_id)
    return redirect(url_for("prices"))


@app.route("/prices/add", methods=["POST"])
def prices_add():
    name = request.form.get("name", "").strip()
    if not name:
        return redirect(url_for("prices"))
    try:
        price = float(request.form.get("price", 0))
    except (TypeError, ValueError):
        price = 0.0
    db.save_price_item(
        None,
        name,
        request.form.get("unit", ""),
        price,
        request.form.get("category", "прочее"),
    )
    return redirect(url_for("prices"))


# ---------------------------------------------------------------------------
# Tab 3 — Search settings
# ---------------------------------------------------------------------------

@app.route("/settings")
def settings():
    s = db.get_search_settings()
    # Rescore stats after a save (redirect carries them as query params)
    rescore_stats = None
    if request.args.get("saved"):
        rescore_stats = {
            "suitable": request.args.get("suitable", "0"),
            "rejected": request.args.get("rejected", "0"),
            "archived": request.args.get("archived", "0"),
        }
    return render_template(
        "settings.html", settings=s, all_regions=ALL_REGIONS,
        rescore_stats=rescore_stats,
    )


@app.route("/settings/save", methods=["POST"])
def settings_save():
    regions = request.form.getlist("regions")
    try:
        min_budget = float(request.form.get("min_budget", 36000))
    except ValueError:
        min_budget = 36000.0
    try:
        x_threshold = float(request.form.get("x_threshold", 30))
    except ValueError:
        x_threshold = 30.0
    try:
        y_threshold = float(request.form.get("y_threshold", 5))
    except ValueError:
        y_threshold = 5.0

    db.update_search_settings({
        "min_budget": min_budget,
        "x_threshold": x_threshold,
        "y_threshold": y_threshold,
        "regions": regions,
    })

    # Correctness over speed: results computed under the OLD parameters must
    # not linger. Re-score every formula-decided tender under the new
    # settings right away — Python-only (stored confidences), no Claude.
    try:
        stats = file_processor.rescore_existing_tenders()
    except Exception as e:
        log.error(f"Rescore after settings save failed: {e}")
        stats = {"suitable": 0, "rejected": 0, "archived": 0}

    return redirect(url_for("settings", saved=1, **stats))


# ---------------------------------------------------------------------------
# Tab 4 — AI assistant
# ---------------------------------------------------------------------------

@app.route("/chat")
def chat():
    return render_template("chat.html")


@app.route("/chat/message", methods=["POST"])
def chat_message():
    user_msg = (request.json or {}).get("message", "").strip()
    if not user_msg:
        return jsonify({"error": "Пустое сообщение"}), 400

    system_prompt = f"""Ты — AI-ассистент для белорусского строительного подрядчика.
Отвечай на русском языке. Будь конкретен и практичен.

МОЙ ПРАЙС-ЛИСТ:
{db.get_all_price_items_as_text()}

ПАРАМЕТРЫ ПОИСКА:
{db.get_search_settings_as_text()}

ПОСЛЕДНИЕ 10 ПОДХОДЯЩИХ ТЕНДЕРОВ:
{db.get_suitable_tenders_summary()}

СТАТИСТИКА ВОРОНКИ:
- Всего обработано: {db.count_tenders()}
- Подходящих найдено: {db.count_suitable()}
- Отсеяно по ключевым словам (I): {db.count_rejected('failed_I')}
- Отсеяно по бюджету (B): {db.count_rejected('failed_B')}
- Отсеяно по региону (R): {db.count_rejected('failed_R')}
- Отсеяно мало совпадений (K): {db.count_rejected('failed_K')}
- Отсеяно низкая релевантность (L): {db.count_rejected('failed_L')}
- Отсеяно низкая маржа (M): {db.count_rejected('failed_M')}
- Средняя маржа: {db.avg_margin()}%
"""

    try:
        msg = _claude.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        reply = "".join(b.text for b in msg.content if b.type == "text")
    except Exception as e:
        log.error(f"Chat API error: {e}")
        reply = "Ошибка при обращении к AI. Попробуйте ещё раз."

    return jsonify({"response": reply})


# ---------------------------------------------------------------------------
# Runner (used by main.py)
# ---------------------------------------------------------------------------

def run_dashboard():
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
