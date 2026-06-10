"""Flask-дашборд для просмотра тендеров, анализа и управления ценами."""

import json

from flask import Flask, jsonify, render_template, request

import db
from logger import get_logger

log = get_logger("dashboard")

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _fetch_tenders(filters: dict = None):
    """Возвращает список тендеров с данными анализа, с учётом фильтров."""
    filters = filters or {}

    query = """
        SELECT t.*, a.analysis, a.score, a.verdict, a.margin_byn, a.margin_pct
        FROM tenders t
        LEFT JOIN ai_analysis a ON a.tender_id = t.id
        WHERE 1=1
    """
    params = []

    if filters.get("min_score"):
        query += " AND a.score >= ?"
        params.append(filters["min_score"])

    if filters.get("source"):
        query += " AND t.source = ?"
        params.append(filters["source"])

    if filters.get("category"):
        query += " AND (t.matched_group = ? OR t.category LIKE ?)"
        params.append(filters["category"])
        params.append(f"%{filters['category']}%")

    query += " ORDER BY a.score DESC NULLS LAST, t.parsed_at DESC"

    conn = db.get_conn()
    try:
        cur = conn.execute(query, params)
    except db.sqlite3.OperationalError:
        # NULLS LAST не поддерживается в старых версиях sqlite
        query = query.replace("a.score DESC NULLS LAST", "a.score IS NULL, a.score DESC")
        cur = conn.execute(query, params)

    return [dict(row) for row in cur.fetchall()]


def _fetch_tender(tender_id: str):
    conn = db.get_conn()
    cur = conn.execute("""
        SELECT t.*, a.analysis, a.score, a.verdict, a.margin_byn, a.margin_pct
        FROM tenders t
        LEFT JOIN ai_analysis a ON a.tender_id = t.id
        WHERE t.id = ?
    """, (tender_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def _smeta_positions_for_tender(tender: dict):
    """Пытается получить позиции сметы и расчёт маржи для отображения."""
    try:
        from smeta_parser import process_tender_smeta
        from margin_calculator import calculate_margin

        positions = process_tender_smeta(tender)
        if not positions:
            return None

        price_items = db.get_price_items()
        return calculate_margin(positions, price_items)
    except Exception as e:
        log.error(f"Не удалось получить позиции сметы для {tender.get('id')}: {e}")
        return None


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    filters = {
        "min_score": request.args.get("min_score", type=int),
        "source": request.args.get("source") or None,
        "category": request.args.get("category") or None,
    }
    tenders = _fetch_tenders(filters)
    return render_template("index.html", tenders=tenders, filters=filters)


@app.route("/tender/<tender_id>")
def tender_detail(tender_id):
    tender = _fetch_tender(tender_id)
    if not tender:
        return "Тендер не найден", 404

    raw_data = tender.get("raw_data")
    if isinstance(raw_data, str):
        try:
            tender["raw_data"] = json.loads(raw_data)
        except (json.JSONDecodeError, TypeError):
            tender["raw_data"] = {}

    margin_result = None
    if tender.get("margin_byn") is not None:
        margin_result = _smeta_positions_for_tender(tender)

    return render_template("tender.html", tender=tender, margin_result=margin_result)


@app.route("/tender/<tender_id>/feedback", methods=["POST"])
def tender_feedback(tender_id):
    reaction = request.form.get("reaction") or (request.json or {}).get("reaction")
    reason = request.form.get("reason") or (request.json or {}).get("reason")
    comment = request.form.get("comment") or (request.json or {}).get("comment")

    if reaction not in ("👍", "🤷", "👎"):
        return jsonify({"error": "invalid reaction"}), 400

    db.save_feedback(tender_id, reaction, reason, comment)
    return jsonify({"ok": True})


@app.route("/prices")
def prices():
    items = db.get_price_items()
    materials = [i for i in items if (i.get("category") or "").lower().startswith("матер")]
    works = [i for i in items if (i.get("category") or "").lower().startswith("работ")]
    other = [i for i in items if i not in materials and i not in works]
    return render_template("prices.html", materials=materials, works=works, other=other)


@app.route("/prices/save", methods=["POST"])
def prices_save():
    """Сохраняет обновлённые цены и/или добавляет новую позицию."""
    new_name = request.form.get("new_name")
    if new_name:
        db.save_price_item(
            name=new_name,
            unit=request.form.get("new_unit", ""),
            my_price=request.form.get("new_price", type=float),
            category=request.form.get("new_category", "Материалы"),
        )

    for key, value in request.form.items():
        if not key.startswith("price_"):
            continue
        item_id = key.split("_", 1)[1]
        if not value:
            continue
        try:
            price = float(value)
        except ValueError:
            continue

        conn = db.get_conn()
        cur = conn.execute("SELECT name, unit, category FROM price_items WHERE id = ?", (item_id,))
        row = cur.fetchone()
        if row:
            db.save_price_item(row["name"], row["unit"], price, row["category"])

    return render_template_redirect_to_prices()


def render_template_redirect_to_prices():
    from flask import redirect, url_for
    return redirect(url_for("prices"))


@app.route("/api/tenders")
def api_tenders():
    filters = {
        "min_score": request.args.get("min_score", type=int),
        "source": request.args.get("source") or None,
        "category": request.args.get("category") or None,
    }
    tenders = _fetch_tenders(filters)
    for t in tenders:
        t.pop("raw_data", None)
    return jsonify(tenders)


@app.route("/api/stats")
def api_stats():
    conn = db.get_conn()

    total_tenders = conn.execute("SELECT COUNT(*) c FROM tenders").fetchone()["c"]
    analyzed = conn.execute("SELECT COUNT(*) c FROM ai_analysis").fetchone()["c"]
    sent = conn.execute("SELECT COUNT(*) c FROM tenders WHERE sent = 1").fetchone()["c"]

    by_source = conn.execute("""
        SELECT source, COUNT(*) c FROM tenders GROUP BY source
    """).fetchall()

    by_verdict = conn.execute("""
        SELECT verdict, COUNT(*) c FROM ai_analysis GROUP BY verdict
    """).fetchall()

    feedback_stats = db.get_feedback_stats()

    return jsonify({
        "total_tenders": total_tenders,
        "analyzed": analyzed,
        "sent": sent,
        "by_source": {row["source"]: row["c"] for row in by_source},
        "by_verdict": {row["verdict"]: row["c"] for row in by_verdict},
        "feedback": feedback_stats,
    })


def run_dashboard():
    """Запускает Flask-дашборд (блокирующий вызов, для запуска в отдельном потоке)."""
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
