"""PostgreSQL database layer — all DB access goes through this module."""

import json
from contextlib import contextmanager
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config import DATABASE_URL
from logger import get_logger

log = get_logger("db")

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(2, 10, DATABASE_URL)
    return _pool


@contextmanager
def _conn():
    pool = _get_pool()
    conn = pool.getconn()
    try:
        conn.autocommit = False
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db():
    with _conn() as conn:
        cur = conn.cursor()

        # price_items must come before tender_positions (FK reference)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_items (
                id          SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                category    TEXT,
                unit        TEXT,
                my_price    FLOAT,
                is_active   BOOLEAN DEFAULT TRUE,
                is_unknown  BOOLEAN DEFAULT FALSE,
                occurrences INT DEFAULT 0,
                created_at  TIMESTAMPTZ DEFAULT NOW(),
                updated_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenders (
                id            SERIAL PRIMARY KEY,
                external_id   TEXT NOT NULL,
                source        TEXT NOT NULL,
                title         TEXT,
                url           TEXT,
                region        TEXT,
                budget_byn    FLOAT,
                deadline      TEXT,
                status        TEXT DEFAULT 'pending',
                reject_reason TEXT,
                k_score       FLOAT,
                l_score       FLOAT,
                m_score       FLOAT,
                s_score       FLOAT,
                ai_comment    TEXT,
                created_at    TIMESTAMPTZ DEFAULT NOW(),
                updated_at    TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(external_id, source)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tender_positions (
                id                   SERIAL PRIMARY KEY,
                tender_id            INTEGER REFERENCES tenders(id) ON DELETE CASCADE,
                smeta_name           TEXT,
                smeta_unit           TEXT,
                smeta_quantity       FLOAT,
                smeta_labor_cost     FLOAT,
                smeta_material_cost  FLOAT,
                smeta_transport_cost FLOAT,
                smeta_total_cost     FLOAT,
                category             TEXT,
                matched_item_id      INTEGER REFERENCES price_items(id),
                confidence           FLOAT,
                match_status         TEXT,
                margin_byn           FLOAT,
                created_at           TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS search_settings (
                id    SERIAL PRIMARY KEY,
                key   TEXT UNIQUE NOT NULL,
                value TEXT
            )
        """)

        # Seed default settings (skip if already exist)
        defaults = [
            ("min_budget", "36000"),
            ("x_threshold", "30"),
            ("y_threshold", "5"),
            ("regions", json.dumps(["Минская", "г. Минск"])),
        ]
        for key, value in defaults:
            cur.execute("""
                INSERT INTO search_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO NOTHING
            """, (key, value))

    log.info("Database initialized")


# ---------------------------------------------------------------------------
# Tenders
# ---------------------------------------------------------------------------

def tender_exists(external_id: str, source: str) -> bool:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute(
            "SELECT 1 FROM tenders WHERE external_id = %s AND source = %s",
            (external_id, source),
        )
        return cur.fetchone() is not None


def save_tender(tender: dict) -> int:
    """Insert new tender with status=pending. Returns tender id."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tenders
                (external_id, source, title, url, region, budget_byn, deadline, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'pending')
            RETURNING id
        """, (
            tender["external_id"],
            tender["source"],
            tender.get("title"),
            tender.get("url"),
            tender.get("region"),
            tender.get("budget_byn"),
            tender.get("deadline"),
        ))
        return cur.fetchone()[0]


def reject_tender(tender_id: int, reason: str):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders
            SET status = 'rejected', reject_reason = %s, updated_at = NOW()
            WHERE id = %s
        """, (reason, tender_id))


def update_tender_scores(tender_id: int, k: float, l: float, m: float, s: float):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders
            SET k_score = %s, l_score = %s, m_score = %s, s_score = %s, updated_at = NOW()
            WHERE id = %s
        """, (k, l, m, s, tender_id))


def update_tender_status(tender_id: int, status: str):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders SET status = %s, updated_at = NOW() WHERE id = %s
        """, (status, tender_id))


def update_tender_ai_comment(tender_id: int, comment: str):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders SET ai_comment = %s, updated_at = NOW() WHERE id = %s
        """, (comment, tender_id))


def get_tender(tender_id: int) -> dict | None:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM tenders WHERE id = %s", (tender_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def get_last_external_id(source: str) -> str | None:
    """Return the external_id of the most recently saved tender for this source."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT external_id FROM tenders
            WHERE source = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (source,))
        row = cur.fetchone()
        return row[0] if row else None


# Rejection reasons that a settings change can overturn — everything the
# Python filters/formula decided. ai_error records are NOT rescorable (the
# analysis itself failed, there are no stored positions to re-score).
_RESCORABLE_REASONS = ("failed_B", "failed_R", "failed_K", "failed_L", "failed_M")


def get_rescorable_tenders() -> list[dict]:
    """Tenders whose status was decided by the search formula and therefore
    must be recomputed when the user changes parameters."""
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT * FROM tenders
            WHERE status = 'suitable'
               OR (status = 'rejected' AND reject_reason IN %s)
        """, (_RESCORABLE_REASONS,))
        return [dict(r) for r in cur.fetchall()]


def archive_tender(tender_id: int, marker: str):
    """Park a tender that can't be re-scored under new settings; the marker
    (e.g. 'результаты до изменения от 11.07.2026') is shown instead of mixing
    old-criteria results with new ones."""
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE tenders
            SET status = 'archived', reject_reason = %s, updated_at = NOW()
            WHERE id = %s
        """, (marker, tender_id))


def get_suitable_tenders(limit: int = 200) -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT * FROM tenders
            WHERE status = 'suitable'
            ORDER BY s_score DESC NULLS LAST, created_at DESC
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def get_rejected_counts(hours: int = 24) -> dict:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT reject_reason, COUNT(*) as cnt
            FROM tenders
            WHERE status = 'rejected'
              AND created_at >= NOW() - INTERVAL '%s hours'
            GROUP BY reject_reason
        """ % hours)
        return {row["reject_reason"]: row["cnt"] for row in cur.fetchall()}


def get_last_suitable_tenders(limit: int = 5) -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT id, title, budget_byn, region, k_score, l_score, m_score, s_score
            FROM tenders
            WHERE status = 'suitable'
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        return [dict(r) for r in cur.fetchall()]


def count_tenders() -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tenders")
        return cur.fetchone()[0]


def count_suitable() -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tenders WHERE status = 'suitable'")
        return cur.fetchone()[0]


def count_rejected(reason: str) -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM tenders WHERE status = 'rejected' AND reject_reason = %s",
            (reason,),
        )
        return cur.fetchone()[0]


def avg_margin() -> float:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT AVG(m_score) FROM tenders WHERE status = 'suitable' AND m_score IS NOT NULL")
        val = cur.fetchone()[0]
        return round(float(val) * 100, 1) if val else 0.0


# ---------------------------------------------------------------------------
# Tender positions
# ---------------------------------------------------------------------------

def save_tender_positions(tender_id: int, positions: list[dict]):
    """Insert extracted + matched positions. Each dict has smeta_* and match fields."""
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM tender_positions WHERE tender_id = %s", (tender_id,))
        for p in positions:
            cur.execute("""
                INSERT INTO tender_positions
                    (tender_id, smeta_name, smeta_unit, smeta_quantity,
                     smeta_labor_cost, smeta_material_cost, smeta_transport_cost,
                     smeta_total_cost, category, matched_item_id, confidence,
                     match_status, margin_byn)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                tender_id,
                p.get("smeta_name"),
                p.get("smeta_unit"),
                p.get("smeta_quantity"),
                p.get("smeta_labor_cost"),
                p.get("smeta_material_cost"),
                p.get("smeta_transport_cost"),
                p.get("smeta_total_cost"),
                p.get("category"),
                p.get("matched_item_id"),
                p.get("confidence"),
                p.get("match_status"),
                p.get("margin_byn"),
            ))


def get_tender_positions(tender_id: int) -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT
                tp.*,
                pi.name  AS matched_item_name,
                pi.my_price AS matched_my_price
            FROM tender_positions tp
            LEFT JOIN price_items pi ON tp.matched_item_id = pi.id
            WHERE tp.tender_id = %s
            ORDER BY tp.smeta_total_cost DESC NULLS LAST
        """, (tender_id,))
        return [dict(r) for r in cur.fetchall()]


def get_top_positions(tender_id: int, limit: int = 5) -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT smeta_name, smeta_unit, smeta_quantity, smeta_total_cost, match_status
            FROM tender_positions
            WHERE tender_id = %s
            ORDER BY smeta_total_cost DESC NULLS LAST
            LIMIT %s
        """, (tender_id, limit))
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Price items
# ---------------------------------------------------------------------------

def get_active_price_item_names() -> list[str]:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT name FROM price_items WHERE is_active = TRUE")
        return [r[0] for r in cur.fetchall()]


def get_active_price_items() -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("""
            SELECT * FROM price_items WHERE is_active = TRUE ORDER BY category, name
        """)
        return [dict(r) for r in cur.fetchall()]


def get_all_price_items() -> list[dict]:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM price_items ORDER BY category, name")
        return [dict(r) for r in cur.fetchall()]


def get_price_item(item_id: int) -> dict | None:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT * FROM price_items WHERE id = %s", (item_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def price_items_count() -> int:
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM price_items WHERE is_unknown = FALSE")
        return cur.fetchone()[0]


def save_price_item(item_id: int | None, name: str, unit: str,
                    my_price: float, category: str, is_active: bool = True) -> int:
    with _conn() as conn:
        cur = conn.cursor()
        if item_id:
            cur.execute("""
                UPDATE price_items
                SET name=%s, unit=%s, my_price=%s, category=%s, is_active=%s, updated_at=NOW()
                WHERE id=%s
                RETURNING id
            """, (name, unit, my_price, category, is_active, item_id))
            return cur.fetchone()[0]
        else:
            cur.execute("""
                INSERT INTO price_items (name, unit, my_price, category, is_active)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (name, unit, my_price, category, is_active))
            return cur.fetchone()[0]


def toggle_price_item(item_id: int, is_active: bool):
    with _conn() as conn:
        conn.cursor().execute("""
            UPDATE price_items SET is_active = %s, updated_at = NOW() WHERE id = %s
        """, (is_active, item_id))


def delete_price_item(item_id: int):
    with _conn() as conn:
        conn.cursor().execute("DELETE FROM price_items WHERE id = %s", (item_id,))


def add_unknown_price_item(name: str, unit: str, my_price: float):
    """Add grey-match position as inactive unknown item (contractor reviews later)."""
    with _conn() as conn:
        cur = conn.cursor()
        # Skip if already exists (by name)
        cur.execute("SELECT 1 FROM price_items WHERE name = %s", (name,))
        if cur.fetchone():
            return
        cur.execute("""
            INSERT INTO price_items (name, unit, my_price, is_active, is_unknown)
            VALUES (%s, %s, %s, FALSE, TRUE)
        """, (name, unit, my_price))


def insert_price_items_batch(items: list[dict]):
    """Bulk insert for initial price list population."""
    with _conn() as conn:
        cur = conn.cursor()
        for item in items:
            cur.execute("""
                INSERT INTO price_items (name, category, unit, my_price, is_active, is_unknown)
                VALUES (%s, %s, %s, %s, TRUE, FALSE)
                ON CONFLICT DO NOTHING
            """, (item["name"], item["category"], item["unit"], item["my_price"]))


# ---------------------------------------------------------------------------
# Search settings
# ---------------------------------------------------------------------------

def get_search_settings() -> dict:
    with _conn() as conn:
        cur = _dict_cursor(conn)
        cur.execute("SELECT key, value FROM search_settings")
        rows = {r["key"]: r["value"] for r in cur.fetchall()}
    return {
        "min_budget": float(rows.get("min_budget", 36000)),
        "x_threshold": float(rows.get("x_threshold", 30)),
        "y_threshold": float(rows.get("y_threshold", 5)),
        "regions": json.loads(rows.get("regions", '["Минская","г. Минск"]')),
        # When the parameters were last changed (ISO); None until first save.
        # The dashboard shows results as "актуально на {this date}".
        "params_updated_at": rows.get("params_updated_at"),
    }


def update_search_settings(settings: dict):
    # Every save stamps the change moment so the dashboard can state which
    # parameter version the displayed results were computed under.
    settings = {**settings, "params_updated_at": _now()}
    with _conn() as conn:
        cur = conn.cursor()
        for key, value in settings.items():
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            else:
                value = str(value)
            cur.execute("""
                INSERT INTO search_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, value))


# ---------------------------------------------------------------------------
# AI assistant context helpers
# ---------------------------------------------------------------------------

def get_all_price_items_as_text() -> str:
    items = get_active_price_items()
    if not items:
        return "Прайс-лист пуст."
    lines = ["Наименование | Ед. | Цена BYN | Категория"]
    for i in items:
        lines.append(f"{i['name']} | {i['unit']} | {i['my_price']} | {i['category']}")
    return "\n".join(lines)


def get_search_settings_as_text() -> str:
    s = get_search_settings()
    return (
        f"Минимальный бюджет: {s['min_budget']} BYN\n"
        f"Порог релевантности X: {s['x_threshold']}%\n"
        f"Минимальная маржа Y: {s['y_threshold']}%\n"
        f"Регионы: {', '.join(s['regions'])}"
    )


def get_suitable_tenders_summary() -> str:
    tenders = get_last_suitable_tenders(limit=10)
    if not tenders:
        return "Подходящих тендеров пока нет."
    lines = []
    for t in tenders:
        lines.append(
            f"- {t['title'][:80]} | {t['budget_byn']} BYN | "
            f"K={t['k_score']:.0%} L={t['l_score']:.0%} M={t['m_score']:.0%} S={t['s_score']:.2f}"
        )
    return "\n".join(lines)
