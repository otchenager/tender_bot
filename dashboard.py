"""Flask dashboard — 4 tabs: suitable tenders, price list, settings, AI chat."""

import io
import json

import anthropic
from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for

import db
import docgen
import file_processor
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL
from logger import get_logger
from ratelimit import global_rate_limit, rate_limit

log = get_logger("dashboard")
app = Flask(__name__)


@app.before_request
def _rate_guard():
    return global_rate_limit()


# Bumped when a deploy must be externally detectable (Railway gives no
# other cheap signal that the new build is serving).
APP_REV = "2026-07-24.all-tenders-view.1"


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "rev": APP_REV})

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


@app.template_filter("fmt_days_remaining")
def fmt_days_remaining(value):
    """ISO deadline → 'N дн.' / 'сегодня' / 'истёк'. Falls back to the raw
    text for the rare non-ISO deadline the scraper couldn't parse."""
    if not value:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        days = (dt - datetime.now(timezone.utc)).days
        if days < 0:
            return "истёк"
        if days == 0:
            return "сегодня"
        return f"{days} дн."
    except (TypeError, ValueError):
        return str(value)[:10]


@app.template_filter("fmt_relative")
def fmt_relative(value):
    """TIMESTAMPTZ/ISO → 'N ч. назад' / 'N дн. назад' (coarse, Russian)."""
    if not value:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if hours < 1:
            return "менее часа назад"
        if hours < 24:
            return f"{int(hours)} ч. назад"
        return f"{int(hours // 24)} дн. назад"
    except (TypeError, ValueError):
        return str(value)[:16]


# ---------------------------------------------------------------------------
# Tab 1 — Suitable tenders
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    tenders = db.get_suitable_tenders()
    active_count = sum(1 for t in tenders if t["status"] != "archived")
    rejected = db.get_rejected_counts(hours=24)
    params_updated_at = db.get_search_settings().get("params_updated_at")
    return render_template(
        "index.html",
        tenders=tenders,
        active_count=active_count,
        rejected=rejected,
        params_updated_at=params_updated_at,
    )


@app.route("/revalidate/trigger", methods=["POST"])
@rate_limit(10, 60)
def revalidate_trigger():
    requested_at = db.request_manual_revalidation()
    return jsonify({"requested_at": requested_at})


@app.route("/revalidate/status")
@rate_limit(120, 60)  # polled every few seconds while a check is running
def revalidate_status():
    since = request.args.get("since", "")
    state = db.get_revalidation_state()
    finished_at = state.get("last_run_finished_at")
    done = bool(since and finished_at and finished_at.isoformat() > since)
    return jsonify({
        "done": done,
        "checked_count": state.get("last_checked_count"),
        "archived_count": state.get("last_archived_count"),
    })


# ---------------------------------------------------------------------------
# All tenders — full-funnel view combining tenders_raw + tenders, so the
# contractor can see everything the parser has ever touched (queued,
# filtered out, rejected, suitable, archived), not just the actionable
# subset shown on the main tab. Filtering/sorting happens client-side in
# JS (see all_tenders.html) — the row count is small enough (low
# thousands) that a single render + JS filter is simpler and snappier
# than paginated server-side queries.
# ---------------------------------------------------------------------------

@app.route("/all-tenders")
def all_tenders():
    rows = db.get_all_tenders_combined()
    return render_template("all_tenders.html", rows=rows, all_regions=ALL_REGIONS)


@app.route("/tender/<int:tender_id>")
def tender_detail(tender_id):
    tender = db.get_tender(tender_id)
    if not tender:
        return "Тендер не найден", 404
    positions = db.get_tender_positions(tender_id)
    return render_template("tender.html", tender=tender, positions=positions)


# ---------------------------------------------------------------------------
# Document package + submit decision (HUMAN IN THE LOOP — see docgen.py)
# ---------------------------------------------------------------------------

@app.route("/tender/<int:tender_id>/documents", methods=["POST"])
@rate_limit(10, 60)  # each package may include a Claude draft call
def tender_documents(tender_id):
    package = docgen.generate_package(tender_id)
    if package is None:
        return "Тендер не найден", 404
    zip_bytes, filename = package
    return send_file(
        io.BytesIO(zip_bytes),
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/tender/<int:tender_id>/submit", methods=["POST"])
@rate_limit(10, 60)
def tender_submit(tender_id):
    # Iron rule 3: only an explicit per-tender confirmation from the user
    # flips the status, and even then nothing is sent anywhere — actual
    # submission happens manually with the director's digital signature.
    if request.form.get("confirm") != "yes":
        return redirect(url_for("tender_detail", tender_id=tender_id))
    db.mark_tender_submitted(tender_id)
    log.info(f"User confirmed submission decision for tender {tender_id}")
    return redirect(url_for("tender_detail", tender_id=tender_id))


# ---------------------------------------------------------------------------
# Company profile (sensitive)
# ---------------------------------------------------------------------------

@app.route("/profile")
def profile():
    saved = request.args.get("saved")
    return render_template("profile.html", profile=db.get_company_profile(), saved=saved)


@app.route("/profile/save", methods=["POST"])
@rate_limit(10, 60)
def profile_save():
    db.update_company_profile({
        f: request.form.get(f, "") for f in db.COMPANY_PROFILE_FIELDS
    })
    return redirect(url_for("profile", saved=1))


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
@rate_limit(10, 60)  # each save triggers a full rescore of stored tenders
def settings_save():
    regions = request.form.getlist("regions")
    try:
        min_budget = float(request.form.get("min_budget", 36000))
    except ValueError:
        min_budget = 36000.0
    try:
        # Defaults to min_budget itself when left blank — same "no behavior
        # change until consciously set" semantics the VPS .env value used to have.
        min_budget_single_source = float(
            request.form.get("min_budget_single_source") or min_budget
        )
    except ValueError:
        min_budget_single_source = min_budget
    try:
        x_threshold = float(request.form.get("x_threshold", 30))
    except ValueError:
        x_threshold = 30.0
    try:
        y_threshold = float(request.form.get("y_threshold", 5))
    except ValueError:
        y_threshold = 5.0

    new_settings = {
        "min_budget": min_budget,
        "min_budget_single_source": min_budget_single_source,
        "x_threshold": x_threshold,
        "y_threshold": y_threshold,
        "regions": regions,
    }
    db.update_search_settings(new_settings)

    # Correctness over speed: results computed under the OLD parameters must
    # not linger. Re-score every formula-decided tender under the new
    # settings right away — Python-only (stored confidences), no Claude.
    try:
        stats = file_processor.rescore_existing_tenders()
    except Exception as e:
        log.error(f"Rescore after settings save failed: {e}")
        stats = {"suitable": 0, "rejected": 0, "archived": 0}

    # Same idea for tenders_raw: rows filtered_out under the OLD budget/region
    # can flip to passed instantly, with no re-scraping.
    try:
        raw_stats = db.recompute_all_raw_BR(db.get_search_settings())
        log.info(f"Raw recompute after settings save: {raw_stats}")
    except Exception as e:
        log.error(f"Raw recompute after settings save failed: {e}")

    return redirect(url_for("settings", saved=1, **stats))


@app.route("/settings/preview")
@rate_limit(60, 60)  # fired on slider drag (debounced client-side)
def settings_preview():
    """Live preview: how many already-analyzed tenders would pass under the
    parameters currently set in the form (nothing is saved)."""
    try:
        trial = {
            "min_budget": float(request.args.get("min_budget", 0)),
            "x_threshold": float(request.args.get("x_threshold", 30)),
            "y_threshold": float(request.args.get("y_threshold", 5)),
            "regions": request.args.getlist("regions"),
        }
    except (TypeError, ValueError):
        return jsonify({"error": "bad params"}), 400
    return jsonify(file_processor.preview_pass_counts(trial))


# ---------------------------------------------------------------------------
# Tab 4 — AI assistant
# ---------------------------------------------------------------------------

@app.route("/chat")
def chat():
    return render_template("chat.html")


@app.route("/chat/message", methods=["POST"])
@rate_limit(10, 60)  # each call is a Claude API call — keep abuse cheap
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
- Отсеяно по ключевым словам (I): {db.count_rejected('keyword')}
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
